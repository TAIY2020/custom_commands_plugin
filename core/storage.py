"""自定义命令数据的加载、保存与查询。

``CommandDataManager`` 只关心已解析的作用域名 + 数据；作用域解析由 ``ScopeResolver``
在调用方完成。不依赖 SDK ctx，可独立测试。
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import time
import uuid
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

from .common import DEFAULT_MAX_COMMANDS_PER_SCOPE

logger = logging.getLogger(__name__)


class CommandDataManager:
    """自定义命令数据的加载、保存和查询。**只关心已解析的作用域名 + 数据**。

    作用域解析（group_scopes / 隔离开关 → 当前 scope 名）由 ``ScopeResolver``
    在调用方完成；本类不再持有任何反向索引或隔离配置。

    所有写操作通过 asyncio.Lock 保护，防止并发数据竞争。
    文件写入使用"临时文件 + 原子重命名"模式，防止崩溃导致数据损坏。
    """

    def __init__(self) -> None:
        self.commands: Dict[str, Dict[str, str]] = {}
        self.file_path: Optional[Path] = None
        self._lock = asyncio.Lock()
        # 加载已存在文件时发生解析/读取失败 → True。此时内存被重置为空库，若再落盘会覆盖
        # 用户原始（可能只是手工编辑出错、仍可修复）的数据，故卸载收尾的 save_locked 会跳过保存。
        self._load_failed = False

    def load(self, plugin_dir: str) -> None:
        """加载命令数据文件，包含深层数据校验。

        已存在文件解析/读取失败、或 JSON 能解析但结构语义异常（顶层非 dict、或任一作用域
        非 dict/含非字符串键值）时：先把原文件备份成 ``*.corrupt.<时间戳>.bak``，再置
        ``_load_failed``（结构异常时仍保留可识别的合法作用域到内存）；据此 ``save_locked``
        （on_unload 收尾）会跳过保存，避免清洗/重置后的内存静默覆盖用户仍可手工修复的原始数据。
        """
        self.file_path = Path(plugin_dir) / "custom_commands.json"
        self._load_failed = False

        # 文件不存在：新建空库。新建失败仅记日志，不算"加载失败"——没有原始数据需要保护，
        # 且若据此禁止保存，用户将永远无法落盘任何命令。
        if not self.file_path.exists():
            self.commands = {"global": {}}
            try:
                self._save_sync()
                logger.info("未找到 '%s'，已创建新文件", self.file_path.name)
            except OSError as e:
                logger.error("创建命令数据文件 '%s' 失败: %s", self.file_path.name, e)
            return

        # 文件存在：读取 + 解析。失败则备份原文件并标记 _load_failed，保护原始数据。
        try:
            with open(self.file_path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (json.JSONDecodeError, OSError) as e:
            logger.error(
                "加载 '%s' 失败: %s；已备份原文件，卸载时将不会自动保存以免覆盖",
                self.file_path.name, e,
            )
            self._load_failed = True
            self._backup_corrupt_file()
            self.commands = {"global": {}}
            return

        # 深层校验：必须是 Dict[str, Dict[str, str]]。
        # 关键：JSON 能解析但语义结构不符（顶层非 dict、或任一作用域非 dict/含非字符串键值）
        # 时，同样视为"文件已非插件干净格式"——备份原文件并置 _load_failed，让 on_unload 的
        # save_locked 跳过自动保存，避免用"清洗/重置后的版本"静默覆盖用户仍可手工修复的原始
        # 数据（与 JSONDecodeError/OSError 分支同一保护语义；此前这里只 warning 不保护是隐患）。
        if not isinstance(data, dict):
            logger.error(
                "命令数据顶层结构异常（非字典），已备份原文件并进入保护模式，"
                "卸载时将不会自动保存以免覆盖",
            )
            self._load_failed = True
            self._backup_corrupt_file()
            self.commands = {"global": {}}
            return

        validated: Dict[str, Dict[str, str]] = {}
        has_corrupt_scope = False
        for scope_key, scope_val in data.items():
            if isinstance(scope_val, dict) and all(
                isinstance(k, str) and isinstance(v, str)
                for k, v in scope_val.items()
            ):
                validated[scope_key] = scope_val
            else:
                has_corrupt_scope = True
                logger.warning("作用域 '%s' 数据格式异常，已跳过", scope_key)
        # 任一作用域被判损坏：合法作用域仍载入内存供本次运行使用，但备份原文件并进入保护
        # 模式，避免卸载自动保存时把损坏作用域从磁盘上静默抹掉（用户可能想手工修复它们）。
        if has_corrupt_scope:
            logger.error(
                "部分作用域数据结构异常，已备份原文件并进入保护模式，卸载时将不会自动保存以免覆盖",
            )
            self._load_failed = True
            self._backup_corrupt_file()
        self.commands = validated if validated else {"global": {}}
        if "global" not in self.commands:
            self.commands["global"] = {}
        total_cmds = sum(len(scope) for scope in self.commands.values())
        logger.info(
            "成功加载 %d 条自定义命令 (涵盖 %d 个作用域)",
            total_cmds, len(self.commands),
        )

    def _backup_corrupt_file(self) -> None:
        """把无法解析的命令数据文件复制一份带时间戳的备份，保留原文件以便用户原地修复。

        用 copy 而非 move：原文件保持不动，配合 ``_load_failed`` 跳过卸载保存，
        用户可直接修复 ``custom_commands.json`` 后重载恢复；``.bak`` 是额外的冗余快照。
        """
        if not self.file_path or not self.file_path.exists():
            return
        try:
            timestamp = time.strftime("%Y%m%d_%H%M%S")
            backup_path = self.file_path.with_name(
                f"{self.file_path.name}.corrupt.{timestamp}.bak"
            )
            shutil.copy2(self.file_path, backup_path)
            logger.warning("已备份疑似损坏的命令数据到 %s", backup_path.name)
        except OSError as e:
            logger.error("备份损坏的命令数据文件失败: %s", e)

    def _save_sync(self) -> None:
        """持久化命令数据到 JSON 文件（原子写入，同步版本）。

        使用"写临时文件 + 原子重命名"模式，防止写入过程中崩溃导致数据损坏。
        仅在 load() 初始化时同步调用，运行时请使用 save()。
        """
        if not self.file_path:
            return
        # 临时文件名加入随机后缀，避免热重载/跨实例并发保存时多个进程争用同一个固定 .tmp
        # 导致写入交错损坏（与 images.py 图片落盘同一防御）。前缀 "." 让它在目录里不显眼。
        tmp_path = self.file_path.with_name(
            f".{self.file_path.name}.{uuid.uuid4().hex}.tmp"
        )
        try:
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(self.commands, f, ensure_ascii=False, indent=4)
                f.flush()
                os.fsync(f.fileno())
            tmp_path.replace(self.file_path)  # 原子替换
        except OSError as e:
            logger.error("保存命令数据失败: %s", e)
            # 清理可能残留的临时文件
            try:
                tmp_path.unlink(missing_ok=True)
            except OSError:
                pass
            # 向上抛出：调用方（add/delete）据此回滚内存改动，业务层据此向用户回报失败，
            # 避免"内存已改、磁盘没落、却提示成功"的静默数据不一致。
            raise

    async def save(self) -> None:
        """持久化命令数据到 JSON 文件（异步版本，避免阻塞事件循环）。"""
        await asyncio.to_thread(self._save_sync)

    async def save_locked(self) -> None:
        """加锁后再保存——on_unload 收尾用。

        与 add/delete 共享同一把 ``_lock``，避免插件卸载时的最终 save 与
        正在进行中的 add/delete 写操作 race 同一份 ``self.commands``。
        """
        async with self._lock:
            if self._load_failed:
                logger.warning(
                    "命令数据曾加载失败（内存为空库），跳过卸载保存以保护原文件；"
                    "请修复 custom_commands.json 后重载插件",
                )
                return
            await self.save()

    def get(self, trigger: str, scope: str) -> Optional[str]:
        """获取命令回复（优先指定 scope，回退 global）。"""
        if scope in self.commands and trigger in self.commands[scope]:
            return self.commands[scope][trigger]
        if scope != "global" and "global" in self.commands and trigger in self.commands["global"]:
            return self.commands["global"][trigger]
        return None

    async def add(self, trigger: str, response: str, scope: str,
                  max_per_scope: int = DEFAULT_MAX_COMMANDS_PER_SCOPE) -> Optional[str]:
        """添加命令到指定作用域（带并发锁和数量上限）。

        覆盖已有触发词时，若旧回复内容替换后已无任何命令引用，会作为「孤儿」返回，
        供调用方按需清理（典型为带图添加自动落盘的图片文件）。

        Returns:
            Optional[str]: 因本次覆盖而失去全部引用的旧回复内容；无需清理时返回 None。

        Raises:
            ValueError: 当作用域命令数达到上限时抛出。
        """
        async with self._lock:
            scope_created = scope not in self.commands
            if scope_created:
                self.commands[scope] = {}
            # 检查命令数量上限（更新已有命令不受限制）
            if (
                trigger not in self.commands[scope]
                and len(self.commands[scope]) >= max_per_scope
            ):
                if scope_created:
                    del self.commands[scope]  # 回滚本次为校验而新建的空作用域
                raise ValueError(
                    f"作用域 '{scope}' 已达到最大命令数 {max_per_scope}"
                )
            old_value = self.commands[scope].get(trigger)
            self.commands[scope][trigger] = response
            try:
                await self.save()
            except OSError:
                # 保存失败：回滚本次内存改动，使 add 要么完整成功、要么无副作用；
                # 异常继续上抛，业务层据此提示用户"未持久化"，不再误报成功。
                if old_value is None:
                    self.commands[scope].pop(trigger, None)
                    if scope_created and not self.commands[scope]:
                        del self.commands[scope]
                else:
                    self.commands[scope][trigger] = old_value
                raise
            # 覆盖且新旧内容不同时，旧内容可能变孤儿；引用计数须在写入新值之后、锁内统计，
            # 避免与并发写操作看到不一致快照（同一图片 hash 去重后可被多个触发词共享）。
            if old_value is not None and old_value != response and not self._is_referenced(old_value):
                return old_value
            return None

    async def delete(self, trigger: str, scope: str) -> Tuple[bool, Optional[str]]:
        """从指定作用域删除命令（带并发锁）。

        Returns:
            Tuple[bool, Optional[str]]: ``(是否真的删除, 删除后失去全部引用的旧回复内容)``。
            第二项供调用方清理孤儿资源；仍被其他命令引用或未删除时为 None。
        """
        async with self._lock:
            if scope in self.commands and trigger in self.commands[scope]:
                old_value = self.commands[scope][trigger]
                del self.commands[scope][trigger]
                scope_removed = not self.commands[scope] and scope != "global"
                if scope_removed:
                    del self.commands[scope]
                try:
                    await self.save()
                except OSError:
                    # 保存失败：撤销删除（必要时重建被清掉的空作用域），异常上抛由业务层回报。
                    if scope not in self.commands:
                        self.commands[scope] = {}
                    self.commands[scope][trigger] = old_value
                    raise
                orphan = old_value if not self._is_referenced(old_value) else None
                return True, orphan
            return False, None

    async def delete_global(self, trigger: str) -> Tuple[bool, Optional[str]]:
        """直接从 global 作用域删除命令（带并发锁）。

        Returns:
            Tuple[bool, Optional[str]]: ``(是否真的删除, 删除后失去全部引用的旧回复内容)``。
        """
        async with self._lock:
            if "global" in self.commands and trigger in self.commands["global"]:
                old_value = self.commands["global"][trigger]
                del self.commands["global"][trigger]
                try:
                    await self.save()
                except OSError:
                    # 保存失败：撤销删除，异常上抛由业务层回报"未持久化"。
                    self.commands["global"][trigger] = old_value
                    raise
                orphan = old_value if not self._is_referenced(old_value) else None
                return True, orphan
            return False, None

    def _is_referenced(self, value: str) -> bool:
        """是否仍有任意作用域的任意触发词引用 ``value`` 作为回复内容。

        用于删除/覆盖命令后判断旧回复内容（典型为带图添加落盘的图片文件名）是否已成孤儿。
        必须在 ``_lock`` 持有期间、且记录变更完成后调用，确保与并发写操作看到一致快照。
        同一张图片经 hash 去重可被多个触发词共享，因此只有计数归零才算孤儿。
        """
        for bucket in self.commands.values():
            for response in bucket.values():
                if response == value:
                    return True
        return False

    async def cleanup_if_unreferenced(self, value: str, deleter: Callable[[], None]) -> bool:
        """锁内原子地判断 ``value`` 是否已成孤儿，若是则调用 ``deleter`` 删除，返回是否执行了删除。

        把"引用计数判断 + 资源删除"合并进同一把写锁，消除 ``_is_referenced`` 判断与外部删除之间的
        TOCTOU 窗口——典型场景：两人并发添加同一张图（同 hash → 同文件名）、其中一个因作用域
        超限失败，若"判断未引用"与"删除文件"之间被另一方写入引用，旧实现会误删对方刚引用的图。
        ``deleter`` 须为快速的同步删除（如 ``os.unlink``，自行吞掉 missing/IO 异常），在锁内执行。
        """
        async with self._lock:
            if self._is_referenced(value):
                return False
            deleter()
            return True

    def has_global(self, trigger: str) -> bool:
        """global 作用域是否存在某个 trigger（提示消息用，避免外部窥探 commands dict）。"""
        return "global" in self.commands and trigger in self.commands["global"]

    def get_triggers_for_scope(self, scope: str) -> List[str]:
        """获取指定作用域下可见的所有触发词（本域独有 + global 共享），已排序。"""
        triggers: set[str] = set()
        if "global" in self.commands:
            triggers.update(self.commands["global"].keys())
        if scope in self.commands:
            triggers.update(self.commands[scope].keys())
        return sorted(triggers)

    def purge_reserved_triggers(self, is_reserved: Callable[[str], bool]) -> int:
        """清除所有作用域中命中保留词的"幽灵"trigger，返回清除条数（仅改内存，不落盘）。

        历史数据或手工编辑 ``custom_commands.json`` 可能写入与内置命令同名的 trigger，
        这类 trigger 永远无法通过动态触发访问，只会污染 ``.列表`` 输出。
        清空后的非 global 作用域一并移除，保持与 load() 后结构一致。
        """
        removed = 0
        for scope in list(self.commands.keys()):
            bucket = self.commands[scope]
            ghost_triggers = [trigger for trigger in bucket if is_reserved(trigger)]
            for trigger in ghost_triggers:
                del bucket[trigger]
                removed += 1
            if not bucket and scope != "global":
                del self.commands[scope]
        return removed
