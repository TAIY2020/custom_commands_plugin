"""列表转发模块：把命令列表渲成 Napcat 合并转发卡片，失败降级纯文本。

``ListForwardSender`` 持 plugin 弱引用 + 自缓存 bot_uin。**强耦合 napcat 适配器**的
透传合并转发 API（send_group/private_forward_msg）——为携带 news/source/summary/prompt
卡片外显字段（SDK 通用 ctx.send.forward 不暴露这些）。换其他适配器（Lagrange/Telegram
等）时该 API 名不存在、调用会失败；``send_list`` 把失败原因返回给调用方据此降级为纯文本，
功能不中断、仅退化为无卡片展示。"哪里强耦合 napcat"的知识集中在本文件。
"""

from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING, Any, Dict, List, Optional

if TYPE_CHECKING:
    from ..plugin import CustomCommandsPlugin

logger = logging.getLogger(__name__)


class ListForwardSender:
    """命令列表的 Napcat 合并转发投递（含纯文本降级）。"""

    # get_login_info 查询失败时，合并转发节点使用的占位发送者 uin。
    _FALLBACK_FORWARD_UIN = "0"
    # bot uin 缓存有效期（秒）。缓存避免每次发列表都发一次 RPC；设上限让 bot 换登录账号后
    # 最迟在 TTL 内自动刷新，而非永久沿用旧号。仅用于合并转发节点发送者外显，TTL 取宽松值
    # 即可，额外 RPC 开销可忽略。
    _UIN_CACHE_TTL_SECONDS = 300.0

    def __init__(self, plugin: "CustomCommandsPlugin") -> None:
        self._plugin = plugin
        self._bot_uin: str = ""  # 缓存 bot 自身 QQ 号（合并转发节点发送者 uin 用）
        self._bot_uin_fetched_at: float = 0.0  # 上次成功取到 uin 的单调时钟时间戳

    async def _get_bot_uin(self) -> str:
        """获取并缓存 bot 自身 QQ 号，作为合并转发节点的发送者 uin。

        通过适配器强类型 API ``get_login_info`` 查询；结果在本实例内带 TTL 缓存，既避免
        每次发送列表都发起一次 RPC，又能在 bot 换号后于 TTL 内自动刷新。查询失败时沿用
        上次的有效 uin（若有），否则回退占位 uin——Napcat 仍能渲染合并转发，只是发送者
        信息缺省。失败路径不刷新时间戳，故下次发列表会主动重试而非缓存失败态一个 TTL。
        """
        now = time.monotonic()
        if self._bot_uin and (now - self._bot_uin_fetched_at) < self._UIN_CACHE_TTL_SECONDS:
            return self._bot_uin
        try:
            result = await self._plugin.ctx.api.call("adapter.napcat.system.get_login_info")
        except Exception as exc:
            logger.warning("获取 bot 登录信息失败: %s，合并转发将沿用旧 uin 或占位", exc)
            return self._bot_uin or self._FALLBACK_FORWARD_UIN
        # ctx.api.call 成功时 SDK 已剥掉 Host 的 {success,result} 外层
        # (context.py::_normalize_capability_result 提取 result 字段)，result 直接是
        # get_login_info 的业务对象 {user_id,...}；失败响应 {success:False,...} 无 result
        # key 不被剥层、原样返回，下面取不到 user_id 会正确回退。切勿再 .get("result")。
        login_info = result if isinstance(result, dict) else None
        if isinstance(login_info, dict):
            uin = str(login_info.get("user_id") or "").strip()
            if uin:
                self._bot_uin = uin
                self._bot_uin_fetched_at = now
                return uin
        logger.warning("get_login_info 未返回有效 user_id，合并转发将沿用旧 uin 或占位")
        return self._bot_uin or self._FALLBACK_FORWARD_UIN

    @staticmethod
    def _build_node(text: str, uin: str) -> Dict[str, Any]:
        """构造 Napcat 合并转发节点。``uin`` 为节点发送者 QQ 号。"""
        return {
            "type": "node",
            "data": {
                "name": "自定义命令",
                "uin": uin,
                "content": [{"type": "text", "data": {"text": text}}],
            },
        }

    @staticmethod
    def _parse_target_id(target_id: str, field_name: str) -> int:
        """解析 Napcat 合并转发所需的目标 ID。"""
        normalized_target_id = str(target_id).strip()
        if not normalized_target_id:
            raise ValueError(f"缺少 {field_name}")
        try:
            parsed_target_id = int(normalized_target_id)
        except ValueError as exc:
            raise ValueError(f"{field_name} 不是有效数字: {normalized_target_id}") from exc
        if parsed_target_id <= 0:
            raise ValueError(f"{field_name} 必须是正整数")
        return parsed_target_id

    @staticmethod
    def _get_api_error(api_result: Any) -> Optional[str]:
        """提取 Napcat 合并转发 API 的失败信息。

        正常情况下 NapCat 业务失败会由 adapter raise，并被 Host 包装成
        ``success=False``；这里也兼容原始 NapCat 响应透传到插件端的形态
        （如 ``status != ok`` 或 ``retcode != 0``），避免误判为发送成功。
        """
        if not isinstance(api_result, dict):
            return None
        if api_result.get("success") is False:
            return str(
                api_result.get("error")
                or api_result.get("message")
                or api_result.get("wording")
                or "Napcat 合并转发调用失败"
            )

        status = api_result.get("status")
        if isinstance(status, str) and status.lower() not in ("ok", "success"):
            return str(
                api_result.get("wording")
                or api_result.get("message")
                or api_result.get("error")
                or f"Napcat 返回异常状态: {status}"
            )

        retcode = api_result.get("retcode")
        if retcode not in (None, 0):
            return str(
                api_result.get("wording")
                or api_result.get("message")
                or api_result.get("error")
                or f"Napcat 返回异常 retcode: {retcode}"
            )
        return None

    async def send_list(self, header_text: str, list_content: str,
                        group_id: str, user_id: str,
                        triggers: Optional[List[str]] = None,
                        prefix: str = "") -> Optional[str]:
        """使用 Napcat 合并转发发送列表。

        Args:
            triggers: 用于在卡片预览（news）中展示的触发词列表，最多取前 4 条。
            prefix: 命令前缀，用于在 news 文本中拼接。

        Returns:
            Optional[str]: 失败原因（供调用方降级）；成功返回 None。
            目标 ID 非法时抛 ValueError，由调用方捕获降级。
        """
        bot_uin = await self._get_bot_uin()
        message_nodes = [
            self._build_node(header_text, bot_uin),
            self._build_node(list_content, bot_uin),
        ]

        news = [
            {"text": f"{prefix}{t}"} for t in (triggers or [])[:4]
        ] or [{"text": "点击查看完整列表"}]

        if group_id:
            api_result = await self._plugin.ctx.api.call(
                "adapter.napcat.message.send_group_forward_msg",
                params={
                    "message_type": "group",
                    "group_id": self._parse_target_id(group_id, "group_id"),
                    "message": message_nodes,
                    "source": "自定义命令",
                    "news": news,
                    "summary": "自定义命令列表",
                    "prompt": "点击查看命令列表",
                },
            )
            return self._get_api_error(api_result)

        api_result = await self._plugin.ctx.api.call(
            "adapter.napcat.message.send_private_forward_msg",
            params={
                "message_type": "private",
                "user_id": self._parse_target_id(user_id, "user_id"),
                "message": message_nodes,
                "source": "自定义命令",
                "news": news,
                "summary": "自定义命令列表",
                "prompt": "点击查看命令列表",
            },
        )
        return self._get_api_error(api_result)

    async def send_as_text(self, header_text: str, list_content: str,
                           stream_id: str) -> Any:
        """合并转发不可用时的纯文本降级：头部与列表拼成一条文本发送。

        返回 ``ctx.send.text`` 的结果：SDK 在业务失败时返回 ``False`` 而非抛异常
        （见 _BOOLEAN_SUCCESS_CAPABILITIES），调用方据此判断纯文本是否真正送达。
        """
        return await self._plugin.ctx.send.text(f"{header_text}\n\n{list_content}", stream_id)
