"""自定义命令插件 — MaiBot SDK v2

通过聊天命令动态添加、删除、列出和触发自定义回复，支持文本和图片。
支持群组数据隔离与自定义分组映射。

[架构]
- 内置命令（添加/删除/列表/删全局）用 @Command 注册，pattern 是精确的"前缀+关键字"
  (如 ``^\\.列表$``)。get_components() 把 pattern 中的 [^\\w\\s] 占位重写为
  re.escape(prefix)，让 Runner 注册的正则只匹配实际配置的前缀。
- Host 用 processed_plain_text 匹配命令正则，而该文本会把被引用消息原文、@昵称、占位符
  用空格拼在一起（Host message.py::process）。引用一条 ``.问：旧答：旧回复`` 再发 ``.列表``，
  Host 匹配用的文本是 ``.问：旧答：旧回复 .列表``，会命中 add 而非 list。两道防线：
  ① chat.receive.after_process hook 发现正文是内置命令且与 processed_plain_text 不一致时，
     改写后 continue，让 Host 按干净正文命中正确的组件（组件启停等约束由 Host 正常执行）；
  ② 四个 @Command handler 不信任 Host 的 matched_groups，用 raw_message 的 text 段重新匹配，
     且**只执行与自身组件一致的命令**——正文不是本组件对应的命令一律放行，不越权借用。
- 动态触发（用户 add 的 .xxx）走同一个 @HookHandler：命中已注册 trigger 才回复 + abort；
  未命中直接放行，避免抢占其他插件的 Command。

[模块拓扑]
本文件为薄入口：装配协作模块 + 生命周期 + get_components 前缀重写 + 5 个装饰器入口
（@Command×4 / @HookHandler×1，body 委托）。@Command/@HookHandler 必须定义在插件类上
才会被 collect_components 收集，故入口留此、业务下沉。具体能力拆在 ``core`` 子包：

* ``core.common``      —— 常量 / 命令关键字与 pattern / 保留词判断 / 消息段解析 / 路由上下文
* ``core.config``      —— 强类型配置 Schema（PluginSection / SettingsSection）
* ``core.scope``       —— ScopeResolver：群组作用域解析（纯，可复用）
* ``core.storage``     —— CommandDataManager：命令数据 CRUD + 原子落盘 + 孤儿判定 + 退役（纯）
* ``core.images``      —— ImageStore：图片安全存取、规范化文件身份、孤儿回收、图片回复投递
* ``core.forward``     —— ListForwardSender：命令列表的合并转发（多适配器路由 + 纯文本降级）
* ``core.service``     —— CommandService：增删查与动态应答的统一编排（写命令唯一入口）
* ``core.dispatcher``  —— DynamicDispatcher：入站消息的动态命令路由（带图添加 / 命中应答 / 后台任务）
"""

from maibot_sdk import Command, HookHandler, MaiBotPlugin
from maibot_sdk.types import ErrorPolicy, HookMode, HookOrder

import asyncio
import json
import logging
import os
import re
import shutil
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .core.common import (
    BUILTIN_PATTERN_ADD,
    BUILTIN_PATTERN_DELETE,
    BUILTIN_PATTERN_DELETE_GLOBAL,
    BUILTIN_PATTERN_LIST,
    PLUGIN_VERSION,
    PREFIX_PLACEHOLDER,
    build_message_route,
    compile_builtin_patterns,
    extract_text_and_images,
    is_reserved_trigger,
)
from .core.config import CustomCommandsConfig
from .core.dispatcher import DynamicDispatcher
from .core.forward import ListForwardSender
from .core.images import ImageStore
from .core.scope import ScopeResolver
from .core.service import CommandService
from .core.storage import CommandDataManager, DataProtectionError

logger = logging.getLogger(__name__)

# on_unload 等待在途后台任务（动态回复投递 / 带图添加落盘）的上限秒数。
_UNLOAD_DRAIN_TIMEOUT_SECONDS = 10.0
# 旧图片目录迁移完成标记（持久数据目录下）。以标记而非「目标目录是否存在」判断是否已迁移：
# 目标目录会被 on_load 的 mkdir 或后续新增图片提前创建，不能作为迁移完成的证据。
_LEGACY_IMAGES_MARKER = ".legacy_images_migrated.json"


# --- 主插件类 ---

class CustomCommandsPlugin(MaiBotPlugin):
    """自定义命令插件。

    通过 @Command 注册精确 pattern 的命令处理器，不影响其他插件。
    配置通过 config_model 强类型管理，运行时通过 self.config 读取。
    入口只做装配与派发，业务在 core 子包的协作模块里（见模块 docstring 拓扑）。
    """

    config_model = CustomCommandsConfig

    def __init__(self) -> None:
        super().__init__()
        # 不绑 plugin 的纯模块（无参构造）
        self._data_manager = CommandDataManager()
        self._scope_resolver = ScopeResolver()
        # 生命周期状态
        self._plugin_dir: str = ""
        self._data_dir: str = ""  # 持久数据目录（ctx.paths.data_dir），命令 JSON 与托管图片的存放基准
        self._admin_set: set[str] = set()  # 缓存管理员集合
        self._registered_prefix: Optional[str] = None  # 注册到主程序时使用的 prefix，用于检测热改
        self._self_reload_scheduled: bool = False  # 标记是否已调度自重载任务，防重入
        # 持有自重载 task 的强引用：asyncio.create_task 返回的 task 若无人引用，
        # 可能在执行中途被 GC 回收（CPython 已知行为），这里存到实例属性兜底。
        # 它不进 BackgroundExecutor：on_unload 若等它会形成自等待死锁。
        self._reload_task: Optional["asyncio.Task[None]"] = None
        # 内置命令正则缓存（按前缀编译），供 match_builtin 用纯文本段重新匹配。
        self._builtin_patterns: Dict[str, "re.Pattern[str]"] = {}
        self._builtin_patterns_prefix: str = ""
        # 4 个持 plugin 弱引用的协作模块；构造仅存 self 引用，相互依赖在调用时延迟解析，
        # 故构造顺序无关（service 用 images/forward/scope/storage，dispatcher 用 service）。
        self._images = ImageStore(self)
        self._forward = ListForwardSender(self)
        self._service = CommandService(self)
        self._dispatcher = DynamicDispatcher(self)
        # 图片回复的引用比较键：让 "cc_x.png" 与 "./cc_x.png" 算同一引用，避免误清孤儿。
        self._data_manager.set_value_normalizer(self._images.reference_key)

    def get_components(self) -> List[Dict[str, Any]]:
        """重写组件收集：将 Command pattern 里的前缀占位符替换为实际配置的前缀。

        装饰器声明阶段无法读 self.config.settings.command_prefix，所以 pattern 里
        先用 [^\\w\\s] 占位（PREFIX_PLACEHOLDER），在此把占位重写成 re.escape(prefix)，
        让 Runner 注册的正则只匹配实际配置的前缀，避免与其他插件的命令在
        "第一个命中独占"的分发逻辑下相互抢匹配。set_plugin_config() 在
        get_components() 之前完成，self.config 在此处已经可用。

        动态触发（用户 add 的 .xxx）不在这里注册——见类 docstring 中 @HookHandler
        chat.receive.after_process 的设计。这里只处理 4 个精确 pattern 的 @Command。

        热重载场景：主程序在 on_config_update 后不会重新调用 get_components；
        on_config_update 检测到 prefix 变化时会通过 ctx.component.reload_plugin
        主动触发本插件重载，让 get_components 重新执行，主程序据此重新编译命令正则。
        """
        components = super().get_components()
        try:
            prefix = self.config.settings.command_prefix
        except Exception as exc:
            # 读不到前缀时不能原样返回：pattern 里的 [^\w\s] 占位仍是通配状态，会匹配任意
            # 单个标点前缀，在 Host first-match-wins 分发下抢占其他插件的命令。宁可本插件
            # 内置命令暂时失效，也要把 4 个 COMMAND 组件剔除，避免误伤他人。
            logger.error(
                "读取 command_prefix 失败: %s；已剔除全部 COMMAND 组件以免占位符通配抢占其他插件命令，"
                "请检查配置后重载插件", exc,
            )
            return [comp for comp in components if comp.get("type") != "COMMAND"]

        escaped_prefix = re.escape(prefix)
        for comp in components:
            if comp.get("type") != "COMMAND":
                continue
            metadata = comp.get("metadata")
            if not isinstance(metadata, dict):
                continue
            pattern = metadata.get("command_pattern", "")
            if not isinstance(pattern, str) or PREFIX_PLACEHOLDER not in pattern:
                continue
            # count=1：只替换开头那个前缀占位符。当前 4 个 pattern 的占位都仅在 ^ 后出现一次，
            # 限定替换次数可防未来 pattern 在 trigger/response 段也用到 [^\w\s] 时被连带误替换。
            metadata["command_pattern"] = pattern.replace(PREFIX_PLACEHOLDER, escaped_prefix, 1)
        self._registered_prefix = prefix
        return components

    # ===== 生命周期 =====

    def _resolve_data_dir(self) -> str:
        """解析持久数据目录（SDK 2.6.0 ctx.paths.data_dir），失败时回退插件目录。

        回退仅为兜底旧 Host / 异常场景：此时行为等同旧版本（数据随插件目录），
        不迁移也不破坏任何数据。
        """
        try:
            data_dir = Path(self.ctx.paths.data_dir)
            data_dir.mkdir(parents=True, exist_ok=True)
            return str(data_dir)
        except Exception as e:
            logger.warning("获取插件持久数据目录失败: %s；回退到插件目录存储", e)
            return self._plugin_dir

    def _migrate_legacy_json_sync(self) -> bool:
        """把旧版本存在插件目录内的 ``custom_commands.json`` 迁移到持久数据目录（同步，须经 to_thread）。

        用 copy 而非 move，旧文件原地保留充当备份快照；先复制到同目录临时名、成功后原子改名，
        避免半途失败留下的残缺目标让后续启动误判为已迁移。目标已存在即跳过，永不反向覆盖。

        Returns:
            bool: 「需要迁移但没能迁成」。True 时调用方不得在持久目录新建空库，
            否则下次启动会因目标已存在而永久跳过迁移。
        """
        if not self._data_dir or self._data_dir == self._plugin_dir:
            return False
        old_json = Path(self._plugin_dir) / "custom_commands.json"
        new_json = Path(self._data_dir) / "custom_commands.json"
        if not old_json.exists() or new_json.exists():
            return False
        tmp_json = new_json.with_name(f".{new_json.name}.migrating.tmp")
        try:
            shutil.copy2(old_json, tmp_json)
            tmp_json.replace(new_json)
            logger.info("已把命令数据从插件目录迁移到持久目录: %s", new_json)
            return False
        except OSError as e:
            logger.error(
                "迁移命令数据失败: %s；本次将以只读空库运行（不新建文件），下次启动重试迁移，旧数据仍在 %s",
                e, old_json,
            )
            try:
                tmp_json.unlink(missing_ok=True)
            except OSError:
                pass
            return True

    def _migrate_legacy_images_sync(self) -> None:
        """把旧版本存在插件目录内的图片目录迁移到持久数据目录（同步，须经 to_thread）。

        完成与否以标记文件为准，不看目标目录是否存在（目标目录会被 mkdir/新增图片提前创建）。
        迁移方式是**逐文件补齐**：目标已有的文件一律不覆盖（保护新产生的数据），缺失的才复制
        （临时名 + 原子改名）。任一文件失败则不写标记，下次启动重跑只补剩余文件，天然可重试。
        用户把 image_directory 配成绝对路径时图片本就不在插件目录内，无需迁移。
        """
        if not self._data_dir or self._data_dir == self._plugin_dir:
            return
        marker = Path(self._data_dir) / _LEGACY_IMAGES_MARKER
        if marker.exists():
            return
        try:
            configured = Path(self.config.settings.image_directory)
        except Exception:
            return
        if configured.is_absolute():
            return
        old_images = (Path(self._plugin_dir) / configured).resolve()
        if not old_images.is_dir():
            return
        new_images = (Path(self._data_dir) / configured).resolve()
        if old_images == new_images:
            return

        copied = skipped = failed = 0
        try:
            new_images.mkdir(parents=True, exist_ok=True)
            for src in sorted(old_images.rglob("*")):
                if src.is_symlink() or src.name.startswith("."):
                    continue
                rel = src.relative_to(old_images)
                dst = new_images / rel
                if src.is_dir():
                    dst.mkdir(parents=True, exist_ok=True)
                    continue
                if not src.is_file():
                    continue
                if dst.exists():
                    skipped += 1
                    continue
                tmp = dst.with_name(f".{dst.name}.migrating.tmp")
                try:
                    dst.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(src, tmp)
                    tmp.replace(dst)
                    copied += 1
                except OSError as e:
                    failed += 1
                    logger.error("迁移图片 %s 失败: %s", rel, e)
                    try:
                        tmp.unlink(missing_ok=True)
                    except OSError:
                        pass
        except OSError as e:
            logger.error("扫描旧图片目录失败: %s；下次启动重试", e)
            return

        if failed:
            logger.error(
                "图片目录迁移未完成：复制 %d、已存在跳过 %d、失败 %d；下次启动将只补齐剩余文件，旧图片仍在 %s",
                copied, skipped, failed, old_images,
            )
            return
        try:
            marker.write_text(
                json.dumps(
                    {
                        "source": str(old_images),
                        "target": str(new_images),
                        "copied": copied,
                        "skipped_existing": skipped,
                        "migrated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
                    },
                    ensure_ascii=False, indent=2,
                ),
                encoding="utf-8",
            )
        except OSError as e:
            logger.warning("写入图片迁移完成标记失败: %s；下次启动会再扫描一遍（不会覆盖已有文件）", e)
        logger.info("图片目录迁移完成：复制 %d、已存在跳过 %d，目标 %s", copied, skipped, new_images)

    async def on_load(self) -> None:
        """插件加载时初始化数据管理器和图片目录。

        重载失败回滚时 Host 会对同一个旧实例再次调用本方法，因此这里的每一步都必须可重入：
        开启新代际（顺带解除退役，卸载前开始的旧任务因代际过期而不能再写）、换新的后台
        执行器、重新加载数据、重建缓存。
        """
        generation = self._data_manager.begin_generation()
        self._dispatcher.reopen()
        if not self._plugin_dir:
            self._plugin_dir = os.path.dirname(os.path.abspath(__file__))
        self._data_dir = self._resolve_data_dir()

        # 旧版本把数据存在插件目录内，插件更新/重装会连数据一起丢；
        # 迁移到 Host 授予的持久目录，再从持久目录加载。
        json_migration_failed = await asyncio.to_thread(self._migrate_legacy_json_sync)

        # 加载命令数据：持写锁 + 线程池（见 load_async）。迁移失败时不新建空库。
        await self._data_manager.load_async(self._data_dir, create_if_missing=not json_migration_failed)

        # 清洗历史/手工编辑残留的"幽灵命令"：命中内置命令保留词的 trigger 永远无法
        # 被动态触发（hook 见到会让位给精确 @Command），留在库里只会污染 .列表 输出。
        # 保护模式下只清内存、不落盘：save() 会抛 DataProtectionError（非 OSError），
        # 若不拦截会让整个 on_load 失败、插件下线。
        ghost_removed = self._data_manager.purge_reserved_triggers(is_reserved_trigger)
        if ghost_removed:
            logger.warning("清理了 %d 条与内置命令冲突的幽灵命令", ghost_removed)
            if self._data_manager.is_protected:
                logger.warning("命令数据处于保护模式，本次清理仅作用于内存，未落盘")
            else:
                try:
                    await self._data_manager.save()
                except (OSError, DataProtectionError) as e:
                    logger.error("保存清理后的命令数据失败: %s；幽灵命令未落盘，下次加载会再次清理", e)

        # 刷新作用域解析器（解析 group_scopes + 反向索引）
        self._scope_resolver.refresh(
            group_scopes=self.config.settings.group_scopes,
            enable_isolation=self.config.settings.enable_group_isolation,
        )

        # 缓存管理员集合与内置命令正则
        self._admin_set = {str(uid) for uid in self.config.settings.admin_user_ids}
        self._refresh_builtin_patterns()

        # 旧图片目录迁移（以标记判断完成，逐文件补齐），然后确保图片目录存在
        await asyncio.to_thread(self._migrate_legacy_images_sync)
        try:
            await asyncio.to_thread(lambda: self._images.resolve_dir().mkdir(parents=True, exist_ok=True))
        except OSError as e:
            logger.warning("创建图片目录失败: %s，图片功能可能不可用", e)

        logger.info("自定义命令插件(v%s)初始化完成（代际 %d）。", PLUGIN_VERSION, generation)

    async def on_unload(self) -> None:
        """插件卸载：停收新业务 → 排空在途后台任务 → 退役数据管理器并做最终保存。

        单插件重载在 Runner 进程内完成且不等待在途 hook/任务。排空带超时；超时后仍在跑的
        旧任务不会被取消（取消等待协程无法终止已在线程池里的文件写入），而是由
        ``retire_and_save`` 在写锁内先退役再保存：此后任何写入与孤儿清理都被拒绝并回执"请重试"，
        旧任务无法在最终快照之后再改数据或按过期快照删图。自重载任务 ``_reload_task`` 不在排空集合内。
        """
        still_running, dropped = await self._dispatcher.background.drain(_UNLOAD_DRAIN_TIMEOUT_SECONDS)
        if still_running or dropped:
            logger.warning(
                "卸载时仍有 %d 个后台任务未在 %.0fs 内结束、%d 个排队任务被放弃；"
                "数据管理器即将退役，它们的后续写入将被拒绝",
                still_running, _UNLOAD_DRAIN_TIMEOUT_SECONDS, dropped,
            )
        try:
            await self._data_manager.retire_and_save()
        except (OSError, DataProtectionError) as e:
            logger.error("卸载时保存命令数据失败: %s", e)
        logger.info("自定义命令插件已卸载。")

    async def on_config_update(self, scope: str, config_data: dict, version: str) -> None:
        """配置热重载回调。config_model 会自动更新 self.config。"""
        if scope == "self":
            # 刷新管理员缓存与内置命令正则
            self._admin_set = {str(uid) for uid in self.config.settings.admin_user_ids}
            self._refresh_builtin_patterns()
            # 刷新作用域解析器
            self._scope_resolver.refresh(
                group_scopes=self.config.settings.group_scopes,
                enable_isolation=self.config.settings.enable_group_isolation,
            )
            # 图片目录可能被修改，确保新目录存在（resolve_dir 缓存键含配置值，自动失效）
            try:
                self._images.resolve_dir().mkdir(parents=True, exist_ok=True)
            except OSError as e:
                logger.warning("热重载后创建图片目录失败: %s", e)
            # 命令前缀变更后，通过 ctx.component.reload_plugin 主动触发自身热重载
            # 让 get_components重新执行，主程序据此重新编译命令正则。
            new_prefix = self.config.settings.command_prefix
            if (
                self._registered_prefix is not None
                and new_prefix != self._registered_prefix
                and not self._self_reload_scheduled
            ):
                self._self_reload_scheduled = True
                # 存引用防止 task 被 GC 提前回收；task 结束后清空引用，避免长期持有已完成 task。
                self._reload_task = asyncio.create_task(
                    self._reload_self_after_prefix_change(self._registered_prefix, new_prefix)
                )
                self._reload_task.add_done_callback(lambda _t: setattr(self, "_reload_task", None))

    async def _reload_self_after_prefix_change(self, old_prefix: str, new_prefix: str) -> None:
        """命令前缀变更后，让 Host 重新加载本插件，让新前缀生效。

        必须先把控制权交回事件循环，让本次 on_config_update 完整返回，再发起 reload，
        否则当前协程会与即将到来的 on_unload 串行执行而存在死锁风险。

        所有失败路径都必须复位 ``_self_reload_scheduled``，否则用户后续再改 prefix
        无法触发 reload；成功路径不复位——reload 完成后旧实例即将被 GC，flag 状态无关紧要。
        """
        await asyncio.sleep(0)
        success = False
        try:
            plugin_id = ""
            try:
                plugin_id = self.ctx.plugin_id
            except Exception as exc:
                logger.error("命令前缀变更后无法获取 plugin_id：%s；请手动重载插件让新前缀生效", exc)
                return

            logger.info(
                "检测到命令前缀已从 %r 修改为 %r，正在自动重载插件 %s 让新前缀生效",
                old_prefix, new_prefix, plugin_id,
            )
            try:
                result = await self.ctx.component.reload_plugin(plugin_id)
            except Exception as exc:
                logger.error(
                    "自动重载插件 %s 失败：%s；请在插件管理器中手动重载使新前缀生效",
                    plugin_id, exc, exc_info=True,
                )
                return

            # 兼容两种返回契约：当前 Host 的 component.reload_plugin 返回 {"success": bool} dict
            # （见主程序 capabilities/components.py::_cap_component_reload_plugin），而 SDK 文档
            # 承诺返回裸 bool。任一形态表示失败时都必须经下方 finally 复位 _self_reload_scheduled，
            # 否则用户后续再改 prefix 将无法再触发自动重载。
            reload_failed = result is False or (
                isinstance(result, dict) and not result.get("success", True)
            )
            if reload_failed:
                error_detail = (
                    result.get("error", "未知错误")
                    if isinstance(result, dict)
                    else "重载未生效（Host 可能已回滚到旧实例）"
                )
                logger.error(
                    "自动重载插件 %s 失败：%s；请在插件管理器中手动重载使新前缀生效",
                    plugin_id, error_detail,
                )
                return

            success = True
        finally:
            if not success:
                self._self_reload_scheduled = False

    def _check_admin(self, user_id: str) -> bool:
        """检查用户是否有管理员权限（使用缓存集合）。

        缓存集合 ``_admin_set`` 随 on_load / on_config_update 刷新，是插件生命周期状态，
        故留在入口类；CommandService 经 ``self._plugin._check_admin`` 调用。
        """
        return str(user_id) in self._admin_set

    # ===== 内置命令：纯文本段重新匹配 =====

    def _refresh_builtin_patterns(self) -> None:
        """按当前前缀（重新）编译四个内置命令正则。"""
        try:
            prefix = self.config.settings.command_prefix
        except Exception:
            return
        if prefix and prefix != self._builtin_patterns_prefix:
            self._builtin_patterns = compile_builtin_patterns(prefix)
            self._builtin_patterns_prefix = prefix

    def match_builtin(self, text: str) -> Optional[Tuple[str, Dict[str, str]]]:
        """``text`` 是否构成一条完整的内置命令；是则返回 (kind, 命名捕获组)，否则 None。

        dispatcher 用它决定是否改写 Host 的匹配文本，``_dispatch_builtin`` 用它重新匹配正文。
        """
        self._refresh_builtin_patterns()
        for kind, pattern in self._builtin_patterns.items():
            match = pattern.match(text)
            if match is not None:
                return kind, match.groupdict()
        return None

    async def _dispatch_builtin(
        self, kind: str, message: Optional[dict], stream_id: str, group_id: str, user_id: str,
    ) -> Tuple[bool, str, int]:
        """四个 @Command 的统一入口：用 raw_message 纯文本段重新匹配，**只执行与 ``kind`` 一致的命令**。

        Host 传入的 matched_groups 来自 processed_plain_text，会被引用消息原文和 @昵称污染
        （见类 docstring）。这里只信任 text 段拼出的正文：
        - 正文恰是 ``kind`` 对应的内置命令 → 用重新匹配得到的捕获组执行。
        - 正文是另一种内置命令 → 不越权执行：Host 只对它命中的那个组件做了启停/聊天级禁用检查，
          借本入口执行别的组件会绕过这些约束（正常情况下 hook 已改写匹配文本，Host 会直接命中
          正确组件，走不到这里）。
        - 正文不是内置命令（例如只是 ``.天气``，或消息没有 text 段）→ 放行。
        放行时返回不拦截，Host 按 continue_process=True 继续主链，等同从未命中命令。
        """
        raw_message = message.get("raw_message") if isinstance(message, dict) else None
        text, _ = extract_text_and_images(raw_message)
        text = text.strip()
        if not text:
            logger.info("Host 命中内置命令 %s 但消息无 text 段（多半来自引用内容），已放行", kind)
            return False, "正文无文本段，放行", 0

        matched = self.match_builtin(text)
        if matched is None:
            logger.info("Host 命中内置命令 %s 但正文 %r 不是内置命令（多半来自引用内容），已放行", kind, text[:60])
            return False, "正文与内置命令不匹配，放行", 0
        matched_kind, groups = matched
        if matched_kind != kind:
            logger.info("Host 命中内置命令 %s 但正文实为 %s，不越权执行，已放行", kind, matched_kind)
            return False, "正文命令与 Host 命中的组件不一致，放行", 0

        # 绑定进入时的代际：命令 RPC 若跨越一次卸载+回滚，写入会因代际过期被拒绝。
        with self._data_manager.bind_generation():
            if kind == "add":
                return await self._service.add_text(groups, stream_id, group_id, user_id)
            if kind == "delete":
                return await self._service.delete(groups, stream_id, group_id, user_id)
            if kind == "delete_global":
                return await self._service.delete_global(groups, stream_id, user_id)
            route = build_message_route(message, stream_id=stream_id, group_id=group_id, user_id=user_id)
            return await self._service.build_list(stream_id, group_id, user_id, route=route)

    # ===== 装饰器入口（body 委托协作模块）=====

    @Command(
        "custom_command_add",
        description="添加自定义命令。格式：<前缀>问：触发词答：回复内容",
        pattern=BUILTIN_PATTERN_ADD,
    )
    async def handle_add(self, stream_id: str = "", group_id: str = "",
                         user_id: str = "", message: Optional[dict] = None, **kwargs):
        """添加命令：<前缀>问：触发词答：回复内容（经 _dispatch_builtin 重新匹配后委托 CommandService）。"""
        return await self._dispatch_builtin("add", message, stream_id, group_id, user_id)

    @Command(
        "custom_command_delete",
        description="删除自定义命令。格式：<前缀>删：触发词",
        pattern=BUILTIN_PATTERN_DELETE,
    )
    async def handle_delete(self, stream_id: str = "", group_id: str = "",
                            user_id: str = "", message: Optional[dict] = None, **kwargs):
        """删除命令：<前缀>删：触发词（经 _dispatch_builtin 重新匹配后委托 CommandService）。"""
        return await self._dispatch_builtin("delete", message, stream_id, group_id, user_id)

    @Command(
        "custom_command_delete_global",
        description="删除全局自定义命令。格式：<前缀>删全局：触发词",
        pattern=BUILTIN_PATTERN_DELETE_GLOBAL,
    )
    async def handle_delete_global(self, stream_id: str = "", group_id: str = "",
                                   user_id: str = "", message: Optional[dict] = None, **kwargs):
        """删除全局命令：<前缀>删全局：触发词（经 _dispatch_builtin 重新匹配后委托 CommandService）。"""
        return await self._dispatch_builtin("delete_global", message, stream_id, group_id, user_id)

    @Command(
        "custom_command_list",
        description="列出所有可用的自定义命令。格式：<前缀>列表",
        pattern=BUILTIN_PATTERN_LIST,
    )
    async def handle_list(self, stream_id: str = "", group_id: str = "",
                          user_id: str = "", message: Optional[dict] = None, **kwargs):
        """列出命令：<前缀>列表（经 _dispatch_builtin 重新匹配后委托 CommandService）。"""
        return await self._dispatch_builtin("list", message, stream_id, group_id, user_id)

    @HookHandler(
        "chat.receive.after_process",
        name="custom_command_dynamic_dispatcher",
        description="动态自定义命令分发：命中已注册 trigger 则回复+abort；内置命令正文被引用/@污染时改写匹配文本后放行；其余直接放行",
        mode=HookMode.BLOCKING,
        order=HookOrder.EARLY,
        timeout_ms=20000,
        error_policy=ErrorPolicy.SKIP,
    )
    async def handle_dynamic_trigger(
        self, message: Optional[dict] = None, **kwargs
    ) -> Optional[Dict[str, Any]]:
        """动态触发命令的 hook 入口（委托 DynamicDispatcher.dispatch）。

        返回 ``{"action": "abort"}`` 表示已接管 + 拦截后续主链；``{"action": "continue",
        "modified_kwargs": ...}`` 表示改写了 Host 的命令匹配文本；``None`` 放行。
        命中后的发送/落盘在后台执行器完成，hook 本身只做判定，避免撞 Host 的 hook 超时；
        执行器拒收时仍 abort 并发有界的短回执。

        带图添加依赖入站 message 的 image/emoji 段携带 ``binary_data_base64``。该字段由 Host
        对 chat.receive.* hook 默认序列化提供（主程序 hook_payloads.serialize_session_message
        固定按 include_binary_data=True 序列化 message），当前 Host 不读取任何组件 metadata
        来决定是否下发二进制数据。
        """
        with self._data_manager.bind_generation() as generation:
            return await self._dispatcher.dispatch(message, kwargs, generation=generation)


def create_plugin() -> CustomCommandsPlugin:
    """创建自定义命令插件实例。"""
    return CustomCommandsPlugin()
