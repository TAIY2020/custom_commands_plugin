"""命令业务层：增删查与动态应答的统一编排（写命令的唯一入口）。

``CommandService`` 执行式——持 plugin 弱引用，直接 ``ctx.send`` 发回执，返回
``(ok, log, intercept)`` 三元组供 @Command handler 透传。编排 scope/storage/images/forward
四个能力模块；admin 校验与作用域解析在此统一，消除文本添加(add_text)与带图添加
(add_image)两条写路径在编排层面的重复。

⚠️ 三元组第三项是 Host 的 ``intercept_message_level``（不是"成本/优先级"，勿照字面误解）：
Host 按 ``bool()`` 解释它——见主程序 ``chat/message_receive/bot.py``::
``continue_process = not bool(intercept_message_level)``。本插件内置命令处理完即应拦截后续主链
（命令已自行回执，不该再走 LLM/Maisaka 回复），故所有返回恒取 ``1``（拦截）。若改成 ``0``，
命令执行后消息会继续落入 LLM 被"二次 AI 回复"。
"""

from __future__ import annotations

import asyncio
import base64
import logging
import re
import time
from pathlib import Path
from typing import TYPE_CHECKING, Dict, Optional, Tuple

from .common import (
    ADAPTER_IMAGE_FALLBACK_TEXTS,
    MessageRoute,
    build_list_header_text,
    build_scope_desc,
    is_persistable_text,
    is_reserved_trigger,
    looks_like_image_response,
    resolve_scope_id,
)
from .storage import CommandQuotaError, DataProtectionError, DataSaveError, StaleResourceError

if TYPE_CHECKING:
    from ..plugin import CustomCommandsPlugin

logger = logging.getLogger(__name__)

# ensure_session 映射的有效期（秒）：超过后重新经 open_session 解析，避免路由变更后永久沿用旧结果。
_SESSION_MAP_TTL_SECONDS = 1800.0
# 「繁忙/重载中」快速回执的发送超时（毫秒）：这条回执在 blocking hook 内发出，必须有界。
_BUSY_NOTICE_TIMEOUT_MS = 3000
# 组件查询同时限制整个调用与底层 RPC 响应等待，不复用之前的启用状态。
_ADD_ENABLED_QUERY_TIMEOUT_MS = 3000
_ADD_COMMAND_COMPONENT = "custom_command_add"


class CommandService:
    """自定义命令的增删查与动态应答业务。"""

    def __init__(self, plugin: "CustomCommandsPlugin") -> None:
        self._plugin = plugin
        # 入站 stream_id → (Host 实际会话 ID, 记录时刻)。hook 路径先于 Host 建流，冷会话下
        # ctx.send.* 会因「未找到聊天流」失败；每个 stream 经 open_session 解析一次并缓存映射。
        # 失效条件：TTL 到期，或经该会话发送返回 False（note_send_failure）。
        self._session_map: Dict[str, Tuple[str, float]] = {}

    async def notify_busy(self, stream_id: str, text: str) -> None:
        """有界的快速回执：执行器拒收或无法确认组件状态时告知用户，失败只记日志。

        在 blocking hook 内调用，故必须带发送超时，不能让 hook 等待一次可能卡住的发送。
        不经 ensure_session（冷会话下本条回执可能发不出去，属可接受的最坏情况）。
        """
        if not stream_id:
            return
        try:
            # RPC 自身的响应超时不包含 IPC 发送等待，外层期限覆盖整个 await。
            ok = await asyncio.wait_for(
                self._plugin.ctx.send.text(text, stream_id, timeout_ms=_BUSY_NOTICE_TIMEOUT_MS),
                timeout=_BUSY_NOTICE_TIMEOUT_MS / 1000.0,
            )
        except asyncio.TimeoutError:
            logger.warning("繁忙回执等待超过 %dms(stream=%s)，不重试以免重复发送", _BUSY_NOTICE_TIMEOUT_MS, stream_id)
            return
        except Exception as exc:
            logger.warning("繁忙回执发送异常(stream=%s): %s", stream_id, exc)
            return
        if ok is False:
            logger.warning("繁忙回执发送失败(stream=%s)：send.text 返回 False", stream_id)
            self.note_send_failure(stream_id)

    async def is_add_command_enabled(self) -> Optional[bool]:
        """``custom_command_add`` 组件当前是否全局启用（Host 组件管理开关）。

        带图添加走 hook 路径、不经 Host 的 Command 分发，不会被组件启停自动约束；这里主动
        为每次请求查询全局状态，避免旧的启用缓存让禁用失效。会话级禁用与命令前置 Hook
        不包含在此接口中，仍不由这次查询覆盖。查询失败、组件缺失或状态非法时返回 None，
        调用方应消费本次带图添加并提示重试，不能放行后又落入文本添加路径。
        """
        try:
            plugin_id = self._plugin.ctx.plugin_id
            plugins = await asyncio.wait_for(
                self._plugin.ctx.call_capability(
                    "component.get_all_plugins", timeout_ms=_ADD_ENABLED_QUERY_TIMEOUT_MS,
                ),
                timeout=_ADD_ENABLED_QUERY_TIMEOUT_MS / 1000.0,
            )
        except asyncio.TimeoutError:
            logger.warning("查询添加命令组件超过 %dms，本次带图添加不放行", _ADD_ENABLED_QUERY_TIMEOUT_MS)
            return None
        except Exception as exc:
            logger.warning("查询添加命令组件启用状态失败: %s；本次带图添加不放行", exc)
            return None

        plugin_info = plugins.get(plugin_id) if isinstance(plugins, dict) else None
        components = plugin_info.get("components") if isinstance(plugin_info, dict) else None
        if isinstance(components, list):
            for component in components:
                if (
                    isinstance(component, dict)
                    and component.get("name") == _ADD_COMMAND_COMPONENT
                    and str(component.get("type") or "").upper() == "COMMAND"
                ):
                    enabled = component.get("enabled")
                    if isinstance(enabled, bool):
                        return enabled
                    break
        logger.warning("添加命令组件缺失或启用状态非法，本次带图添加不放行")
        return None

    async def ensure_session(self, route: MessageRoute) -> str:
        """保证 ``route`` 指向的会话在 Host 侧存在，返回应当用于发送的 stream_id。

        ``chat.receive.after_process`` hook 触发于 Host ``register_message``/``get_or_create_session``
        之前（bot.py 主链），从未建过会话的聊天在 hook 内发送必失败且 abort 后会话也不会被创建。
        这里按入站消息自带的 platform/account_id/scope 调 ``chat.open_session`` 兜底建流，
        保留多账号/多连接路由；返回 Host 给出的实际会话 ID（可能与入站 ID 不同，映射被缓存），
        失败时退回原 stream_id 交由发送路径自行报错。
        """
        stream_id = route.stream_id
        if not stream_id:
            return stream_id
        now = time.monotonic()
        cached = self._session_map.get(stream_id)
        if cached is not None and (now - cached[1]) < _SESSION_MAP_TTL_SECONDS:
            return cached[0]
        try:
            result = await self._plugin.ctx.chat.open_session(
                platform=route.platform or "qq",
                chat_type=route.chat_type,
                user_id=route.user_id,
                group_id=route.group_id,
                account_id=route.account_id,
                scope=route.scope,
            )
        except Exception as exc:
            logger.warning("确保会话存在失败(stream=%s): %s；将直接按原 stream_id 发送", stream_id, exc)
            return stream_id
        if isinstance(result, dict) and result.get("success"):
            opened = str(result.get("session_id") or result.get("stream_id") or "").strip() or stream_id
            if opened != stream_id:
                logger.info("会话 %s 经 open_session 解析为 %s", stream_id, opened)
            self._session_map[stream_id] = (opened, now)
            return opened
        error = result.get("error") if isinstance(result, dict) else result
        logger.warning("open_session 未成功(stream=%s): %s；将直接按原 stream_id 发送", stream_id, error)
        return stream_id

    def note_send_failure(self, stream_id: str) -> None:
        """经某会话发送返回 False 时调用：丢弃指向它的映射，下次重新解析。"""
        if not stream_id:
            return
        stale = [key for key, (actual, _) in self._session_map.items() if key == stream_id or actual == stream_id]
        for key in stale:
            self._session_map.pop(key, None)

    def _resolve_scope(self, group_id: str, user_id: str) -> Tuple[str, str]:
        """两路共用的作用域解析：返回 (原始 scope_id, 映射后的数据分区名)。"""
        scope_id = resolve_scope_id(group_id, user_id)
        return scope_id, self._plugin._scope_resolver.resolve(scope_id)

    async def _send_text(self, text: str, stream_id: str, *, context: str = "回执") -> bool:
        """发送文本并统一记录失败；调用方仍按原业务语义返回。"""
        try:
            send_ok = await self._plugin.ctx.send.text(text, stream_id)
        except Exception as exc:
            logger.warning("%s发送异常: %s", context, exc, exc_info=True)
            return False
        if send_ok is False:
            logger.warning("%s发送失败：send.text 返回 False（可能被风控或连接异常）", context)
            self.note_send_failure(stream_id)
            return False
        return True

    @staticmethod
    def _save_failure_text(exc: OSError, verb: str) -> str:
        """保存失败回执：编码类失败给出具体原因，其余按磁盘异常提示。"""
        if isinstance(exc, DataSaveError):
            return f"❌ 命令{verb}失败（{exc}），未生效"
        return f"❌ 命令{verb}失败（磁盘写入异常），未生效，请稍后重试"

    # ===== 添加 =====

    async def add_text(self, matched_groups: Optional[dict], stream_id: str,
                       group_id: str, user_id: str) -> Tuple[bool, str, int]:
        """添加文本命令：<前缀>问：触发词答：回复内容。"""
        p = self._plugin
        # 能进入命令路径即说明含前缀的 pattern 已匹配，无需再校验前缀；matched_groups 异常时兜底。
        if not matched_groups:
            return False, "缺少匹配参数", 1

        if not p._check_admin(user_id):
            await self._send_text("❌ 你没有权限执行此管理员命令", stream_id, context="无权限提示")
            return False, f"用户 {user_id} 无权限", 1

        trigger = matched_groups.get("trigger", "").strip()
        response = matched_groups.get("response", "").strip()

        if not trigger or not response:
            prefix = p.config.settings.command_prefix
            await self._send_text(
                f"❌ 命令格式错误，请使用：{prefix}问：触发词答：回复内容", stream_id,
                context="格式错误提示",
            )
            return False, "格式错误", 1

        # 图片下载失败拦截：带图添加时若适配器下载图片失败，image/emoji 段会降级成
        # "[image]"/"[emoji]" 文本，绕过带图逻辑落到这条纯文本添加路径。此时 response 恰为
        # 占位符本身，说明用户本意是发图而非发这段字面文本，拦截并提示重试，避免存成幽灵命令。
        if response in ADAPTER_IMAGE_FALLBACK_TEXTS:
            await self._send_text(
                "❌ 图片获取失败（可能下载超时或被风控），请重新发送「添加命令 + 图片」", stream_id,
                context="图片获取失败提示",
            )
            return False, "图片下载失败占位符", 1

        # 输入长度校验
        if len(trigger) > p.config.settings.max_trigger_length:
            await self._send_text(
                f"❌ 触发词过长（最多 {p.config.settings.max_trigger_length} 字符）", stream_id,
                context="触发词过长提示",
            )
            return False, "触发词过长", 1
        if len(response) > p.config.settings.max_response_length:
            await self._send_text(
                f"❌ 回复内容过长（最多 {p.config.settings.max_response_length} 字符）", stream_id,
                context="回复内容过长提示",
            )
            return False, "回复内容过长", 1
        # 孤立代理项等无法按 UTF-8 落盘的字符：写盘时会抛编码错误，这里先拦下给出明确原因。
        if not is_persistable_text(trigger) or not is_persistable_text(response):
            await self._send_text("❌ 触发词或回复内容包含无法保存的字符", stream_id, context="不可落盘字符提示")
            return False, "包含无法保存的字符", 1

        # 触发词命中 hook 的 reserved 列表时，写入会变成幽灵数据：
        # hook 见到 `.<reserved>` 直接 return None 让给 @Command，动态分发永不触达。
        if is_reserved_trigger(trigger):
            await self._send_text(
                f"❌ 触发词「{trigger}」与内置命令冲突，请换一个", stream_id,
                context="保留词冲突提示",
            )
            return False, "触发词为保留词", 1

        # 图片路径安全校验：在写入前拒绝含路径穿越的回复内容；
        # 路径合法但文件尚不存在时不阻止添加（允许"先建命令、后放图"），仅在回执里追加提醒，
        # 避免用户拼错文件名后、直到触发命令时才发现"找不到图片"。
        missing_image_hint = ""
        validated_image_directory: Optional[Path] = None
        if looks_like_image_response(response):
            validated_image_directory = p._images.resolve_dir()
            image_path = p._images.safe_path(response, allow_links=False)
            if image_path is None:
                logger.warning("拒绝越界或链接图片路径: '%s'", response)
                await self._send_text(
                    "❌ 图片路径不合法，不允许越界或引用符号链接、目录链接，请使用普通图片文件",
                    stream_id, context="路径非法提示",
                )
                return False, "不安全的图片路径", 1
            if not image_path.exists():
                missing_image_hint = (
                    f"\n⚠️ 注意：图片「{response}」当前不在图片目录中，"
                    "请将其放入图片目录后再触发，否则会提示找不到图片。"
                )

        scope_id, scope_used = self._resolve_scope(group_id, user_id)

        try:
            orphan = await p._data_manager.add(
                trigger, response, scope_used,
                max_per_scope=p.config.settings.max_commands_per_scope,
                precondition=(
                    (lambda: p._images.resolve_dir() == validated_image_directory)
                    if validated_image_directory is not None else None
                ),
            )
        except StaleResourceError as exc:
            await self._send_text(f"❌ {exc}", stream_id, context="图片目录变更提示")
            return False, "图片目录已变更", 1
        except CommandQuotaError as exc:
            await self._send_text(f"❌ {exc}", stream_id, context="命令数量超限提示")
            return False, "命令数量超限", 1
        except DataProtectionError as exc:
            logger.warning("拒绝添加命令: %s", exc)
            await self._send_text(f"❌ {exc}", stream_id, context="数据保护提示")
            return False, "数据保护模式", 1
        except OSError as exc:
            logger.error("保存命令数据失败: %s", exc, exc_info=True)
            await self._send_text(self._save_failure_text(exc, "保存"), stream_id, context="保存失败提示")
            return False, "保存失败", 1

        # 覆盖旧命令导致旧图片文件失去全部引用时顺手回收（仅清理插件自动生成的 cc_ 文件）
        if orphan:
            await p._images.cleanup_orphan_locked(orphan, p._data_manager)

        scope_desc = build_scope_desc(scope_id, scope_used)

        await self._send_text(
            f"✅ 成功添加自定义命令{scope_desc}！\n触发词：{trigger}\n回复内容：{response}{missing_image_hint}",
            stream_id,
            context="添加命令成功提示",
        )
        logger.info("用户 '%s' 在作用域 '%s' 添加命令: '%s'", user_id, scope_used, trigger)
        return True, "添加成功", 1

    async def add_image(self, trigger: str, b64_data: str, url_hint: str,
                        stream_id: str, group_id: str, user_id: str,
                        ignored_image_count: int = 0) -> Tuple[bool, str, int]:
        """添加图片命令：把消息内图片落盘并绑定触发词。

        ``trigger`` / ``b64_data`` / ``url_hint`` 由 DynamicDispatcher 从入站消息解析后传入。
        触发词校验与 add_text 同源（长度 + 保留词）；图片字节的解码/空判定/大小校验在此完成。
        触发词已存在时直接覆盖（与 add_text 语义一致）。``ignored_image_count`` 为同条消息中
        被忽略的额外图片数（仅取第一张），>0 时在成功回执里提示用户。
        """
        p = self._plugin
        if not p._check_admin(user_id):
            await self._send_text("❌ 你没有权限执行此管理员命令", stream_id, context="无权限提示")
            return False, f"用户 {user_id} 无权限", 1

        if not trigger:
            await self._send_text("❌ 触发词不能为空", stream_id, context="触发词为空提示")
            return False, "触发词为空", 1
        if len(trigger) > p.config.settings.max_trigger_length:
            await self._send_text(
                f"❌ 触发词过长（最多 {p.config.settings.max_trigger_length} 字符）", stream_id,
                context="触发词过长提示",
            )
            return False, "触发词过长", 1
        if is_reserved_trigger(trigger):
            await self._send_text(
                f"❌ 触发词「{trigger}」与内置命令冲突，请换一个", stream_id,
                context="保留词冲突提示",
            )
            return False, "触发词为保留词", 1
        if not is_persistable_text(trigger):
            await self._send_text("❌ 触发词包含无法保存的字符", stream_id, context="不可落盘字符提示")
            return False, "包含无法保存的字符", 1

        if not b64_data:
            await self._send_text(
                "❌ 没能获取到图片数据，请重试，或改用「文件名」方式添加", stream_id,
                context="无图片数据提示",
            )
            return False, "无图片数据", 1

        max_size = p.config.settings.max_image_size
        # 去除全部空白（含内部换行）而非仅首尾：validate=True 会拒绝任何非 base64 字母表字符。
        # 当前 napcat 给的是无换行标准 base64，但若换用按 76 字符折行（MIME 风格）的来源，仅
        # strip 首尾会让中间换行触发解码失败；统一清空白做前向加固，不改变现有 napcat 行为。
        normalized_b64 = re.sub(r"\s", "", b64_data)
        # 解码前按 base64 长度粗筛超大图，避免对明显超限的大字符串做无谓的 b64decode（省内存/CPU）；
        # 真正的精确校验由下方解码后的 len(image_bytes) > max_size 兜底。
        # base64 每 4 字符编码 3 字节 → 解码后字节数 ≈ len(b64) * 3 // 4，此估算比真实值最多高估 2
        # （末尾 1~2 个 "=" padding）；+2 即补偿该高估，确保只在确定超限时才拒绝、不误杀临界图片。
        if (len(normalized_b64) * 3) // 4 > max_size + 2:
            limit_mb = max_size / (1024 * 1024)
            await self._send_text(
                f"❌ 图片文件过大（上限 {limit_mb:.0f}MB）", stream_id,
                context="图片过大提示",
            )
            return False, "图片过大", 1

        try:
            image_bytes = base64.b64decode(normalized_b64, validate=True)
        except Exception:
            await self._send_text("❌ 图片数据解码失败", stream_id, context="图片解码失败提示")
            return False, "图片解码失败", 1
        if not image_bytes:
            await self._send_text("❌ 图片数据为空，请重试", stream_id, context="图片数据为空提示")
            return False, "图片数据为空", 1

        if len(image_bytes) > max_size:
            size_mb = len(image_bytes) / (1024 * 1024)
            limit_mb = max_size / (1024 * 1024)
            await self._send_text(
                f"❌ 图片文件过大（{size_mb:.1f}MB，上限 {limit_mb:.0f}MB）", stream_id,
                context="图片过大提示",
            )
            return False, "图片过大", 1

        # 带图添加的字节必须是真实图片：未匹配已知魔数则拒绝，避免非图片内容被
        # guess_extension 兜底存成 .png 后触发时发送失败或行为不可预期。
        if not p._images.has_image_magic(image_bytes):
            await self._send_text(
                "❌ 附带的内容不是受支持的图片格式（仅支持 PNG/JPEG/GIF/WebP），未保存",
                stream_id,
                context="非图片格式提示",
            )
            return False, "非图片格式", 1

        # 落盘前先看当前是否可写（保护模式 / 实例退役），免得白存一张图再回滚。
        # add() 内仍会再查一次；两次检查之间若状态变化，下方 DataProtectionError 分支负责回收图片。
        try:
            p._data_manager.ensure_writable()
        except DataProtectionError as exc:
            await self._send_text(f"❌ {exc}", stream_id, context="数据保护提示")
            return False, "数据保护模式", 1

        scope_id, scope_used = self._resolve_scope(group_id, user_id)
        filename = p._images.managed_filename_for(image_bytes, url_hint)
        orphan: Optional[str] = None
        async with p._images.managed_file_lock(filename):
            # 落盘并核对目录版本：保存期间若配置热更新了 image_directory，文件会留在旧目录，
            # 绑定后按新目录找不到。核对不一致就按新目录再存一次（最多两轮）；仍不一致则拒绝提交。
            image_dir_used: Optional[Path] = None
            try:
                for _attempt in range(2):
                    filename, image_dir_used = await p._images.store_prepared(image_bytes, filename)
                    if image_dir_used == p._images.resolve_dir():
                        break
                    logger.warning("图片保存期间 image_directory 已变更（%s），按新目录重存", image_dir_used)
            except Exception as exc:
                logger.error("保存带图命令的图片失败: %s", exc, exc_info=True)
                await self._send_text("❌ 保存图片时发生内部错误", stream_id, context="保存图片失败提示")
                return False, "保存图片失败", 1

            try:
                orphan = await p._data_manager.add(
                    trigger, filename, scope_used,
                    max_per_scope=p.config.settings.max_commands_per_scope,
                    # 写锁内再核对一次目录版本，把目录一致性纳入提交事务
                    precondition=lambda: p._images.resolve_dir() == image_dir_used,
                )
            except StaleResourceError as exc:
                # 文件在旧目录、按当前目录清理也找不到它；留在旧目录待人工/迁移处理，记路径。
                logger.warning("拒绝绑定图片命令: %s；图片仍在 %s", exc, image_dir_used)
                await self._send_text(f"❌ {exc}", stream_id, context="目录变更提示")
                return False, "图片目录已变更", 1
            except DataProtectionError as exc:
                # 保护模式/退役/代际过期：命令未写入。回收由 cleanup_if_unreferenced 自行判断——
                # 退役/过期时它会拒绝删除（本实例的引用快照可能已过期，新实例可能刚引用了同一文件）。
                await p._images.cleanup_orphan_locked(filename, p._data_manager, file_lock_held=True)
                logger.warning("拒绝添加图片命令: %s", exc)
                await self._send_text(f"❌ {exc}", stream_id, context="数据保护提示")
                return False, "数据保护模式", 1
            except CommandQuotaError as exc:
                # 命令数超限：本次已落盘的图未能写入任何命令，若不被现有命令引用则回收，避免孤儿堆积。
                # 同 hash 图可能已被其它触发词引用，故须判断引用计数；这里仍持有文件级锁，避免清理
                # 与另一个同 hash 图片的保存/绑定交错。
                await p._images.cleanup_orphan_locked(filename, p._data_manager, file_lock_held=True)
                await self._send_text(f"❌ {exc}", stream_id, context="命令数量超限提示")
                return False, "命令数量超限", 1
            except OSError as exc:
                # 保存失败：add 已回滚内存（命令未写入），本次落盘图成孤儿，按超限同样回收；
                # 仍持文件级锁，回收与并发同 hash 保存/绑定串行化。
                await p._images.cleanup_orphan_locked(filename, p._data_manager, file_lock_held=True)
                logger.error("保存图片命令数据失败: %s", exc, exc_info=True)
                await self._send_text(self._save_failure_text(exc, "保存"), stream_id, context="保存失败提示")
                return False, "保存失败", 1

        # 覆盖旧图片命令时回收失去引用的旧图；同图 hash 相同则 old==new，不会误删本次刚存的图
        if orphan:
            await p._images.cleanup_orphan_locked(orphan, p._data_manager)

        scope_desc = build_scope_desc(scope_id, scope_used)

        cmd_prefix = p.config.settings.command_prefix
        # 带图添加仅取第一张：用户同条消息附带多张有效图片时，在成功回执里明确提示，
        # 避免误以为多张都已绑定（ignored_image_count 由 DynamicDispatcher 统计后传入）。
        multi_image_hint = (
            f"\n⚠️ 检测到 {ignored_image_count + 1} 张图片，仅保存了第一张"
            if ignored_image_count > 0 else ""
        )
        await self._send_text(
            f"✅ 成功添加图片命令{scope_desc}！\n"
            f"触发词：{trigger}\n"
            f"发送 {cmd_prefix}{trigger} 即可获取这张图片{multi_image_hint}",
            stream_id,
            context="添加图片命令成功提示",
        )
        logger.info(
            "用户 '%s' 在作用域 '%s' 通过消息内图片添加命令: '%s' -> %s",
            user_id, scope_used, trigger, filename,
        )
        return True, "添加成功", 1

    # ===== 删除 =====

    async def delete(self, matched_groups: Optional[dict], stream_id: str,
                     group_id: str, user_id: str) -> Tuple[bool, str, int]:
        """删除命令：<前缀>删：触发词。"""
        p = self._plugin
        if not matched_groups:
            return False, "缺少匹配参数", 1

        if not p._check_admin(user_id):
            await self._send_text("❌ 你没有权限执行此管理员命令", stream_id, context="无权限提示")
            return False, f"用户 {user_id} 无权限", 1

        trigger = matched_groups.get("trigger", "").strip()
        _, current_scope = self._resolve_scope(group_id, user_id)
        try:
            success, orphan = await p._data_manager.delete(trigger, current_scope)
        except DataProtectionError as exc:
            logger.warning("拒绝删除命令: %s", exc)
            await self._send_text(f"❌ {exc}", stream_id, context="数据保护提示")
            return False, "数据保护模式", 1
        except OSError as exc:
            logger.error("保存命令数据失败: %s", exc, exc_info=True)
            await self._send_text(self._save_failure_text(exc, "删除"), stream_id, context="保存失败提示")
            return False, "保存失败", 1

        if success:
            if orphan:
                await p._images.cleanup_orphan_locked(orphan, p._data_manager)
            await self._send_text(
                f"✅ 成功删除了自定义命令（作用域: {current_scope}）：'{trigger}'",
                stream_id,
                context="删除命令成功提示",
            )
            return True, "删除成功", 1

        msg = f"❌ 未在当前作用域 [{current_scope}] 找到命令：'{trigger}'"

        if (
            current_scope != "global"
            and p._data_manager.has_global(trigger)
        ):
            prefix = p.config.settings.command_prefix
            msg += f"\n💡 提示：这是一个【全局命令】。可使用 {prefix}删全局：{trigger} 来删除。"

        await self._send_text(msg, stream_id, context="命令未找到提示")
        return False, "命令未找到", 1

    async def delete_global(self, matched_groups: Optional[dict], stream_id: str,
                            user_id: str) -> Tuple[bool, str, int]:
        """删除全局命令：<前缀>删全局：触发词。"""
        p = self._plugin
        if not matched_groups:
            return False, "缺少匹配参数", 1

        if not p._check_admin(user_id):
            await self._send_text("❌ 你没有权限执行此管理员命令", stream_id, context="无权限提示")
            return False, f"用户 {user_id} 无权限", 1

        trigger = matched_groups.get("trigger", "").strip()
        try:
            success, orphan = await p._data_manager.delete_global(trigger)
        except DataProtectionError as exc:
            logger.warning("拒绝删除全局命令: %s", exc)
            await self._send_text(f"❌ {exc}", stream_id, context="数据保护提示")
            return False, "数据保护模式", 1
        except OSError as exc:
            logger.error("保存命令数据失败: %s", exc, exc_info=True)
            await self._send_text(self._save_failure_text(exc, "删除"), stream_id, context="保存失败提示")
            return False, "保存失败", 1

        if success:
            if orphan:
                await p._images.cleanup_orphan_locked(orphan, p._data_manager)
            await self._send_text(
                f"✅ 成功删除了全局自定义命令：'{trigger}'", stream_id,
                context="删除全局命令成功提示",
            )
            logger.info("用户 '%s' 删除全局命令: '%s'", user_id, trigger)
            return True, "全局删除成功", 1

        await self._send_text(
            f"❌ 未在全局作用域找到命令：'{trigger}'", stream_id,
            context="全局命令未找到提示",
        )
        return False, "全局命令未找到", 1

    # ===== 列表 =====

    async def build_list(self, stream_id: str, group_id: str,
                         user_id: str,
                         route: Optional[MessageRoute] = None) -> Tuple[bool, str, int]:
        """列出命令：<前缀>列表。优先合并转发，任何失败降级纯文本。

        ``route`` 为入站消息的路由上下文，用于在多适配器共存时选定合并转发的提供者与账号。
        """
        p = self._plugin
        scope_id, current_scope = self._resolve_scope(group_id, user_id)
        triggers = p._data_manager.get_triggers_for_scope(current_scope)
        prefix = p.config.settings.command_prefix

        if not triggers:
            await self._send_text(
                f"🤷‍♀️ 当前作用域 [{current_scope}] 下没有可用的自定义命令",
                stream_id,
                context="空列表提示",
            )
            return True, "列表已发送", 1

        header_text = build_list_header_text(scope_id, current_scope)
        # triggers 已在 get_triggers_for_scope 中排序
        list_content = "\n".join(f"▪️ {prefix}{trigger}" for trigger in triggers)
        # 优先用 Napcat 合并转发；任何失败（目标 ID 非法、转发被风控、内部异常）
        # 都降级为纯文本列表，保证用户至少能拿到命令清单。
        forward_failure: Optional[str] = None
        try:
            forward_failure = await p._forward.send_list(
                header_text, list_content, group_id, user_id,
                triggers=triggers, prefix=prefix, route=route,
            )
        except ValueError as exc:
            logger.warning("发送命令列表时目标 ID 非法: %s", exc)
            forward_failure = str(exc)
        except Exception as exc:
            logger.error("发送命令列表时发生异常: %s", exc, exc_info=True)
            forward_failure = "内部错误"

        if forward_failure:
            # 来源未知/非 QQ 平台等路由决策也走这条降级，属正常路径，记 info 即可。
            logger.info("合并转发未使用(%s)，降级为纯文本列表", forward_failure)
            try:
                text_ok = await p._forward.send_as_text(header_text, list_content, stream_id)
            except Exception as exc:
                logger.error("纯文本列表降级发送也失败: %s", exc, exc_info=True)
                await self._send_text("❌ 发送命令列表失败", stream_id, context="列表失败提示")
                return False, "列表发送失败", 1
            # send.text 业务失败时返回 False 而非抛异常；合并转发与纯文本两条路径都没送达列表，
            # 此时再发任何文案大概率同样失败，故仅记日志并以失败结果收尾，避免静默成功。
            if text_ok is False:
                logger.warning("纯文本列表降级发送返回 False（可能被风控），列表未送达")
                return False, "列表发送失败", 1

        return True, "列表已发送", 1

    # ===== 动态命中应答 =====

    def lookup(self, trigger: str, group_id: str, user_id: str) -> Optional[str]:
        """查询动态触发词在当前作用域（含 global 回退）下的回复内容；未注册返回 None。

        纯内存查询、不发送。hook 据此在确认命中后立即返回 abort，把真正的投递交给
        ``deliver`` 在后台完成，避免大图发送耗时撞上 Hook 超时后被 Host 按 SKIP 放行、
        同一条消息再落入 LLM 主链造成重复处理。
        """
        _, current_scope = self._resolve_scope(group_id, user_id)
        return self._plugin._data_manager.get(trigger, current_scope)

    async def deliver(self, trigger: str, response_value: str, stream_id: str) -> None:
        """把已命中的回复投递出去：图片走 ImageStore.dispatch_response，文本走 ctx.send。

        所有异常在此吞掉并记日志——本方法运行在后台任务中，异常无人接收。
        """
        p = self._plugin
        try:
            if looks_like_image_response(response_value):
                await p._images.dispatch_response(response_value, stream_id)
            else:
                await self._send_text(response_value, stream_id, context=f"动态命令 '{trigger}' 文本回复")
        except Exception as exc:
            logger.error("动态命令 '%s' 回复投递异常: %s", trigger, exc, exc_info=True)

    async def respond(self, trigger: str, stream_id: str,
                      group_id: str, user_id: str) -> bool:
        """同步版应答（查询 + 投递一步完成），保留给不需要后台化的调用方。

        Returns:
            bool: 命中并已应答返回 True；未注册返回 False。
        """
        response_value = self.lookup(trigger, group_id, user_id)
        if response_value is None:
            return False
        await self.deliver(trigger, response_value, stream_id)
        return True
