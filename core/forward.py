"""列表转发模块：把命令列表经 Host 通用合并转发发出，失败降级纯文本。

``ListForwardSender`` 持 plugin 弱引用，只用 SDK 通用能力 ``ctx.send.forward``：消息经 Host 的
Platform IO 路由到入站消息所属的适配器与账号，由适配器按对端实现组装 OneBot 动作（例如 NapCat
对端用 ``message`` 键、SnowLuma 对端用 ``messages`` 键），插件无需识别来源适配器、拼接 API 全名
或兼容各家的返回形态。代价是没有 news/summary/prompt/source 卡片外显字段——SnowLuma 本就会丢弃
它们，卡片按默认样式展示。

仅对 QQ 平台使用合并转发；其它平台、发送失败或返回 False 时由调用方降级为纯文本。
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, Dict, Optional

from .common import MessageRoute

if TYPE_CHECKING:
    from ..plugin import CustomCommandsPlugin

logger = logging.getLogger(__name__)

# 合并转发节点的展示昵称。
_FORWARD_NODE_NICKNAME = "自定义命令"
# 入站消息不带 bot 账号时节点使用的占位发送者；QQ 端仍能渲染合并转发，只是发送者头像缺省。
_FALLBACK_FORWARD_UIN = "0"


class ListForwardSender:
    """命令列表的合并转发投递（含纯文本降级）。"""

    def __init__(self, plugin: "CustomCommandsPlugin") -> None:
        self._plugin = plugin

    @staticmethod
    def _build_node(text: str, uin: str) -> Dict[str, Any]:
        """构造 ``send.forward`` 的单个转发节点（由 Host 转成适配器的 node 段）。"""
        return {
            "user_id": uin,
            "nickname": _FORWARD_NODE_NICKNAME,
            "segments": [{"type": "text", "content": text}],
        }

    async def send_list(self, header_text: str, list_content: str,
                        stream_id: str, route: Optional[MessageRoute] = None) -> Optional[str]:
        """使用合并转发发送列表。

        Args:
            stream_id: 目标聊天流（Host 命令链给出的会话 ID）。
            route: 入站消息的路由上下文；用于判断平台与取 bot 账号作为节点发送者。

        Returns:
            Optional[str]: 失败原因（供调用方降级）；成功返回 None。
        """
        platform = ((route.platform if route is not None else "") or "qq").strip().lower()
        if platform != "qq":
            return f"平台 {platform!r} 不使用 QQ 合并转发"
        if not stream_id:
            return "缺少会话 ID"

        uin = (route.account_id if route is not None else "") or _FALLBACK_FORWARD_UIN
        sent = await self._plugin.ctx.send.forward(
            [self._build_node(header_text, uin), self._build_node(list_content, uin)],
            stream_id,
            processed_plain_text="[自定义命令列表]",
        )
        # send.* 业务失败返回 False 而非抛异常（SDK _BOOLEAN_SUCCESS_CAPABILITIES）。
        if sent is False:
            return "send.forward 返回 False"
        return None

    async def send_as_text(self, header_text: str, list_content: str,
                           stream_id: str) -> Any:
        """合并转发不可用时的纯文本降级：头部与列表拼成一条文本发送。

        返回 ``ctx.send.text`` 的结果：SDK 在业务失败时返回 ``False`` 而非抛异常
        （见 _BOOLEAN_SUCCESS_CAPABILITIES），调用方据此判断纯文本是否真正送达。
        """
        return await self._plugin.ctx.send.text(f"{header_text}\n\n{list_content}", stream_id)
