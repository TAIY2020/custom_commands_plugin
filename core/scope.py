"""自定义命令插件的群组作用域解析。

``ScopeResolver`` 集中管理 ``group_scopes`` 配置的迁移、解析、反向索引与运行时
当前 scope 解析。不依赖 SDK ctx，也不依赖 core 内任何其它模块，是可独立测试、
可被其它插件复用的纯 deep module。设计动机见 docs/adr/0005-scope-resolver.md。
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List

logger = logging.getLogger(__name__)


class ScopeResolver:
    """群组作用域解析器：集中管理 group_scopes 的解析、反向索引、当前 scope 解析。

    替代曾经散在 5 处的逻辑：
        * ``SettingsSection._migrate_legacy_group_scopes`` —— 旧 dict 格式迁移
        * ``CustomCommandsPlugin._parse_group_scopes`` + ``_refresh_group_scopes_cache``
        * ``CommandDataManager._build_reverse_map`` + ``rebuild_reverse_map`` + ``resolve_scope``

    每个调用方需要"当前 scope_id 应落到哪个数据分区"时，只调 ``resolve(scope_id)``。
    新增扩展（按平台隔离、DM scope 等）只动这一个类。
    """

    def __init__(self) -> None:
        self._group_scopes: Dict[str, List[str]] = {}
        self._reverse_map: Dict[str, str] = {}
        self._enable_isolation: bool = False

    # ----- 配置层入口（pydantic model_validator 调用）-----

    @staticmethod
    def migrate_legacy(data: Any) -> Any:
        """旧 dict 格式 group_scopes 转 List[str]；同时清洗每条两端的成对引号。"""
        if not isinstance(data, dict):
            return data
        # 浅拷贝后再改，避免原地修改调用方（pydantic model_validator(before) 的输入 dict）
        # 产生副作用；下面只整体替换 group_scopes 这一个键，浅拷贝即足够。
        data = dict(data)
        legacy = data.get("group_scopes")
        if isinstance(legacy, dict):
            converted: List[str] = []
            for name, ids in legacy.items():
                if not isinstance(ids, (list, tuple)):
                    continue
                ids_str = ",".join(str(item).strip() for item in ids if str(item).strip())
                if str(name).strip() and ids_str:
                    converted.append(f"{str(name).strip()}:{ids_str}")
            data["group_scopes"] = converted
        elif isinstance(legacy, list):
            cleaned: List[str] = []
            for item in legacy:
                if not isinstance(item, str):
                    continue
                stripped = ScopeResolver._strip_paired_quotes(item.strip())
                if stripped:
                    cleaned.append(stripped)
            data["group_scopes"] = cleaned
        return data

    @staticmethod
    def _strip_paired_quotes(text: str) -> str:
        """循环剥掉字符串两端的成对引号（单/双），最多剥两层防御异常输入。"""
        result = text
        for _ in range(2):
            if len(result) >= 2 and result[0] == result[-1] and result[0] in ('"', "'"):
                result = result[1:-1].strip()
            else:
                break
        return result

    # ----- 解析 + 缓存重建 -----

    @staticmethod
    def parse(scopes: List[str]) -> Dict[str, List[str]]:
        """将 List[str] 格式的 group_scopes 解析为 {作用域名: [群号]}。

        每条字符串约定语法 ``"作用域名:群号1,群号2"``。
        非法/空条目静默跳过；重复作用域名合并各条群号并告警（不再静默覆盖）。
        防御性剥掉外层成对引号，防止用户照旧示例输入时残留干扰解析。
        """
        result: Dict[str, List[str]] = {}
        for entry in scopes or []:
            if not isinstance(entry, str):
                continue
            cleaned = ScopeResolver._strip_paired_quotes(entry.strip())
            if ":" not in cleaned:
                continue
            name, _, ids_part = cleaned.partition(":")
            name = name.strip()
            if not name:
                continue
            group_ids = [g.strip() for g in ids_part.split(",") if g.strip()]
            if not group_ids:
                continue
            if name in result:
                # 同名作用域出现多次：合并各条群号（去重保序）而非静默覆盖，并告警提示排查，
                # 避免用户写了两行同名映射却只有最后一行生效、群号神秘丢失。
                logger.warning(
                    "作用域名 '%s' 在 group_scopes 中重复出现，已合并各条目的群号；"
                    "若非有意，请检查配置是否笔误", name,
                )
                for gid in group_ids:
                    if gid not in result[name]:
                        result[name].append(gid)
            else:
                result[name] = group_ids
        return result

    def refresh(self, *, group_scopes: List[str], enable_isolation: bool) -> None:
        """重建解析缓存与反向索引；on_load 与 on_config_update 时调用。

        同一群号被映射到多个作用域时，沿用"后解析者覆盖前者"的行为，但额外打一条
        warning——这种重复几乎都是 group_scopes 配置笔误，静默覆盖会让用户困惑
        "为什么这个群的命令跑到别的作用域去了"。
        """
        self._group_scopes = self.parse(group_scopes)
        reverse_map: Dict[str, str] = {}
        for scope_name, gids in self._group_scopes.items():
            for gid in gids:
                gid_str = str(gid)
                previous = reverse_map.get(gid_str)
                if previous is not None and previous != scope_name:
                    logger.warning(
                        "群号 %s 同时被映射到作用域 '%s' 和 '%s'，将以后者 '%s' 为准；"
                        "请检查 group_scopes 配置是否重复",
                        gid_str, previous, scope_name, scope_name,
                    )
                reverse_map[gid_str] = scope_name
        self._reverse_map = reverse_map
        self._enable_isolation = enable_isolation

    # ----- 当前 scope 解析（运行时热路径）-----

    def resolve(self, scope_id: str) -> str:
        """解析当前 ID 对应的数据作用域。

        优先级：group_scopes 映射 > 群组隔离 > global

        - 命中 group_scopes 映射 → 使用映射的作用域名（不受隔离开关影响）
        - 未命中 + 隔离开启 → 使用 scope_id 自身
        - 未命中 + 隔离关闭 → 使用 ``"global"``

        用 ``refresh()`` 预构建的 ``_reverse_map`` 做 O(1) 查找。
        """
        scope_id_str = str(scope_id)
        mapped = self._reverse_map.get(scope_id_str)
        if mapped is not None:
            return mapped
        return scope_id_str if self._enable_isolation else "global"
