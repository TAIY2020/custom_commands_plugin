"""自定义命令数据的加载、保存与查询。

``CommandDataManager`` 只关心已解析的作用域名 + 数据；作用域解析由 ``ScopeResolver``
在调用方完成。不依赖 SDK ctx，可独立测试。

写入约束（``ensure_writable`` / ``cleanup_if_unreferenced`` 共用）三层：
1. **保护模式**：数据文件加载失败，拒绝覆盖用户仍可手工修复的原文件。
2. **退役**：on_unload 做完最终保存后置位，超过排空期限仍在跑的旧任务不能再改数据。
3. **代际**：每次 on_load 递增 ``generation``；每个入口（hook / Command / 后台任务）把进入时的
   代际绑进 contextvar，写入与清理时校验。重载失败回滚会对同一个实例再次 on_load 并解除退役，
   若只靠退役布尔值，卸载前开始的旧任务会在回滚后重新获得写权限；代际校验把它们挡住。
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import time
import uuid
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path
from typing import Awaitable, Callable, Dict, Iterator, List, Optional, Tuple

from .common import DEFAULT_MAX_COMMANDS_PER_SCOPE, is_persistable_text

logger = logging.getLogger(__name__)

# 当前执行流所属的插件代际；None 表示未绑定（不做代际校验）。
ACTIVE_GENERATION: ContextVar[Optional[int]] = ContextVar("custom_commands_generation", default=None)


class DataProtectionError(RuntimeError):
    """命令数据当前拒绝写入：保护模式、实例已退役，或本次操作所属代际已过期。"""


class DataSaveError(OSError):
    """序列化或写盘失败（含 ``UnicodeEncodeError`` 等编码错误）。

    继承 ``OSError`` 使 ``add``/``delete`` 的「保存失败即回滚」分支与业务层的
    「未生效，请重试」回执对编码错误同样生效，不再被误归入配额错误。
    """


class CommandQuotaError(ValueError):
    """作用域命令数达到上限。独立类型，避免与其它 ``ValueError`` 混淆。"""


class StaleResourceError(RuntimeError):
    """写入前置条件不成立（如图片已落盘的目录与当前配置不一致），本次提交作废。"""


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
        # 用户原始（可能只是手工编辑出错、仍可修复）的数据，故运行期写入与卸载保存都要拒绝。
        self._load_failed = False
        # 实例退役：on_unload 做完最终保存后置位。单插件重载在 Runner 进程内完成且不等待在途
        # 任务，旧实例上超过排空期限仍在跑的带图添加若继续写入，会在新实例加载快照之后落盘、
        # 再被新实例的下一次保存覆盖。退役后一切写入与清理都被拒绝，业务层回执"请重试"。
        self._retired = False
        # 代际：begin_generation() 每次 on_load 递增；见模块 docstring 第 3 层约束。
        self._generation = 0
        # 回复内容的引用比较键：图片回复 "cc_x.png" 与 "./cc_x.png" 指向同一文件，按原始字符串
        # 比较会把仍被引用的图片误判成孤儿。由调用方（ImageStore）注入规范化函数；未注入时按原样比较。
        self._value_key: Callable[[str], str] = lambda value: value

    def set_value_normalizer(self, normalizer: Callable[[str], str]) -> None:
        """注入回复内容的引用比较键函数（同一文件的不同写法应映射到同一键）。"""
        self._value_key = normalizer

    # ===== 状态 =====

    @property
    def is_protected(self) -> bool:
        """命令数据是否因加载失败进入保护模式。"""
        return self._load_failed

    @property
    def is_retired(self) -> bool:
        """实例是否已退役（正在重载/卸载）。"""
        return self._retired

    @property
    def generation(self) -> int:
        """当前代际号。"""
        return self._generation

    def begin_generation(self) -> int:
        """on_load 起始调用：开启新代际并解除退役。旧代际绑定的执行流此后写入全部被拒。"""
        self._generation += 1
        self._retired = False
        return self._generation

    @contextmanager
    def bind_generation(self, generation: Optional[int] = None) -> Iterator[int]:
        """把 ``generation``（默认当前代际）绑到当前执行流；退出时还原。

        入口（hook / Command handler）在同步位置调用可覆盖其内直接 await 的业务；
        后台任务须在协程体内用捕获值再绑一次——任务上下文是创建时的副本，不随外层还原。
        """
        bound = self._generation if generation is None else generation
        token = ACTIVE_GENERATION.set(bound)
        try:
            yield bound
        finally:
            ACTIVE_GENERATION.reset(token)

    def _writable_reason(self) -> Optional[str]:
        """当前拒绝写入/清理的原因；可写时返回 None。文案可直接回执给用户。"""
        if self._retired:
            return "插件正在重载或卸载，本次修改未保存，请稍后重试"
        bound = ACTIVE_GENERATION.get()
        if bound is not None and bound != self._generation:
            return "插件已重载，本次操作已过期，请重新发送"
        if self._load_failed:
            return (
                "命令数据文件加载失败，已进入保护模式；"
                "请先修复 custom_commands.json 后重载插件，再修改命令"
            )
        return None

    def ensure_writable(self) -> None:
        """当前拒绝写入时抛 ``DataProtectionError``。"""
        reason = self._writable_reason()
        if reason is not None:
            raise DataProtectionError(reason)

    # ===== 加载 =====

    def load(self, data_dir: str, *, create_if_missing: bool = True) -> None:
        """从 ``data_dir``（插件持久数据目录）加载命令数据文件，包含深层数据校验（同步）。

        已存在文件解析/读取失败、或 JSON 能解析但结构语义异常（顶层非 dict、作用域名或
        任一键值非字符串或不可按 UTF-8 落盘）时：先把原文件备份成 ``*.corrupt.<时间戳>.bak``，
        再置 ``_load_failed``（结构异常时仍保留可识别的合法作用域到内存）；据此卸载时的
        最终保存会跳过，避免清洗/重置后的内存静默覆盖用户仍可手工修复的原始数据。

        ``create_if_missing=False`` 用于「旧数据迁移失败」场景：此时目标文件不存在不是首次
        安装，而是旧数据没能搬过来；若照常新建空库，下次启动迁移会因目标已存在而永久跳过。
        该情况下直接进入保护模式，内存为空库、拒绝落盘，等待下次启动重试迁移。

        本方法不碰退役/代际状态（由 ``begin_generation`` 负责）。运行期请用 ``load_async``。
        """
        self.file_path = Path(data_dir) / "custom_commands.json"
        self._load_failed = False

        # 文件不存在：新建空库。新建失败仅记日志，不算"加载失败"——没有原始数据需要保护，
        # 且若据此禁止保存，用户将永远无法落盘任何命令。
        if not self.file_path.exists():
            self.commands = {"global": {}}
            if not create_if_missing:
                self._load_failed = True
                logger.error(
                    "命令数据文件 '%s' 不存在且本次不允许新建（旧数据迁移未成功），"
                    "已进入保护模式：本次运行内存为空库、拒绝落盘，下次启动将重试迁移",
                    self.file_path.name,
                )
                return
            try:
                self._save_sync()
                logger.info("未找到 '%s'，已创建新文件", self.file_path.name)
            except OSError as e:
                logger.error("创建命令数据文件 '%s' 失败: %s", self.file_path.name, e)
            return

        # 文件存在：读取 + 解析。失败则备份原文件并标记 _load_failed，保护原始数据。
        # ValueError 同时涵盖 JSONDecodeError 与 UnicodeDecodeError（文件含无效 UTF-8 字节）。
        try:
            with open(self.file_path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (ValueError, OSError) as e:
            logger.error(
                "加载 '%s' 失败: %s；已备份原文件，卸载时将不会自动保存以免覆盖",
                self.file_path.name, e,
            )
            self._load_failed = True
            self._backup_corrupt_file()
            self.commands = {"global": {}}
            return

        # 深层校验：必须是 Dict[str, Dict[str, str]]，作用域名与每个键值都可按 UTF-8 落盘。
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
            scope_ok = isinstance(scope_key, str) and is_persistable_text(scope_key)
            if scope_ok and isinstance(scope_val, dict) and all(
                isinstance(k, str) and isinstance(v, str)
                and is_persistable_text(k) and is_persistable_text(v)
                for k, v in scope_val.items()
            ):
                validated[scope_key] = scope_val
            else:
                has_corrupt_scope = True
                logger.warning("作用域 %r 数据格式异常（名称或键值非字符串、或含无法落盘的字符），已跳过", scope_key)
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

    async def load_async(self, data_dir: str, *, create_if_missing: bool = True) -> None:
        """持写锁、在线程池里执行 ``load``。

        持锁：重载失败回滚后旧实例再激活时，超期未结束的旧任务可能仍持锁写 ``commands``，
        与加载替换整库互斥。线程池：``open + json.load`` 在网络盘上可达百毫秒级，
        on_load 也在运行期重载时执行，不能卡住同 Runner 的其他插件与 RPC。
        """
        async with self._lock:
            await asyncio.to_thread(self.load, data_dir, create_if_missing=create_if_missing)

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

    # ===== 保存 =====

    def _save_sync(self) -> None:
        """持久化命令数据到 JSON 文件（原子写入，同步版本）。

        使用"写临时文件 + 原子重命名"模式，防止写入过程中崩溃导致数据损坏。
        仅在 load() 初始化与 retire_and_save() 中直接调用，运行时请使用 save()。

        Raises:
            OSError: 写盘失败；序列化/编码失败以 ``DataSaveError``（OSError 子类）抛出，
            使调用方的回滚与回执逻辑对两类失败一视同仁。
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
            self._discard_tmp(tmp_path)
            # 向上抛出：调用方（add/delete）据此回滚内存改动，业务层据此向用户回报失败，
            # 避免"内存已改、磁盘没落、却提示成功"的静默数据不一致。
            raise
        except (ValueError, TypeError) as e:
            # UnicodeEncodeError（孤立代理项）等序列化失败：不属于 OSError，若不转换会绕过
            # 临时文件清理与 add/delete 的内存回滚。
            logger.error("序列化命令数据失败: %s", e)
            self._discard_tmp(tmp_path)
            raise DataSaveError("命令数据包含无法保存的字符") from e

    @staticmethod
    def _discard_tmp(tmp_path: Path) -> None:
        try:
            tmp_path.unlink(missing_ok=True)
        except OSError:
            pass

    async def save(self) -> None:
        """持久化命令数据到 JSON 文件（异步版本，避免阻塞事件循环）。"""
        self.ensure_writable()
        await asyncio.to_thread(self._save_sync)

    async def retire_and_save(self) -> None:
        """卸载收尾：在写锁内先退役、再做最终保存。

        退役标记与最终保存在同一临界区内完成，后到的任何 ``add``/``delete``（例如超过排空
        期限仍在跑的旧任务）拿到锁后会先撞上 ``ensure_writable`` 而拒绝写入，不会在最终
        快照之后再改内存或落盘。保护模式下跳过保存以免覆盖原文件，但同样退役。
        """
        async with self._lock:
            self._retired = True
            if self._load_failed:
                logger.warning(
                    "命令数据曾加载失败（内存为空库），跳过卸载保存以保护原文件；"
                    "请修复 custom_commands.json 后重载插件",
                )
                return
            await asyncio.to_thread(self._save_sync)

    # ===== 查询 =====

    def get(self, trigger: str, scope: str) -> Optional[str]:
        """获取命令回复（优先指定 scope，回退 global）。"""
        if scope in self.commands and trigger in self.commands[scope]:
            return self.commands[scope][trigger]
        if scope != "global" and "global" in self.commands and trigger in self.commands["global"]:
            return self.commands["global"][trigger]
        return None

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

    # ===== 写入 =====

    async def add(self, trigger: str, response: str, scope: str,
                  max_per_scope: int = DEFAULT_MAX_COMMANDS_PER_SCOPE,
                  precondition: Optional[Callable[[], bool]] = None) -> Optional[str]:
        """添加命令到指定作用域（带并发锁和数量上限）。

        覆盖已有触发词时，若旧回复内容替换后已无任何命令引用，会作为「孤儿」返回，
        供调用方按需清理（典型为带图添加自动落盘的图片文件）。

        ``precondition`` 在写锁内、改内存前求值（同步、须快速），返回 False 即放弃本次提交并抛
        ``StaleResourceError``；用于把「图片落盘目录与当前配置一致」这类外部条件纳入事务。

        Returns:
            Optional[str]: 因本次覆盖而失去全部引用的旧回复内容；无需清理时返回 None。

        Raises:
            CommandQuotaError: 当作用域命令数达到上限时抛出。
            DataProtectionError: 保护模式 / 已退役 / 代际过期。
            StaleResourceError: 前置条件不成立。
            OSError: 保存失败（含 DataSaveError），内存已回滚。
        """
        async with self._lock:
            self.ensure_writable()
            if precondition is not None and not precondition():
                raise StaleResourceError("图片目录刚被修改，本次添加未提交，请重新发送")
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
                raise CommandQuotaError(
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
            self.ensure_writable()
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
            self.ensure_writable()
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
        比较经 ``_value_key`` 规范化（纯字符串运算，不碰文件系统）：``cc_x.png`` 与
        ``./cc_x.png`` 视为同一引用。非图片类回复的键即自身，此时只做等值比较、不逐条求键。
        """
        target_key = self._value_key(value)
        compare_keys = target_key != value
        for bucket in self.commands.values():
            for response in bucket.values():
                if response == value:
                    return True
                if compare_keys and self._value_key(response) == target_key:
                    return True
        return False

    async def cleanup_if_unreferenced(
        self,
        value: str,
        deleter: Callable[[], Optional[bool]],
        *,
        additional_reference_check: Optional[Callable[[Tuple[str, ...]], Awaitable[bool]]] = None,
    ) -> bool:
        """锁内原子地判断 ``value`` 是否已成孤儿，若是则调用 ``deleter`` 删除，返回是否执行了删除。

        把"引用计数判断 + 资源删除"合并进同一把写锁，消除 ``_is_referenced`` 判断与外部删除之间的
        TOCTOU 窗口——典型场景：两人并发添加同一张图（同 hash → 同文件名）、其中一个因作用域
        超限失败，若"判断未引用"与"删除文件"之间被另一方写入引用，旧实现会误删对方刚引用的图。
        ``deleter`` 须为快速的同步删除，在锁内执行；返回 False 表示安全条件变化，未执行删除。
        ``additional_reference_check`` 可对去重后的回复快照做异步文件身份复核。等待时仍持数据锁，
        防止新增引用；等待后重新检查代际，避免旧代的复核结果被用于删除新代资源。

        退役 / 代际过期 / 保护模式下**不删**：此时本实例的引用快照可能已经过期（新实例或新代际
        可能刚引用了同一文件），按过期快照删除会误删共享磁盘上仍在使用的图片。宁可留一个可
        事后回收的孤儿文件。
        """
        async with self._lock:
            reason = self._writable_reason()
            if reason is not None:
                logger.info("跳过孤儿清理 %r（%s），文件留待后续回收", value, reason)
                return False
            if self._is_referenced(value):
                return False
            if additional_reference_check is not None:
                responses = tuple(dict.fromkeys(
                    response for commands in self.commands.values() for response in commands.values()
                ))
                if await additional_reference_check(responses):
                    return False
                reason = self._writable_reason()
                if reason is not None:
                    logger.info("文件身份复核后放弃清理 %r（%s）", value, reason)
                    return False
            return deleter() is not False

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
