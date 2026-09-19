"""动态触发路由层：接管 chat.receive.after_process hook 的入站消息。

``DynamicDispatcher`` 只碰 hook ``message``——重组 raw_message 文本、判定这条消息是
「带图添加 / 动态触发命中 / 内置命令正文需修正 / 该放行」，再委托 ``CommandService``
执行业务。它不直接碰 storage/images/scope，命令业务统一由 service 完成。

改造历史：早期版本把动态触发也用 @Command(pattern=r"^{prefix}(?P<trigger>.+)$")
注册，主程序"第一个 pattern 命中即独占"的分发器会让本插件抢走所有"前缀+任意字符"
的消息，handler 查不到 trigger 时也不能让出——其他插件用同 prefix 的命令会被永久屏蔽。
Hook 路径按 order 顺次执行，未 abort 就放行，彻底绕过 first-match-wins。

正文定义：**只认 raw_message 的 text 段拼接**。Host 的 processed_plain_text 会把被引用消息
原文、@昵称、占位符用空格拼在一起，拿它匹配会让引用内容参与命令；无 text 段的消息不再
回退 processed_plain_text（只有引用、没有正文时，引用内容不是命令输入）。

内置命令正文修正：Host 用 processed_plain_text 跑 @Command 正则，正文是内置命令但被引用/@/
首尾空白污染时 Host 会失配或命中错误的命令。本 hook 允许改参（chat.receive.after_process 允许
modified_kwargs），此时把 message.processed_plain_text 改写为纯正文后 continue，Host 随即按
干净正文匹配到正确组件，组件启停、聊天级禁用、命令前后 hook 全部由 Host 正常流程处理。

耗时与超时：Host 对 blocking hook 有超时（本插件声明 20s），超时后按 ErrorPolicy.SKIP 放行、
消息继续落入 LLM，而 Runner 内的 handler 协程不会被取消——大图发送慢时会出现「图发出去了、
LLM 又回了一条」。因此本层在**确认命中后立即返回 abort**，把发送/落盘交给 ``BackgroundExecutor``
（有界：并发上限 + 排队上限）；执行器拒收（满载 / 卸载中）时**不**在 hook 内同步执行，而是仍
abort 并发一条带超时的短回执，日志可追踪。后台任务在协程体内绑定提交时捕获的代际，
写入与清理由数据层按代际校验。
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from collections import deque
from typing import TYPE_CHECKING, Any, Callable, Coroutine, Deque, Dict, List, Optional, Set, Tuple

from .common import (
    KW_ADD,
    KW_ADD_ANSWER,
    MessageRoute,
    build_message_route,
    extract_text_and_images,
    is_reserved_trigger,
)

if TYPE_CHECKING:
    from ..plugin import CustomCommandsPlugin

logger = logging.getLogger(__name__)

# 后台执行器预算：同时运行上限 / 排队上限。超出即快速拒绝（abort + 短回执），不同步执行。
_MAX_CONCURRENT_TASKS = 8
_MAX_QUEUED_TASKS = 64
# 动态回复投递（只发送、不写数据）的单任务时限，超时取消并记日志。
_DELIVER_TIMEOUT_SECONDS = 60.0
# 带图添加（写数据）不取消：取消等待协程不会终止已在线程池里的文件写入，只会破坏事务收尾；
# 超过此时限仅记 warning 供排查。
_IMAGE_ADD_SLOW_WARN_SECONDS = 60.0

_BUSY_TEXT = "⏳ 当前处理繁忙，请稍后再试"
_RELOADING_TEXT = "⏳ 插件正在重载，请稍后再试"
_ADD_STATUS_UNAVAILABLE_TEXT = "暂时无法确认添加命令的启用状态，请稍后重试"

CoroFactory = Callable[[], Coroutine[Any, Any, Any]]


class BackgroundExecutor:
    """有界后台执行器：``max_concurrent`` 个并发 + ``max_queued`` 个排队，超出快速拒绝。

    - 接受的工作要么立刻启动、要么进队列，进队列的在有空位时自动启动；两者都在生命周期内。
    - 排空（卸载）：不再接收新工作，已接受的（含排队）继续跑到超时；超时后清空队列、
      不再启动新任务，仍在跑的任务由数据层的退役/代际约束限制其写入。
    - 任务用工厂延迟创建协程，被拒绝时不会产生未 await 的协程对象。
    """

    def __init__(self, max_concurrent: int = _MAX_CONCURRENT_TASKS, max_queued: int = _MAX_QUEUED_TASKS) -> None:
        self._running: Set["asyncio.Task[Any]"] = set()
        self._queue: Deque[Tuple[CoroFactory, str]] = deque()
        self._closed = False
        self._max_concurrent = max(1, max_concurrent)
        self._max_queued = max(0, max_queued)

    @property
    def closed(self) -> bool:
        return self._closed

    @property
    def running(self) -> int:
        return len(self._running)

    @property
    def queued(self) -> int:
        return len(self._queue)

    def submit(self, factory: CoroFactory, *, name: str) -> Optional[str]:
        """提交工作；接受返回 None，拒绝返回原因（``closed`` / ``full``）。"""
        if self._closed:
            return "closed"
        if len(self._running) < self._max_concurrent:
            self._start(factory, name)
            return None
        if len(self._queue) >= self._max_queued:
            return "full"
        self._queue.append((factory, name))
        return None

    def _start(self, factory: CoroFactory, name: str) -> None:
        task = asyncio.create_task(factory(), name=name)
        self._running.add(task)
        task.add_done_callback(self._on_done)

    def _on_done(self, task: "asyncio.Task[Any]") -> None:
        self._running.discard(task)
        if not task.cancelled() and task.exception() is not None:
            logger.error("后台任务 %s 异常结束: %r", task.get_name(), task.exception())
        # 已接受的排队工作即便在排空期间也继续启动；排空超时后队列已被清空，不会再启动新任务。
        while self._queue and len(self._running) < self._max_concurrent:
            factory, name = self._queue.popleft()
            self._start(factory, name)

    async def drain(self, timeout: float) -> Tuple[int, int]:
        """停止接收新工作并等待在途（运行中 + 排队）结束，超时则清空队列、放弃等待。

        Returns:
            (超时后仍在运行的任务数, 被丢弃的排队工作数)。
        """
        self._closed = True
        deadline = time.monotonic() + max(0.0, timeout)
        while self._running or self._queue:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            if not self._running:
                await asyncio.sleep(0)  # 队列非空但任务尚未由 done_callback 启动，让出一轮
                continue
            await asyncio.wait(set(self._running), timeout=remaining)
        dropped = len(self._queue)
        self._queue.clear()
        return len(self._running), dropped


class DynamicDispatcher:
    """入站消息的动态命令路由。"""

    def __init__(self, plugin: "CustomCommandsPlugin") -> None:
        self._plugin = plugin
        self.background = BackgroundExecutor()

    def reopen(self) -> None:
        """on_load 时调用：换一个全新的执行器。

        重载失败回滚时 Host 会对同一个旧实例再次 on_load，而上一次 on_unload 的 drain 已把
        执行器置为关闭；若不换新，动态分发会一直被拒收。旧执行器里超期未结束的任务继续
        自然结束，它们绑定的是旧代际，写入与清理会被数据层拒绝。
        """
        old = self.background
        if old.running:
            logger.warning("重新开放后台执行器时仍有 %d 个上一代任务在运行，其写入将按代际被拒绝", old.running)
        self.background = BackgroundExecutor()

    @staticmethod
    def _pick_image(image_segs: List[Dict[str, Any]]) -> Tuple[str, str, int]:
        """取第一张带 binary_data_base64 的图片段，并统计有效图片总数。

        返回 (b64_data, url_hint, valid_count)：valid_count 为带 binary_data_base64 的
        图片段数量，供调用方在多图时提示"仅保存第一张"。

        napcat 适配器的 image/emoji 段 data 字段在下载后被清空（恒为 ""，见适配器
        _build_image_like_segment），故 url_hint 在 napcat 下实际取不到后缀，扩展名
        由 ImageStore.guess_extension 的二进制 magic number 兜底；保留 url_hint 仅为
        兼容「会在 data 里给出 url」的其他适配器。
        """
        first_b64 = ""
        first_hint = ""
        valid_count = 0
        for seg in image_segs:
            candidate = seg.get("binary_data_base64")
            if not (isinstance(candidate, str) and candidate):
                continue
            valid_count += 1
            if first_b64:
                continue  # 已锁定第一张，后续仅继续计数
            first_b64 = candidate
            data_field = seg.get("data")
            if isinstance(data_field, str):
                first_hint = data_field
            elif isinstance(data_field, dict):
                for key in ("url", "file", "path", "summary"):
                    value = data_field.get(key)
                    if isinstance(value, str) and value:
                        first_hint = value
                        break
        return first_b64, first_hint, valid_count

    async def _submit_or_notify(
        self, factory: CoroFactory, *, name: str, stream_id: str, generation: int,
    ) -> None:
        """按入口代际提交工作；过期、退役或满载时只发送有界短回执。"""
        data_manager = self._plugin._data_manager
        if generation != data_manager.generation:
            reason = "stale"
        elif data_manager.is_retired:
            reason = "closed"
        else:
            # 校验与提交之间没有 await，旧请求不能借回滚后新建的执行器重新获得写权限。
            reason = self.background.submit(factory, name=name)
        if reason is None:
            return
        logger.warning(
            "后台执行器拒收(%s, running=%d, queued=%d): %s",
            reason, self.background.running, self.background.queued, name,
        )
        await self._plugin._service.notify_busy(
            stream_id, _RELOADING_TEXT if reason in ("closed", "stale") else _BUSY_TEXT,
        )

    async def dispatch(
        self, message: Optional[dict], hook_kwargs: Optional[Dict[str, Any]] = None,
        *, generation: int,
    ) -> Optional[Dict[str, Any]]:
        """动态触发命令的 hook 入口逻辑。

        返回值：
        - ``{"action": "abort"}``：已接管（动态命中 / 带图添加），拦截后续主链。
        - ``{"action": "continue", "modified_kwargs": {...}}``：正文是内置命令但 Host 的
          processed_plain_text 被引用/@/空白污染，改写后交由 Host 正常匹配。
        - ``None``：放行。任何"不该由本插件处理"的消息（非群/私聊文本、不带前缀、
          未注册 trigger、正文为空、添加命令组件已被禁用的带图添加）都返回 None。

        ``hook_kwargs`` 为 handler 收到的除 message 外的其它 hook 参数，改参时原样带回，
        因为 Host 会用 modified_kwargs **整体替换**后续处理器与主链看到的 kwargs。
        ``generation`` 必须由入口在任何 await 之前捕获，查询返回后不得重新读取当前代际。
        """
        if message is None or not isinstance(message, dict):
            return None

        p = self._plugin
        try:
            prefix = p.config.settings.command_prefix
        except Exception:
            return None
        if not prefix:
            return None

        # 正文只认 text 段；没有 text 段就没有正文，引用/转发内容不是命令输入。
        clean_text, image_segs = extract_text_and_images(message.get("raw_message"))
        base_text = clean_text.strip()
        if not base_text:
            return None

        # 带图添加：<前缀>问：触发词答：[图片]。必须先于动态触发执行——带图添加文本的
        # trigger 段以「问：」开头会被 is_reserved_trigger 判为保留词，而 @Command
        # handle_add 的 pattern 要求「答：」后有 response、带图时「答：」后为空故不匹配，
        # 若不在此抢先截获，这条消息会漏过命令路径直达 LLM。另外，Host 对 chat.receive.* hook
        # 的 message 默认携带二进制数据（hook_payloads.serialize_session_message 固定按
        # include_binary_data=True 序列化），raw_message 的 image/emoji 段才有 binary_data_base64；
        # 而 @Command 路径的 message 被显式以 include_binary_data=False 序列化、拿不到图，
        # 故带图添加只能在本 hook 完成。
        image_add_result = await self._try_image_add(
            message, prefix, base_text, image_segs, generation=generation,
        )
        if image_add_result is not None:
            return image_add_result

        if not base_text.startswith(prefix):
            return None
        trigger = base_text[len(prefix):].strip()
        if not trigger:
            return None

        # 内置命令交给精确 pattern 的 @Command 处理；若 Host 将要用来匹配的文本与正文不一致
        # （被引用原文/@昵称/首尾空白污染），改写后 continue，让 Host 按干净正文命中正确的组件。
        if is_reserved_trigger(trigger):
            return self._rewrite_for_builtin(message, base_text, hook_kwargs)

        route = build_message_route(message)
        if not route.stream_id:
            return None

        # 纯内存查询：未注册直接放行；命中则先 abort，投递放后台，避免撞 Hook 超时。
        response_value = p._service.lookup(trigger, route.group_id, route.user_id)
        if response_value is None:
            return None

        await self._submit_or_notify(
            lambda: self._deliver_in_background(generation, trigger, response_value, route),
            name=f"custom_commands.deliver:{trigger}", stream_id=route.stream_id, generation=generation,
        )
        return {"action": "abort"}

    def _rewrite_for_builtin(
        self, message: Dict[str, Any], base_text: str, hook_kwargs: Optional[Dict[str, Any]],
    ) -> Optional[Dict[str, Any]]:
        """正文是内置命令且与 processed_plain_text 不完全一致时，返回改写 message 的 continue 结果。

        比较不 strip：Host 用未去空白的 processed_plain_text 跑 ``^...$`` 正则，`" .列表"` 也会失配，
        同样需要改写成 `.列表`。
        """
        if self._plugin.match_builtin(base_text) is None:
            return None  # 以保留词开头但不构成完整内置命令（如只有「.问：」），不干预
        processed = str(message.get("processed_plain_text") or "")
        if processed == base_text:
            return None
        modified = dict(message)
        modified["processed_plain_text"] = base_text
        logger.info("内置命令正文 %r 与 Host 匹配文本 %r 不一致（引用/@/空白污染），已改写", base_text[:60], processed[:60])
        new_kwargs: Dict[str, Any] = {
            key: value for key, value in (hook_kwargs or {}).items() if key not in ("message", "hook_name")
        }
        new_kwargs["message"] = modified
        return {"action": "continue", "modified_kwargs": new_kwargs}

    async def _deliver_in_background(
        self, generation: int, trigger: str, response_value: str, route: MessageRoute,
    ) -> None:
        """后台投递：先保证会话存在（冷会话兜底），再发送。只发送不写数据，超时可安全取消。"""
        service = self._plugin._service

        async def _deliver() -> None:
            stream_id = await service.ensure_session(route)
            await service.deliver(trigger, response_value, stream_id)

        with self._plugin._data_manager.bind_generation(generation):
            try:
                await asyncio.wait_for(_deliver(), timeout=_DELIVER_TIMEOUT_SECONDS)
            except asyncio.TimeoutError:
                logger.error("动态命令 '%s' 投递超过 %.0fs 未完成，已取消", trigger, _DELIVER_TIMEOUT_SECONDS)
            except Exception as exc:
                logger.error("动态命令 '%s' 投递任务异常: %s", trigger, exc, exc_info=True)

    async def _try_image_add(
        self, message: Dict[str, Any], prefix: str,
        base_text: str, image_segs: List[Dict[str, Any]],
        *, generation: int,
    ) -> Optional[Dict[str, Any]]:
        """尝试把"<前缀>问：触发词答：[图片]"这条消息作为"带图添加"处理。

        ``base_text`` 与 ``image_segs`` 由 dispatch 从 raw_message 统一重组后传入。
        图片排在文字前面或后面都可以——base_text 取自 raw_message 文本段重组，
        不受图文顺序影响。

        返回值语义：
        - ``None``                —— 不是带图添加场景（无图 / 文本不符合添加格式），或
                                     ``custom_command_add`` 组件已被全局禁用（与 Host 对禁用
                                     Command 的处理一致：不命中、交给后续主链），调用方继续
                                     走 reserved 判断与动态触发。
        - ``{"action": "abort"}`` —— 已确认是带图添加：权限/格式校验、图片解码/落盘/绑定与
                                     回执交给后台任务（拒收时发短回执），本方法立即拦截主链。
        """
        if not image_segs:
            return None  # 没有图片 → 不是带图添加，放行

        # 前缀校验由正则 ^<prefix>问： 承担；base_text 已不含占位符干扰。
        match = re.match(rf"^{re.escape(prefix)}{re.escape(KW_ADD)}(?P<trigger>.+?){re.escape(KW_ADD_ANSWER)}", base_text)
        if not match:
            return None  # 有图但文本不符合添加格式 → 放行（带图触发或普通图片消息）

        enabled = await self._plugin._service.is_add_command_enabled()
        route = build_message_route(message)
        data_manager = self._plugin._data_manager
        if generation != data_manager.generation or data_manager.is_retired:
            await self._plugin._service.notify_busy(route.stream_id, _RELOADING_TEXT)
            return {"action": "abort"}
        if enabled is None:
            # 状态未知不是明确禁用；必须拦截，避免带图正文又被 Host 当作文本添加执行。
            await self._plugin._service.notify_busy(route.stream_id, _ADD_STATUS_UNAVAILABLE_TEXT)
            return {"action": "abort"}
        if not enabled:
            logger.info("custom_command_add 组件已禁用，带图添加消息放行")
            return None

        # —— 确认是"带图添加"，此后无论成败都回发消息并 abort ——
        if not route.stream_id:
            # 意图已确认是带图添加：虽无可回复会话（无法回执），也不能放行——否则这条
            # 「问：x答：+图片」会漏进 LLM 主链被 AI 二次回复。记日志后直接拦截。
            logger.warning("带图添加消息缺少 session_id，无法回执；已拦截以免漏入 LLM 主链")
            return {"action": "abort"}

        trigger = match.group("trigger").strip()
        trailing_text = base_text[match.end():].strip()
        # 仅支持单张图片：取第一张带 binary_data_base64 的图片段，并拿到有效图片总数；
        # 多于一张时由 add_image 在成功回执里提示"仅保存第一张"，避免用户误以为多张都已绑定。
        b64_data, url_hint, valid_count = self._pick_image(image_segs)

        await self._submit_or_notify(
            lambda: self._image_add_in_background(
                generation, route, trigger, trailing_text, b64_data, url_hint, valid_count,
            ),
            name=f"custom_commands.image_add:{trigger}", stream_id=route.stream_id, generation=generation,
        )
        return {"action": "abort"}

    async def _image_add_in_background(
        self, generation: int, route: MessageRoute, trigger: str, trailing_text: str,
        b64_data: str, url_hint: str, valid_count: int,
    ) -> None:
        """带图添加的后台部分：建流兜底 → 权限/格式校验 → 解码落盘绑定 → 回执。写数据，不取消。"""
        service = self._plugin._service
        started = time.monotonic()
        with self._plugin._data_manager.bind_generation(generation):
            try:
                stream_id = await service.ensure_session(route)

                # 先做与文本添加路径一致的管理员校验：无论「答：」后是否多填文字，非管理员都应先
                # 收到统一的「无权限」提示，而不是先撞上格式约束（既与文本添加路径行为不一致，
                # 又把内部格式细节暴露给无权限用户）。add_image 内仍会再校验一次权限，冗余但无害。
                if not self._plugin._check_admin(route.user_id):
                    await service._send_text("❌ 你没有权限执行此管理员命令", stream_id, context="无权限提示")
                    return

                if trailing_text:
                    await service._send_text(
                        "❌ 带图添加时「答：」后请不要再填写文字；如需文本回复请不要附带图片",
                        stream_id,
                        context="带图添加格式冲突提示",
                    )
                    return

                # 权限 / 触发词 / 图片字节的全部校验与回执都在 service.add_image 内完成
                await service.add_image(
                    trigger, b64_data, url_hint, stream_id, route.group_id, route.user_id,
                    ignored_image_count=max(0, valid_count - 1),
                )
            except Exception as exc:
                logger.error("带图添加后台任务异常(trigger=%s): %s", trigger, exc, exc_info=True)
            finally:
                elapsed = time.monotonic() - started
                if elapsed > _IMAGE_ADD_SLOW_WARN_SECONDS:
                    logger.warning("带图添加(trigger=%s)耗时 %.1fs，请检查存储或网络状况", trigger, elapsed)
