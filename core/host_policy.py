"""Host 命令门禁的插件侧镜像：给绕过 Host 命令链的 hook 路径补齐同口径授权。

Host（≥1.3.18）对 @Command 在执行前依次检查（主程序 ``chat/message_receive/bot.py``::
``_process_commands``）：命令管理停用（``plugin.disabled_commands``）→ 插件使用范围
（``plugin.usage_permissions``，1.4.0+）→ ``permission="operator"`` 的受保护命令权限
（``plugin.permission`` 全局管理员 + ``plugin.command_permissions`` 按命令放行；插件代发的消息
一律无权，见 ``core/local_operator.py``::``has_command_permission``）。

带图添加与动态触发在 ``chat.receive.after_process`` hook 内自行完成，不经过这条链，故由本模块
按同一口径复刻判定。数据来源是 ``config.get("plugin")``：Host 读出全局配置的 plugin 节，经
RPC 序列化成普通 dict 下发。插件订阅 ``bot`` 配置广播，热重载时直接用广播快照刷新；平时按调用方
给定的有效期兜底重新读取。会话级禁用（聊天内禁用某命令）没有可读接口，不在复刻范围内。
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Dict, FrozenSet, Iterable, Mapping, Optional

if TYPE_CHECKING:
    from ..plugin import CustomCommandsPlugin

logger = logging.getLogger(__name__)

# 单次读取 Host 配置的期限（毫秒）：调用方在 blocking hook 内等待，必须有界。
_FETCH_TIMEOUT_MS = 3000
# 读取失败后的退避（秒）：期间允许使用旧快照的调用（max_age>0）直接拿旧快照，不再逐次重试，
# 避免 Host 繁忙时每条动态触发都多等一个读取期限。要求最新配置的调用（max_age=0）不受退避影响。
_FAILURE_BACKOFF_SECONDS = 10.0

# 与主程序 core/local_operator.py 保持一致。
_BOT_CONSOLE_PLATFORM = "bot_console"
_LOCAL_OPERATOR_CONFIG_KEY = "is_local_operator"
_PLUGIN_RELAY_CONFIG_KEY = "relayed_by_plugin"


def _scoped_user_id(platform: str, user_id: str) -> str:
    """``平台:用户 ID``，平台转小写、用户 ID 保持原样（同 Host ``build_scoped_user_id``）。"""
    normalized_platform = (platform or "").strip().lower()
    normalized_user_id = (user_id or "").strip()
    if not normalized_platform or not normalized_user_id:
        return ""
    return f"{normalized_platform}:{normalized_user_id}"


def _normalize_users(values: Any) -> FrozenSet[str]:
    """规范化用户放行列表（同 Host ``normalize_operator_permissions``）。"""
    if not isinstance(values, (list, tuple)):
        return frozenset()
    normalized = set()
    for value in values:
        text = str(value or "").strip()
        if not text:
            continue
        platform, _, user_id = text.partition(":")
        normalized.add(f"{platform.strip().lower()}:{user_id.strip()}")
    return frozenset(normalized)


def _normalize_chats(values: Any) -> FrozenSet[str]:
    if not isinstance(values, (list, tuple)):
        return frozenset()
    return frozenset(text for text in (str(value or "").strip() for value in values) if text)


@dataclass(frozen=True)
class SenderIdentity:
    """一条入站消息在 Host 鉴权口径下的发送者身份。"""

    platform: str = ""
    user_id: str = ""
    session_id: str = ""
    local_operator: bool = False
    relayed_by_plugin: bool = False

    @property
    def scoped_user_id(self) -> str:
        return _scoped_user_id(self.platform, self.user_id)


def sender_identity_from_message(message: Optional[Dict[str, Any]]) -> SenderIdentity:
    """从 hook 收到的 message dict 提取发送者身份，字段取法与 Host 命令链一致。"""
    msg = message if isinstance(message, dict) else {}
    msg_info = msg.get("message_info") if isinstance(msg.get("message_info"), dict) else {}
    user_info = msg_info.get("user_info") if isinstance(msg_info.get("user_info"), dict) else {}
    additional = msg_info.get("additional_config") if isinstance(msg_info.get("additional_config"), dict) else {}
    platform = str(msg.get("platform") or "")
    return SenderIdentity(
        platform=platform,
        user_id=str(user_info.get("user_id") or ""),
        session_id=str(msg.get("session_id") or ""),
        local_operator=platform == _BOT_CONSOLE_PLATFORM and additional.get(_LOCAL_OPERATOR_CONFIG_KEY) is True,
        relayed_by_plugin=bool(additional.get(_PLUGIN_RELAY_CONFIG_KEY)),
    )


@dataclass(frozen=True)
class AccessRule:
    """按用户 / 真实聊天流放行的规则。"""

    users: FrozenSet[str] = frozenset()
    chats: FrozenSet[str] = frozenset()

    @classmethod
    def from_mapping(cls, raw: Any) -> "AccessRule":
        if not isinstance(raw, Mapping):
            return cls()
        return cls(users=_normalize_users(raw.get("allow_users")), chats=_normalize_chats(raw.get("allow_chats")))

    def allows(self, identity: SenderIdentity) -> bool:
        scoped_user = identity.scoped_user_id
        return bool(scoped_user and scoped_user in self.users) or bool(
            identity.session_id and identity.session_id in self.chats
        )


@dataclass(frozen=True)
class HostPolicySnapshot:
    """Host ``plugin`` 配置节中与命令授权相关的部分（不可变快照）。"""

    operators: FrozenSet[str] = frozenset()
    silent_permission_denied: bool = False
    disabled_commands: FrozenSet[str] = frozenset()
    command_rules: Mapping[str, AccessRule] = field(default_factory=dict)
    # 只收录 enabled=True 的使用范围规则；未收录即不限制。
    usage_rules: Mapping[str, AccessRule] = field(default_factory=dict)

    @classmethod
    def from_plugin_section(cls, section: Any) -> Optional["HostPolicySnapshot"]:
        """解析 Host 全局配置的 plugin 节；结构不符（缺 permission 列表）时返回 None。

        ``permission`` 是 Host 1.3.x 起就有的必备字段，缺失说明读到的不是预期配置，
        不能把它当成「没有任何管理员」——那会让带图添加对所有人放行或拒绝都失真。
        """
        if not isinstance(section, Mapping) or not isinstance(section.get("permission"), (list, tuple)):
            return None
        raw_command_rules = section.get("command_permissions")
        raw_usage_rules = section.get("usage_permissions")
        return cls(
            operators=_normalize_users(section.get("permission")),
            silent_permission_denied=bool(section.get("silent_permission_denied", False)),
            disabled_commands=_normalize_chats(section.get("disabled_commands")),
            command_rules={
                str(command_id): AccessRule.from_mapping(rule)
                for command_id, rule in (raw_command_rules.items() if isinstance(raw_command_rules, Mapping) else ())
            },
            usage_rules={
                str(plugin_id): AccessRule.from_mapping(rule)
                for plugin_id, rule in (raw_usage_rules.items() if isinstance(raw_usage_rules, Mapping) else ())
                if isinstance(rule, Mapping) and rule.get("enabled") is True
            },
        )

    def is_command_disabled(self, command_id: str) -> bool:
        """命令是否在 Host「命令管理」中被停用。"""
        return command_id in self.disabled_commands

    def can_use_plugin(self, plugin_id: str, identity: SenderIdentity) -> bool:
        """插件使用范围（同 Host ``has_plugin_usage_permission``）：未限制放行，限制后用户或会话任一命中。"""
        rule = self.usage_rules.get(plugin_id)
        return rule is None or rule.allows(identity)

    def has_command_permission(self, command_id: str, identity: SenderIdentity) -> bool:
        """受保护命令权限（同 Host ``has_command_permission``）。"""
        if identity.relayed_by_plugin:
            return False
        if identity.local_operator:
            return True
        scoped_user = identity.scoped_user_id
        if scoped_user and scoped_user in self.operators:
            return True
        rule = self.command_rules.get(command_id)
        return rule is not None and rule.allows(identity)

    def command_allow_user_count(self, command_ids: Iterable[str]) -> int:
        """若干命令的「仅为此命令放行」用户与聊天总数（配置页展示用）。"""
        total = 0
        for command_id in command_ids:
            rule = self.command_rules.get(command_id)
            if rule is not None:
                total += len(rule.users) + len(rule.chats)
        return total


class HostPolicy:
    """Host 命令授权配置的缓存与读取。"""

    def __init__(self, plugin: "CustomCommandsPlugin") -> None:
        self._plugin = plugin
        self._snapshot: Optional[HostPolicySnapshot] = None
        self._fetched_at: float = 0.0
        self._failed_at: Optional[float] = None
        self._refresh_lock = asyncio.Lock()

    @property
    def snapshot(self) -> Optional[HostPolicySnapshot]:
        """最近一次成功获取的快照（可能已过期）；从未成功时为 None。"""
        return self._snapshot

    def apply_broadcast(self, config_data: Any) -> None:
        """用 ``bot`` 配置热重载广播（整份全局配置）刷新快照。"""
        section = config_data.get("plugin") if isinstance(config_data, Mapping) else None
        snapshot = HostPolicySnapshot.from_plugin_section(section)
        if snapshot is None:
            logger.warning("bot 配置广播中缺少有效的 plugin 节，Host 权限快照保持不变")
            return
        self._snapshot = snapshot
        self._fetched_at = time.monotonic()
        self._failed_at = None

    async def get(self, *, max_age: float) -> Optional[HostPolicySnapshot]:
        """返回不超过 ``max_age`` 秒的快照，过期则重新读取；读取失败返回 None。

        ``max_age=0`` 表示必须读到本次调用之后的最新配置（并发调用会合并为一次读取）。
        ``max_age>0`` 时，若最近一次读取失败仍在退避期内，直接返回旧快照（可能为 None）。
        """
        requested_at = time.monotonic()
        if self._snapshot is not None and max_age > 0 and requested_at - self._fetched_at <= max_age:
            return self._snapshot
        if max_age > 0 and self._failed_at is not None and requested_at - self._failed_at < _FAILURE_BACKOFF_SECONDS:
            return self._snapshot
        async with self._refresh_lock:
            if self._snapshot is not None and self._fetched_at >= requested_at:
                return self._snapshot  # 等锁期间已有调用读到更新的配置
            snapshot = await self._fetch()
            self._failed_at = None if snapshot is not None else time.monotonic()
            return snapshot

    async def refresh(self) -> Optional[HostPolicySnapshot]:
        """强制重新读取（on_load 预热用）。"""
        return await self.get(max_age=0)

    async def _fetch(self) -> Optional[HostPolicySnapshot]:
        try:
            section = await asyncio.wait_for(
                self._plugin.ctx.call_capability(
                    "config.get", key="plugin", default=None, timeout_ms=_FETCH_TIMEOUT_MS,
                ),
                timeout=_FETCH_TIMEOUT_MS / 1000.0,
            )
        except asyncio.TimeoutError:
            logger.warning("读取 Host 权限配置超过 %dms", _FETCH_TIMEOUT_MS)
            return None
        except Exception as exc:
            logger.warning("读取 Host 权限配置失败: %s", exc)
            return None
        snapshot = HostPolicySnapshot.from_plugin_section(section)
        if snapshot is None:
            logger.warning("Host 返回的 plugin 配置结构异常（缺少 permission 列表），无法确认命令权限")
            return None
        self._snapshot = snapshot
        self._fetched_at = time.monotonic()
        return snapshot
