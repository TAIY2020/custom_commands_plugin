"""动态触发路由层：接管 chat.receive.after_process hook 的入站消息。

``DynamicDispatcher`` 只碰 hook ``message``——重组 raw_message 文本、判定这条消息是
「带图添加 / 动态触发命中 / 该放行」，再委托 ``CommandService`` 执行业务。它不直接碰
storage/images/scope，命令业务统一由 service 完成。

改造历史：早期版本把动态触发也用 @Command(pattern=r"^{prefix}(?P<trigger>.+)$")
注册，主程序"第一个 pattern 命中即独占"的分发器会让本插件抢走所有"前缀+任意字符"
的消息，handler 查不到 trigger 时也不能让出——其他插件用同 prefix 的命令会被永久屏蔽。
Hook 路径按 order 顺次执行，未 abort 就放行，彻底绕过 first-match-wins。
"""

from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Tuple

from .common import KW_ADD, KW_ADD_ANSWER, is_reserved_trigger

if TYPE_CHECKING:
    from ..plugin import CustomCommandsPlugin

logger = logging.getLogger(__name__)


class DynamicDispatcher:
    """入站消息的动态命令路由。"""

    def __init__(self, plugin: "CustomCommandsPlugin") -> None:
        self._plugin = plugin

    @staticmethod
    def _extract_text_and_images(
        raw_message: Any,
    ) -> Tuple[str, List[Dict[str, Any]]]:
        """从 raw_message 段列表中拼接纯文本、收集图片段。

        不依赖 processed_plain_text——后者会把图片渲染成 [图片]/描述占位符，
        混进"答："区干扰触发词解析。这里只认 text 段原文与 image/emoji 段。
        """
        text_parts: List[str] = []
        image_segs: List[Dict[str, Any]] = []
        if isinstance(raw_message, list):
            for seg in raw_message:
                if not isinstance(seg, dict):
                    continue
                seg_type = seg.get("type")
                if seg_type == "text":
                    data = seg.get("data")
                    if isinstance(data, str):
                        text_parts.append(data)
                    elif isinstance(data, dict):
                        text = data.get("text")
                        if isinstance(text, str):
                            text_parts.append(text)
                elif seg_type in ("image", "emoji"):
                    image_segs.append(seg)
        return "".join(text_parts), image_segs

    @staticmethod
    def _pick_image(image_segs: List[Dict[str, Any]]) -> Tuple[str, str]:
        """取第一张带 binary_data_base64 的图片段，返回 (b64_data, url_hint)。

        napcat 适配器的 image/emoji 段 data 字段在下载后被清空（恒为 ""，见适配器
        _build_image_like_segment），故 url_hint 在 napcat 下实际取不到后缀，扩展名
        由 ImageStore.guess_extension 的二进制 magic number 兜底；保留 url_hint 仅为
        兼容「会在 data 里给出 url」的其他适配器。
        """
        for seg in image_segs:
            candidate = seg.get("binary_data_base64")
            if isinstance(candidate, str) and candidate:
                data_field = seg.get("data")
                if isinstance(data_field, str):
                    url_hint = data_field
                elif isinstance(data_field, dict):
                    url_hint = ""
                    for key in ("url", "file", "path", "summary"):
                        value = data_field.get(key)
                        if isinstance(value, str) and value:
                            url_hint = value
                            break
                else:
                    url_hint = ""
                return candidate, url_hint
        return "", ""

    async def dispatch(self, message: Optional[dict]) -> Optional[Dict[str, Any]]:
        """动态触发命令的 hook 入口逻辑。

        返回 ``{"action": "abort"}`` 表示已处理 + 拦截后续主链；返回 ``None`` 放行
        让消息继续走 Command 调度与 LLM 主链。任何"不该由本插件处理"的消息
        （非群/私聊文本、不带前缀、命中内置命令、未注册 trigger）都必须返回 None。
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

        # 统一从 raw_message 文本段重组「干净文本」：text 段原文拼接，天然不含
        # [image]/[reply]/[emoji]/[voice] 占位符与 "@昵称" 渲染，也不受图文顺序影响。
        # 不能直接用 processed_plain_text 做匹配——适配器 build_plain_text 会把非文本段
        # 渲染成占位符，导致引用消息 / 带图 / @人 场景下触发词被占位符污染而失配。
        # clean_text 为空时（消息无 text 段）兜底 processed_plain_text。
        clean_text, image_segs = self._extract_text_and_images(message.get("raw_message"))
        base_text = clean_text.strip() or str(message.get("processed_plain_text") or "").strip()

        # 带图添加：<前缀>问：触发词答：[图片]。必须先于动态触发执行——带图添加文本的
        # trigger 段以「问：」开头会被 is_reserved_trigger 判为保留词，而 @Command
        # handle_add 的 pattern 要求「答：」后有 response、带图时「答：」后为空故不匹配，
        # 若不在此抢先截获，这条消息会漏过命令路径直达 LLM。另外，Host 对 chat.receive.* hook
        # 的 message 默认携带二进制数据（hook_payloads.serialize_session_message 固定按
        # include_binary_data=True 序列化），raw_message 的 image/emoji 段才有 binary_data_base64；
        # 而 @Command 路径的 message 被显式以 include_binary_data=False 序列化、拿不到图，
        # 故带图添加只能在本 hook 完成（与装饰器声明的 include_binary_data 无关，详见 plugin.py）。
        image_add_result = await self._try_image_add(message, prefix, base_text, image_segs)
        if image_add_result is not None:
            return image_add_result

        # 动态触发：基于 base_text 的「前缀 + 触发词」匹配
        if not base_text or not base_text.startswith(prefix):
            return None

        trigger = base_text[len(prefix):].strip()
        if not trigger:
            return None

        # 内置命令交给精确 pattern 的 @Command 处理，避免与同名动态 trigger 冲突
        if is_reserved_trigger(trigger):
            return None

        msg_info = message.get("message_info") or {}
        user_info = msg_info.get("user_info") or {}
        group_info = msg_info.get("group_info") or {}
        stream_id = str(message.get("session_id") or "")
        group_id = str(group_info.get("group_id") or "")
        user_id = str(user_info.get("user_id") or "")
        if not stream_id:
            return None

        # 命中已注册 trigger 由 service 应答（发送回复）；命中即 abort 后续主链
        hit = await p._service.respond(trigger, stream_id, group_id, user_id)
        return {"action": "abort"} if hit else None

    async def _try_image_add(
        self, message: Dict[str, Any], prefix: str,
        base_text: str, image_segs: List[Dict[str, Any]],
    ) -> Optional[Dict[str, Any]]:
        """尝试把"<前缀>问：触发词答：[图片]"这条消息作为"带图添加"处理。

        ``base_text`` 与 ``image_segs`` 由 dispatch 从 raw_message 统一重组后传入。
        图片排在文字前面或后面都可以——base_text 取自 raw_message 文本段重组，
        不受图文顺序影响。

        返回值语义：
        - ``None``                —— 不是带图添加场景（无图 / 文本不符合添加格式 /
                                     无可回复会话），调用方继续走 reserved 判断与动态触发。
        - ``{"action": "abort"}`` —— 已确认是带图添加，业务已交由 CommandService.add_image
                                     处理完毕（成功或失败都已回发消息），拦截后续主链。
        """
        if not image_segs:
            return None  # 没有图片 → 不是带图添加，放行

        if not base_text:
            return None
        # 前缀校验由正则 ^<prefix>问： 承担；base_text 已不含占位符干扰。
        match = re.match(rf"^{re.escape(prefix)}{KW_ADD}(?P<trigger>.+?){KW_ADD_ANSWER}", base_text)
        if not match:
            return None  # 有图但文本不符合添加格式 → 放行（带图触发或普通图片消息）

        # —— 确认是"带图添加"，此后无论成败都回发消息并 abort ——
        msg_info = message.get("message_info") or {}
        user_info = msg_info.get("user_info") or {}
        group_info = msg_info.get("group_info") or {}
        stream_id = str(message.get("session_id") or "")
        group_id = str(group_info.get("group_id") or "")
        user_id = str(user_info.get("user_id") or "")
        if not stream_id:
            return None  # 没有可回复的会话，交回主链

        trailing_text = base_text[match.end():].strip()
        if trailing_text:
            await self._plugin._service._send_text(
                "❌ 带图添加时「答：」后请不要再填写文字；如需文本回复请不要附带图片",
                stream_id,
                context="带图添加格式冲突提示",
            )
            return {"action": "abort"}

        trigger = match.group("trigger").strip()
        # 仅支持单张图片：取第一张带 binary_data_base64 的图片段。
        b64_data, url_hint = self._pick_image(image_segs)
        # 权限 / 触发词 / 图片字节的全部校验与回执都在 service.add_image 内完成
        await self._plugin._service.add_image(
            trigger, b64_data, url_hint, stream_id, group_id, user_id,
        )
        return {"action": "abort"}
