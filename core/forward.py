"""列表转发模块：把命令列表渲成 Napcat 合并转发卡片，失败降级纯文本。

``ListForwardSender`` 持 plugin 弱引用 + 按提供者缓存 bot_uin。**强耦合 napcat 风格适配器**的
透传合并转发 API（send_group/private_forward_msg）——为携带 news/source/summary/prompt
卡片外显字段（SDK 通用 ctx.send.forward 不暴露这些）。换其他适配器（Lagrange/Telegram
等）时该 API 名不存在、调用会失败；``send_list`` 把失败原因返回给调用方据此降级为纯文本，
功能不中断、仅退化为无卡片展示。"哪里强耦合 napcat"的知识集中在本文件。

多适配器共存：NapCat 与 SnowLuma 两个适配器都把 API 注册在 ``adapter.napcat.*`` 短名下，
同时启用时用短名调用会被 Host 判为「API 名称不唯一」而失败。本模块按 ``MessageRoute``
里识别出的来源适配器拼 ``<plugin_id>.<短名>`` 全名调用，并把登录查询与转发绑定到同一提供者；
识别不出来源时退回短名（单适配器部署下与原行为一致）。
"""

from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING, Any, Dict, List, Optional

from .common import MessageRoute

if TYPE_CHECKING:
    from ..plugin import CustomCommandsPlugin

logger = logging.getLogger(__name__)

_API_GET_LOGIN_INFO = "adapter.napcat.system.get_login_info"
_API_SEND_GROUP_FORWARD = "adapter.napcat.message.send_group_forward_msg"
_API_SEND_PRIVATE_FORWARD = "adapter.napcat.message.send_private_forward_msg"


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
        # 按「提供者 + 账号」缓存 bot uin：多适配器/多账号共存时不能共用一份缓存，
        # 否则经 A 适配器查到的账号会被套在 B 适配器的转发节点上。
        self._bot_uin_cache: Dict[str, tuple[str, float]] = {}

    # ----- API 名解析 -----

    @staticmethod
    def _api_name(route: Optional[MessageRoute], short_name: str) -> str:
        """按来源适配器拼全名；``send_list`` 已保证 route 带来源，此处退回短名仅为防御。"""
        if route is not None and route.adapter_plugin_id:
            return f"{route.adapter_plugin_id}.{short_name}"
        return short_name

    @staticmethod
    def _cache_key(route: Optional[MessageRoute]) -> str:
        if route is None:
            return ""
        return f"{route.adapter_plugin_id}|{route.account_id}"

    # ----- bot uin -----

    @staticmethod
    def _extract_login_uin(result: Any) -> str:
        """从 get_login_info 的返回里取 user_id，兼容两种适配器的返回形态。

        NapCat 强类型 API 返回剥过层的业务 dict（顶层即 user_id）；SnowLuma 直接返回原始
        OneBot 响应（user_id 在 data 里）。先排除失败形态（success=False / status 非 ok），
        再顶层取，取不到再钻一层 data。
        """
        if not isinstance(result, dict):
            return ""
        if result.get("success") is False:
            return ""
        status = result.get("status")
        if isinstance(status, str) and status and status.lower() not in ("ok", "success"):
            return ""
        uin = str(result.get("user_id") or "").strip()
        if uin:
            return uin
        data = result.get("data")
        if isinstance(data, dict):
            return str(data.get("user_id") or "").strip()
        return ""

    async def _get_bot_uin(self, route: Optional[MessageRoute]) -> str:
        """获取并缓存 bot 自身 QQ 号，作为合并转发节点的发送者 uin。

        来源可信时优先用入站消息自带的账号（additional_config.self_id），省一次 RPC；
        否则经适配器 ``get_login_info`` 查询，结果按提供者+账号缓存并带 TTL。查询失败时沿用
        上次的有效 uin（若有），否则回退占位 uin——Napcat 仍能渲染合并转发，只是发送者
        信息缺省。失败路径不刷新时间戳，故下次发列表会主动重试而非缓存失败态一个 TTL。
        """
        if route is not None and route.account_id:
            return route.account_id

        cache_key = self._cache_key(route)
        now = time.monotonic()
        cached_uin, fetched_at = self._bot_uin_cache.get(cache_key, ("", 0.0))
        if cached_uin and (now - fetched_at) < self._UIN_CACHE_TTL_SECONDS:
            return cached_uin

        api_name = self._api_name(route, _API_GET_LOGIN_INFO)
        try:
            result = await self._plugin.ctx.api.call(api_name)
        except Exception as exc:
            logger.warning("获取 bot 登录信息失败(%s): %s，合并转发将沿用旧 uin 或占位", api_name, exc)
            return cached_uin or self._FALLBACK_FORWARD_UIN
        # ctx.api.call 成功时 SDK 已剥掉 Host 的 {success,result} 外层
        # (context.py::_normalize_capability_result 提取 result 字段)，result 直接是
        # 适配器 API 的返回；失败响应 {success:False,...} 无 result key 不被剥层、原样返回。
        uin = self._extract_login_uin(result)
        if uin:
            self._bot_uin_cache[cache_key] = (uin, now)
            return uin
        logger.warning("%s 未返回有效 user_id，合并转发将沿用旧 uin 或占位", api_name)
        return cached_uin or self._FALLBACK_FORWARD_UIN

    # ----- 节点与结果解析 -----

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
        """提取合并转发 API 的失败信息。

        NapCat 业务失败会由 adapter raise，并被 Host 包装成 ``success=False``；SnowLuma 的
        ``_action_result`` 保留原始 ``status``/``retcode`` 且业务失败不抛异常，故也兼容
        ``status != ok`` 或 ``retcode != 0`` 的形态，避免误判为发送成功。
        """
        if not isinstance(api_result, dict):
            return None
        if api_result.get("success") is False:
            return str(
                api_result.get("error")
                or api_result.get("message")
                or api_result.get("wording")
                or "合并转发调用失败"
            )

        status = api_result.get("status")
        if isinstance(status, str) and status and status.lower() not in ("ok", "success"):
            return str(
                api_result.get("wording")
                or api_result.get("message")
                or api_result.get("error")
                or f"适配器返回异常状态: {status}"
            )

        retcode = api_result.get("retcode")
        if retcode not in (None, 0):
            return str(
                api_result.get("wording")
                or api_result.get("message")
                or api_result.get("error")
                or f"适配器返回异常 retcode: {retcode}"
            )
        return None

    # ----- 发送 -----

    async def send_list(self, header_text: str, list_content: str,
                        group_id: str, user_id: str,
                        triggers: Optional[List[str]] = None,
                        prefix: str = "",
                        route: Optional[MessageRoute] = None) -> Optional[str]:
        """使用合并转发发送列表。

        Args:
            triggers: 用于在卡片预览（news）中展示的触发词列表，最多取前 4 条。
            prefix: 命令前缀，用于在 news 文本中拼接。
            route: 入站消息的路由上下文；据此选定提供者与账号。为 None 时退回短名调用。

        Returns:
            Optional[str]: 失败原因（供调用方降级）；成功返回 None。
            目标 ID 非法时抛 ValueError，由调用方捕获降级。
            来源适配器无法识别或平台不是 QQ 时不发起任何 API 调用，直接返回原因：
            "只有一个可用提供者"不代表它就是入站消息的来源，盲目调用 QQ 短名可能把列表
            发到错误的平台或账号，且因 API 成功而不再向原会话降级。
        """
        if route is None or not route.adapter_plugin_id:
            return "无法识别消息来源适配器，不使用合并转发"
        if (route.platform or "qq").strip().lower() != "qq":
            return f"平台 {route.platform!r} 不支持 QQ 合并转发"

        bot_uin = await self._get_bot_uin(route)
        message_nodes = [
            self._build_node(header_text, bot_uin),
            self._build_node(list_content, bot_uin),
        ]

        news = [
            {"text": f"{prefix}{t}"} for t in (triggers or [])[:4]
        ] or [{"text": "点击查看完整列表"}]

        # SnowLuma 的转发 API 会丢掉 source/news/summary/prompt 四个卡片外显字段，
        # 卡片按默认样式展示；这是适配器能力差异，插件侧无需分支处理。
        if group_id:
            api_result = await self._plugin.ctx.api.call(
                self._api_name(route, _API_SEND_GROUP_FORWARD),
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
            self._api_name(route, _API_SEND_PRIVATE_FORWARD),
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
