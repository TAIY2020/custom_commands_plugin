"""自定义命令插件 — MaiBot SDK v2

通过聊天命令动态添加、删除、列出和触发自定义回复，支持文本和图片。
支持群组数据隔离与自定义分组映射。

[架构]
- 内置命令（添加/删除/列表/删全局）用 @Command 注册，pattern 是精确的"前缀+关键字"
  (如 ``^\\.列表$``)。get_components() 把 pattern 中的 [^\\w\\s] 占位重写为
  re.escape(prefix)，让 Runner 注册的正则只匹配实际配置的前缀。
- 动态触发（用户 add 的 .xxx）走 @HookHandler 接管 chat.receive.after_process，
  在 Command 调度前介入：命中已注册 trigger 才回复 + abort；未命中直接放行，
  避免抢占其他插件的 Command。早期版本用贪婪 @Command (^{prefix}.+$) 注册
  trigger 是 first-match-wins 的命令路由地雷，已彻底替换。

"""

from maibot_sdk import Command, Field, HookHandler, MaiBotPlugin, PluginConfigBase
from maibot_sdk.types import ErrorPolicy, HookMode, HookOrder
from pydantic import model_validator

import asyncio
import base64
import json
import logging
import os
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# --- 常量 ---

def _load_manifest_version() -> str:
    """从 _manifest.json 读取版本号，保持插件元数据单一来源。"""
    try:
        manifest_path = Path(__file__).parent / "_manifest.json"
        data = json.loads(manifest_path.read_text(encoding="utf-8"))
        version = data.get("version")
        if isinstance(version, str) and version.strip():
            return version.strip()
        logger.warning(
            "_manifest.json 中 version 字段缺失或非法 (%r)，回落到 0.0.0", version,
        )
    except Exception:
        logger.warning("读取 _manifest.json 失败，回落到 0.0.0", exc_info=True)
    return "0.0.0"


PLUGIN_VERSION = _load_manifest_version()
CONFIG_SCHEMA_VERSION = "2.4.0"
DEFAULT_MAX_TRIGGER_LENGTH = 50           # 触发词默认最大长度
DEFAULT_MAX_RESPONSE_LENGTH = 2000        # 回复内容默认最大长度
DEFAULT_MAX_COMMANDS_PER_SCOPE = 500      # 每个作用域默认最大命令数
DEFAULT_MAX_IMAGE_SIZE = 10 * 1024 * 1024  # 图片文件默认最大 10MB
IMAGE_EXTENSIONS = (".png", ".jpg", ".jpeg", ".gif", ".webp")


def _looks_like_image_response(response: str) -> bool:
    """裸文件名形式的图片回复检测——避免把"详情见 chart.png"这类含空格句子误判为图片协议。

    README 约定的图片语法是 ``.问：xx答：hello.png``——即整个 response 就是一个文件名，
    不带空格/换行。任何含空白字符的 response 都视为纯文本，即便以 ``.png`` 结尾。
    """
    return bool(response) and not any(c.isspace() for c in response) and response.lower().endswith(IMAGE_EXTENSIONS)

# Command pattern 中前缀占位符——装饰器声明阶段无法访问 self.config，
# 这里先用通配占位，get_components() 阶段再用 re.escape(配置前缀) 重写为精确匹配。
PREFIX_PLACEHOLDER = r"[^\w\s]"


# --- 作用域解析 ---


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
        非法/空条目静默跳过；重复作用域名后者覆盖前者。
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
            if group_ids:
                result[name] = group_ids
        return result

    def refresh(self, *, group_scopes: List[str], enable_isolation: bool) -> None:
        """重建解析缓存与反向索引；on_load 与 on_config_update 时调用。"""
        self._group_scopes = self.parse(group_scopes)
        self._reverse_map = {
            str(gid): scope_name
            for scope_name, gids in self._group_scopes.items()
            for gid in gids
        }
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


# --- 配置模型 ---


class PluginSection(PluginConfigBase):
    """插件基本配置。"""

    __ui_label__ = "插件设置"

    name: str = Field(
        default="custom_commands_plugin",
        description="插件名称",
        json_schema_extra={"disabled": True}
    )
    config_version: str = Field(
        default=CONFIG_SCHEMA_VERSION,
        description="配置文件版本",
        json_schema_extra={"disabled": True}
    )
    enabled: bool = Field(
        default=True,
        description="是否启用插件",
        json_schema_extra={"label": "启用插件"}
    )


class SettingsSection(PluginConfigBase):
    """命令基本设置。"""

    __ui_label__ = "命令设置"

    command_prefix: str = Field(
        default=".",
        min_length=1,
        max_length=1,
        pattern=r"^[^\w\s]$",
        description=(
            "所有自定义命令的前缀，必须是单个非字母数字、非空白字符（如 . ! / 等）。"
            "空 prefix 会让 startswith 总命中、字母数字 prefix 会让 Command pattern 中的 "
            "[^\\w\\s] 占位永远失配——两类异常值都会让整个命令体系失灵，因此在配置层强约束。"
        ),
        json_schema_extra={"label": "命令前缀", "hint": "如 . 或 ! 或 /"},
    )
    admin_user_ids: List[str] = Field(
        default_factory=list,
        description="拥有添加/删除命令权限的用户 QQ 号列表",
        json_schema_extra={"label": "管理员列表", "hint": "留空 [] 表示任何人都没有权限"},
    )
    image_directory: str = Field(
        default="images",
        description="存放自定义回复图片的目录路径（相对路径基于插件目录解析，也可填写绝对路径）",
        json_schema_extra={"label": "图片目录"}
    )
    enable_group_isolation: bool = Field(
        default=False,
        description="是否开启群组隔离。开启后未映射的群组将使用各自独立的命令库",
        json_schema_extra={"label": "启用群组隔离"}
    )
    group_scopes: List[str] = Field(
        default_factory=list,
        description="群组作用域映射列表。每条格式: 作用域名:群号1,群号2,...",
        json_schema_extra={
            "label": "群组映射",
            "hint": "每条格式：作用域名:群号1,群号2  例：游戏组:111111,222222",
            "placeholder": "游戏组:111111,222222",
        },
    )
    max_trigger_length: int = Field(
        default=DEFAULT_MAX_TRIGGER_LENGTH,
        description="触发词最大长度",
        ge=1,
        le=500,
        json_schema_extra={"label": "触发词最大长度"},
    )
    max_response_length: int = Field(
        default=DEFAULT_MAX_RESPONSE_LENGTH,
        description="回复内容最大长度",
        ge=1,
        le=20000,
        json_schema_extra={"label": "回复内容最大长度"},
    )
    max_commands_per_scope: int = Field(
        default=DEFAULT_MAX_COMMANDS_PER_SCOPE,
        description="每个作用域最大命令数",
        ge=1,
        le=10000,
        json_schema_extra={"label": "单作用域命令上限"},
    )
    max_image_size: int = Field(
        default=DEFAULT_MAX_IMAGE_SIZE,
        description="图片文件最大字节数",
        ge=1024,
        le=100 * 1024 * 1024,
        json_schema_extra={"label": "图片大小上限（字节）"},
    )

    @model_validator(mode="before")
    @classmethod
    def _migrate_legacy_group_scopes(cls, data: Any) -> Any:
        """兼容旧 dict 格式 group_scopes，并清洗每条两端可能多余的引号。

        实现委托给 ``ScopeResolver.migrate_legacy``，与运行时解析共享同一份逻辑。
        """
        return ScopeResolver.migrate_legacy(data)


class CustomCommandsConfig(PluginConfigBase):
    """自定义命令插件完整配置。"""

    plugin: PluginSection = Field(default_factory=PluginSection)
    settings: SettingsSection = Field(default_factory=SettingsSection)


# --- 数据管理器 ---

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

    def load(self, plugin_dir: str) -> None:
        """加载命令数据文件，包含深层数据校验。"""
        self.file_path = Path(plugin_dir) / "custom_commands.json"
        try:
            if self.file_path.exists():
                with open(self.file_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                # 深层校验：必须是 Dict[str, Dict[str, str]]
                if not isinstance(data, dict):
                    logger.warning("命令数据格式异常（非字典），已重置为空")
                    self.commands = {"global": {}}
                else:
                    validated: Dict[str, Dict[str, str]] = {}
                    for scope_key, scope_val in data.items():
                        if isinstance(scope_val, dict) and all(
                            isinstance(k, str) and isinstance(v, str)
                            for k, v in scope_val.items()
                        ):
                            validated[scope_key] = scope_val
                        else:
                            logger.warning("作用域 '%s' 数据格式异常，已跳过", scope_key)
                    self.commands = validated if validated else {"global": {}}
                    if "global" not in self.commands:
                        self.commands["global"] = {}
                total_cmds = sum(len(scope) for scope in self.commands.values())
                logger.info(
                    "成功加载 %d 条自定义命令 (涵盖 %d 个作用域)",
                    total_cmds, len(self.commands),
                )
            else:
                self.commands = {"global": {}}
                self._save_sync()
                logger.info("未找到 '%s'，已创建新文件", self.file_path.name)
        except (json.JSONDecodeError, IOError) as e:
            logger.error("加载 '%s' 失败: %s", self.file_path.name, e)
            self.commands = {"global": {}}

    def _save_sync(self) -> None:
        """持久化命令数据到 JSON 文件（原子写入，同步版本）。

        使用"写临时文件 + 原子重命名"模式，防止写入过程中崩溃导致数据损坏。
        仅在 load() 初始化时同步调用，运行时请使用 save()。
        """
        if not self.file_path:
            return
        tmp_path = self.file_path.with_suffix(".json.tmp")
        try:
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(self.commands, f, ensure_ascii=False, indent=4)
                f.flush()
                os.fsync(f.fileno())
            tmp_path.replace(self.file_path)  # 原子替换
        except IOError as e:
            logger.error("保存命令数据失败: %s", e)
            # 清理可能残留的临时文件
            try:
                tmp_path.unlink(missing_ok=True)
            except OSError:
                pass

    async def save(self) -> None:
        """持久化命令数据到 JSON 文件（异步版本，避免阻塞事件循环）。"""
        await asyncio.to_thread(self._save_sync)

    async def save_locked(self) -> None:
        """加锁后再保存——on_unload 收尾用。

        与 add/delete 共享同一把 ``_lock``，避免插件卸载时的最终 save 与
        正在进行中的 add/delete 写操作 race 同一份 ``self.commands``。
        """
        async with self._lock:
            await self.save()

    def get(self, trigger: str, scope: str) -> Optional[str]:
        """获取命令回复（优先指定 scope，回退 global）。"""
        if scope in self.commands and trigger in self.commands[scope]:
            return self.commands[scope][trigger]
        if scope != "global" and "global" in self.commands and trigger in self.commands["global"]:
            return self.commands["global"][trigger]
        return None

    async def add(self, trigger: str, response: str, scope: str,
                  max_per_scope: int = DEFAULT_MAX_COMMANDS_PER_SCOPE) -> None:
        """添加命令到指定作用域（带并发锁和数量上限）。

        Raises:
            ValueError: 当作用域命令数达到上限时抛出。
        """
        async with self._lock:
            if scope not in self.commands:
                self.commands[scope] = {}
            # 检查命令数量上限（更新已有命令不受限制）
            if (
                trigger not in self.commands[scope]
                and len(self.commands[scope]) >= max_per_scope
            ):
                raise ValueError(
                    f"作用域 '{scope}' 已达到最大命令数 {max_per_scope}"
                )
            self.commands[scope][trigger] = response
            await self.save()

    async def delete(self, trigger: str, scope: str) -> bool:
        """从指定作用域删除命令（带并发锁）。返回是否真的删除。"""
        async with self._lock:
            if scope in self.commands and trigger in self.commands[scope]:
                del self.commands[scope][trigger]
                if not self.commands[scope] and scope != "global":
                    del self.commands[scope]
                await self.save()
                return True
            return False

    async def delete_global(self, trigger: str) -> bool:
        """直接从 global 作用域删除命令（带并发锁）。"""
        async with self._lock:
            if "global" in self.commands and trigger in self.commands["global"]:
                del self.commands["global"][trigger]
                await self.save()
                return True
            return False

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


# --- 主插件类 ---

class CustomCommandsPlugin(MaiBotPlugin):
    """自定义命令插件。

    通过 @Command 注册精确 pattern 的命令处理器，不影响其他插件。
    配置通过 config_model 强类型管理，运行时通过 self.config 读取。
    """

    config_model = CustomCommandsConfig

    def __init__(self) -> None:
        super().__init__()
        self._data_manager = CommandDataManager()
        self._scope_resolver = ScopeResolver()
        self._plugin_dir: str = ""
        self._admin_set: set[str] = set()  # 缓存管理员集合
        self._registered_prefix: Optional[str] = None  # 注册到主程序时使用的 prefix，用于检测热改
        self._self_reload_scheduled: bool = False  # 标记是否已调度自重载任务，防重入

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
        except Exception:
            return components

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
            metadata["command_pattern"] = pattern.replace(PREFIX_PLACEHOLDER, escaped_prefix)
        self._registered_prefix = prefix
        return components

    async def on_load(self) -> None:
        """插件加载时初始化数据管理器和图片目录。"""
        self._plugin_dir = os.path.dirname(os.path.abspath(__file__))

        # 加载命令数据
        self._data_manager.load(self._plugin_dir)

        # 刷新作用域解析器（解析 group_scopes + 反向索引）
        self._scope_resolver.refresh(
            group_scopes=self.config.settings.group_scopes,
            enable_isolation=self.config.settings.enable_group_isolation,
        )

        # 缓存管理员集合
        self._admin_set = {str(uid) for uid in self.config.settings.admin_user_ids}

        # 确保图片目录存在（基于插件目录解析，避免依赖文件夹的具体名称）
        try:
            self._resolve_image_dir().mkdir(parents=True, exist_ok=True)
        except OSError as e:
            logger.warning("创建图片目录失败: %s，图片功能可能不可用", e)

        logger.info("自定义命令插件(v%s)初始化完成。", PLUGIN_VERSION)

    async def on_unload(self) -> None:
        """插件卸载时执行最终保存。

        走 ``save_locked``——与并发进行中的 add/delete 互斥，避免最终保存与
        正在写入的命令同时操作 ``self.commands`` 造成数据不一致。
        """
        await self._data_manager.save_locked()
        logger.info("自定义命令插件已卸载。")

    async def on_config_update(self, scope: str, config_data: dict, version: str) -> None:
        """配置热重载回调。config_model 会自动更新 self.config。"""
        if scope == "self":
            # 刷新管理员缓存
            self._admin_set = {str(uid) for uid in self.config.settings.admin_user_ids}
            # 刷新作用域解析器
            self._scope_resolver.refresh(
                group_scopes=self.config.settings.group_scopes,
                enable_isolation=self.config.settings.enable_group_isolation,
            )
            # 图片目录可能被修改，确保新目录存在
            try:
                self._resolve_image_dir().mkdir(parents=True, exist_ok=True)
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
                asyncio.create_task(
                    self._reload_self_after_prefix_change(self._registered_prefix, new_prefix)
                )

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

            if isinstance(result, dict) and not result.get("success", True):
                logger.error(
                    "自动重载插件 %s 失败：%s；请在插件管理器中手动重载使新前缀生效",
                    plugin_id, result.get("error", "未知错误"),
                )
                return

            success = True
        finally:
            if not success:
                self._self_reload_scheduled = False

    # ===== 权限与作用域辅助方法 =====

    def _resolve_image_dir(self) -> Path:
        """将配置中的 image_directory 解析为绝对 Path。
        相对路径基于插件目录解析，绝对路径直接使用。
        """
        configured = self.config.settings.image_directory
        path = Path(configured)
        if not path.is_absolute():
            base = Path(self._plugin_dir) if self._plugin_dir else Path.cwd()
            path = base / path
        return path.resolve()

    def _check_admin(self, user_id: str) -> bool:
        """检查用户是否有管理员权限（使用缓存集合）。"""
        return str(user_id) in self._admin_set

    def _check_prefix(self, text: str) -> bool:
        """校验消息文本是否以配置的命令前缀开头。"""
        prefix = self.config.settings.command_prefix
        return text.startswith(prefix) if text else False

    def _get_scope_id(self, group_id: str, user_id: str) -> str:
        """获取当前上下文的作用域 ID。"""
        return group_id if group_id else user_id

    def _resolve_safe_image_path(self, response: str) -> Optional[Path]:
        """将回复内容解析为 image_directory 内的安全路径。

        Returns:
            合法时返回解析后的绝对 Path；包含路径穿越或越界时返回 None。
        """
        image_base_dir = self._resolve_image_dir()
        image_path = (image_base_dir / response).resolve()
        try:
            image_path.relative_to(image_base_dir)
        except ValueError:
            return None
        return image_path

    @staticmethod
    def _read_and_encode_image_sync(
        image_path: Path, max_size: int,
    ) -> Tuple[Optional[str], Optional[str]]:
        """同步读图片并 base64 编码；返回 (b64_data, error)。

        在异步路径上必须通过 ``asyncio.to_thread`` 调用——10MB 级别的
        ``read_bytes`` + ``base64.b64encode`` 在事件循环上会阻塞 100ms+。

        Returns:
            (base64 字符串, None) 成功；
            (None, "OVERSIZE:{file_size}") 文件超过 max_size，调用方据此分流出友好错误；
            (None, 其它人类可读字符串) 其它 I/O 失败描述。
        """
        try:
            file_size = image_path.stat().st_size
        except OSError as e:
            return None, f"读取图片文件信息失败: {e}"
        if file_size > max_size:
            return None, f"OVERSIZE:{file_size}"
        try:
            data = image_path.read_bytes()
        except OSError as e:
            return None, f"读取图片失败: {e}"
        return base64.b64encode(data).decode("utf-8"), None

    def _build_list_header_text(self, scope_id: str, current_scope: str) -> str:
        """构造列表头部文本。"""
        header_text = f"📋 自定义命令列表\n当前ID: {scope_id}\n对应作用域: {current_scope}"
        if current_scope == "global":
            header_text += "\n(全局共享模式)"
        elif scope_id != current_scope:
            header_text += "\n(自定义映射分组模式)"
        else:
            header_text += "\n(独立隔离模式)"
        return header_text

    @staticmethod
    def _build_forward_node(text: str) -> Dict[str, Any]:
        """构造 Napcat 合并转发节点。"""
        return {
            "type": "node",
            "data": {
                "name": "自定义命令",
                "uin": "1",
                "content": [{"type": "text", "data": {"text": text}}],
            },
        }

    @staticmethod
    def _parse_forward_target_id(target_id: str, field_name: str) -> int:
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
    def _get_forward_api_error(api_result: Any) -> Optional[str]:
        """提取 Napcat 合并转发 API 的失败信息。

        NapCat 业务失败由 adapter raise → Host _cap_api_call 包装为 success=False，
        原始 NapCat 响应（status / wording 字段）不会出现在 plugin 端。
        """
        if not isinstance(api_result, dict):
            return None
        if api_result.get("success") is False:
            return str(api_result.get("error") or "Napcat 合并转发调用失败")
        return None

    async def _send_list_as_forward(self, header_text: str, list_content: str,
                                    group_id: str, user_id: str,
                                    triggers: Optional[List[str]] = None,
                                    prefix: str = "") -> Optional[str]:
        """使用 Napcat 合并转发发送列表。

        Args:
            triggers: 用于在卡片预览（news）中展示的触发词列表，最多取前 4 条。
            prefix: 命令前缀，用于在 news 文本中拼接。
        """
        message_nodes = [
            self._build_forward_node(header_text),
            self._build_forward_node(list_content),
        ]

        news = [
            {"text": f"{prefix}{t}"} for t in (triggers or [])[:4]
        ] or [{"text": "点击查看完整列表"}]

        if group_id:
            api_result = await self.ctx.api.call(
                "adapter.napcat.message.send_group_forward_msg",
                params={
                    "message_type": "group",
                    "group_id": self._parse_forward_target_id(group_id, "group_id"),
                    "message": message_nodes,
                    "source": "自定义命令",
                    "news": news,
                    "summary": "自定义命令列表",
                    "prompt": "点击查看命令列表",
                },
            )
            return self._get_forward_api_error(api_result)

        api_result = await self.ctx.api.call(
            "adapter.napcat.message.send_private_forward_msg",
            params={
                "message_type": "private",
                "user_id": self._parse_forward_target_id(user_id, "user_id"),
                "message": message_nodes,
                "source": "自定义命令",
                "news": news,
                "summary": "自定义命令列表",
                "prompt": "点击查看命令列表",
            },
        )
        return self._get_forward_api_error(api_result)

    # ===== 命令处理器 =====

    @Command(
        "custom_command_add",
        description="添加自定义命令。格式：<前缀>问：触发词答：回复内容",
        pattern=r"^(?P<prefix>[^\w\s])问：(?P<trigger>.+?)答：(?P<response>.+)$",
    )
    async def handle_add(self, stream_id: str = "", group_id: str = "",
                         user_id: str = "", text: str = "",
                         matched_groups: Optional[dict] = None,
                         plugin_config: Optional[dict] = None, **kwargs):
        """添加命令：<前缀>问：触发词答：回复内容"""
        if not self._check_prefix(text):
            return False, None, 0
        if not matched_groups:
            return False, "缺少匹配参数", 1

        if not self._check_admin(user_id):
            await self.ctx.send.text("❌ 你没有权限执行此管理员命令", stream_id)
            return False, "用户 %s 无权限" % user_id, 1

        trigger = matched_groups.get("trigger", "").strip()
        response = matched_groups.get("response", "").strip()

        if not trigger or not response:
            prefix = self.config.settings.command_prefix
            await self.ctx.send.text(
                f"❌ 命令格式错误，请使用：{prefix}问：触发词答：回复内容", stream_id,
            )
            return False, "格式错误", 1

        # 输入长度校验
        if len(trigger) > self.config.settings.max_trigger_length:
            await self.ctx.send.text(
                f"❌ 触发词过长（最多 {self.config.settings.max_trigger_length} 字符）", stream_id,
            )
            return False, "触发词过长", 1
        if len(response) > self.config.settings.max_response_length:
            await self.ctx.send.text(
                f"❌ 回复内容过长（最多 {self.config.settings.max_response_length} 字符）", stream_id,
            )
            return False, "回复内容过长", 1

        # 触发词命中 hook 的 reserved 列表时，写入会变成幽灵数据：
        # hook 见到 `.<reserved>` 直接 return None 让给 @Command，动态分发永不触达。
        if trigger in self._RESERVED_TRIGGER_EXACT or any(
            trigger.startswith(p) for p in self._RESERVED_TRIGGER_PREFIXES
        ):
            await self.ctx.send.text(
                f"❌ 触发词「{trigger}」与内置命令冲突，请换一个", stream_id,
            )
            return False, "触发词为保留词", 1

        # 图片路径安全校验：在写入前拒绝含路径穿越的回复内容
        if _looks_like_image_response(response):
            if self._resolve_safe_image_path(response) is None:
                logger.warning("添加命令时检测到路径穿越尝试: '%s'", response)
                await self.ctx.send.text("❌ 图片路径不合法，不允许包含路径穿越", stream_id)
                return False, "路径穿越被阻止", 1

        scope_id = self._get_scope_id(group_id, user_id)
        scope_used = self._scope_resolver.resolve(scope_id)

        try:
            await self._data_manager.add(
                trigger, response, scope_used,
                max_per_scope=self.config.settings.max_commands_per_scope,
            )
        except ValueError as exc:
            await self.ctx.send.text(f"❌ {exc}", stream_id)
            return False, "命令数量超限", 1

        if scope_used == "global":
            scope_desc = "（全局共享）"
        elif scope_id != scope_used:
            scope_desc = f"（映射分组: {scope_used}）"
        else:
            scope_desc = f"（ID: {scope_used} 独享）"

        await self.ctx.send.text(
            f"✅ 成功添加自定义命令{scope_desc}！\n触发词：{trigger}\n回复内容：{response}",
            stream_id,
        )
        logger.info("用户 '%s' 在作用域 '%s' 添加命令: '%s'", user_id, scope_used, trigger)
        return True, "添加成功", 1

    @Command(
        "custom_command_delete",
        description="删除自定义命令。格式：<前缀>删：触发词",
        pattern=r"^(?P<prefix>[^\w\s])删：(?P<trigger>.+)$",
    )
    async def handle_delete(self, stream_id: str = "", group_id: str = "",
                            user_id: str = "", text: str = "",
                            matched_groups: Optional[dict] = None,
                            plugin_config: Optional[dict] = None, **kwargs):
        """删除命令：<前缀>删：触发词"""
        if not self._check_prefix(text):
            return False, None, 0
        if not matched_groups:
            return False, "缺少匹配参数", 1

        if not self._check_admin(user_id):
            await self.ctx.send.text("❌ 你没有权限执行此管理员命令", stream_id)
            return False, "用户 %s 无权限" % user_id, 1

        trigger = matched_groups.get("trigger", "").strip()
        scope_id = self._get_scope_id(group_id, user_id)
        current_scope = self._scope_resolver.resolve(scope_id)
        success = await self._data_manager.delete(trigger, current_scope)

        if success:
            await self.ctx.send.text(
                f"✅ 成功删除了自定义命令（作用域: {current_scope}）：'{trigger}'",
                stream_id,
            )
            return True, "删除成功", 1

        msg = f"❌ 未在当前作用域 [{current_scope}] 找到命令：'{trigger}'"

        if (
            current_scope != "global"
            and self._data_manager.has_global(trigger)
        ):
            prefix = self.config.settings.command_prefix
            msg += f"\n💡 提示：这是一个【全局命令】。可使用 {prefix}删全局：{trigger} 来删除。"

        await self.ctx.send.text(msg, stream_id)
        return False, "命令未找到", 1

    @Command(
        "custom_command_delete_global",
        description="删除全局自定义命令。格式：<前缀>删全局：触发词",
        pattern=r"^(?P<prefix>[^\w\s])删全局：(?P<trigger>.+)$",
    )
    async def handle_delete_global(self, stream_id: str = "", group_id: str = "",
                                   user_id: str = "", text: str = "",
                                   matched_groups: Optional[dict] = None,
                                   plugin_config: Optional[dict] = None, **kwargs):
        """删除全局命令：<前缀>删全局：触发词"""
        if not self._check_prefix(text):
            return False, None, 0
        if not matched_groups:
            return False, "缺少匹配参数", 1

        if not self._check_admin(user_id):
            await self.ctx.send.text("❌ 你没有权限执行此管理员命令", stream_id)
            return False, "用户 %s 无权限" % user_id, 1

        trigger = matched_groups.get("trigger", "").strip()
        success = await self._data_manager.delete_global(trigger)

        if success:
            await self.ctx.send.text(
                f"✅ 成功删除了全局自定义命令：'{trigger}'", stream_id,
            )
            logger.info("用户 '%s' 删除全局命令: '%s'", user_id, trigger)
            return True, "全局删除成功", 1

        await self.ctx.send.text(f"❌ 未在全局作用域找到命令：'{trigger}'", stream_id)
        return False, "全局命令未找到", 1

    @Command(
        "custom_command_list",
        description="列出所有可用的自定义命令。格式：<前缀>列表",
        pattern=r"^(?P<prefix>[^\w\s])列表$",
    )
    async def handle_list(self, stream_id: str = "", group_id: str = "",
                          user_id: str = "", text: str = "",
                          plugin_config: Optional[dict] = None,
                          **kwargs):
        """列出命令：<前缀>列表"""
        if not self._check_prefix(text):
            return False, None, 0

        scope_id = self._get_scope_id(group_id, user_id)
        current_scope = self._scope_resolver.resolve(scope_id)
        triggers = self._data_manager.get_triggers_for_scope(current_scope)
        prefix = self.config.settings.command_prefix

        if not triggers:
            await self.ctx.send.text(
                f"🤷‍♀️ 当前作用域 [{current_scope}] 下没有可用的自定义命令",
                stream_id,
            )
        else:
            header_text = self._build_list_header_text(scope_id, current_scope)
            # triggers 已在 get_triggers_for_scope 中排序
            list_content = "\n".join(f"▪️ {prefix}{trigger}" for trigger in triggers)
            try:
                forward_error = await self._send_list_as_forward(
                    header_text, list_content, group_id, user_id,
                    triggers=triggers, prefix=prefix,
                )
            except ValueError as exc:
                logger.error("发送命令列表时目标 ID 非法: %s", exc)
                await self.ctx.send.text(f"❌ 发送命令列表失败：{exc}", stream_id)
                return False, "列表发送失败", 1
            except Exception as exc:
                logger.error("发送命令列表时发生异常: %s", exc, exc_info=True)
                await self.ctx.send.text("❌ 发送命令列表时发生内部错误", stream_id)
                return False, "列表发送失败", 1

            if forward_error:
                logger.error("Napcat 合并转发发送失败: %s", forward_error)
                await self.ctx.send.text(f"❌ 发送命令列表失败：{forward_error}", stream_id)
                return False, "列表发送失败", 1

        return True, "列表已发送", 1

    # ===== 动态触发：HookHandler 路径 =====
    # 改造历史：早期版本把动态触发也用 @Command(pattern=r"^{prefix}(?P<trigger>.+)$")
    # 注册，主程序"第一个 pattern 命中即独占"的分发器会让本插件抢走所有"前缀+任意字符"
    # 的消息，handler 查不到 trigger 时返回 (False, None, 0) 也不能让出——其他插件用
    # 同 prefix 的命令（用户改 prefix = "/" 时与 llm-balance 的 ^/余额$ 冲突）会被
    # 永久屏蔽。Hook 路径按 order 顺次执行，未 abort 就放行，彻底绕过 first-match-wins。

    # 内置命令前缀集合——以这些片段开头的消息留给精确 pattern 的 @Command 处理
    # （否则用户 add 名为"列表"/"问：x答：y"的 trigger 会与内置命令冲突）。
    _RESERVED_TRIGGER_PREFIXES = ("问：", "删：", "删全局：")
    _RESERVED_TRIGGER_EXACT = ("列表",)

    @HookHandler(
        "chat.receive.after_process",
        name="custom_command_dynamic_dispatcher",
        description="动态自定义命令分发：在 Command 调度前接管前缀消息，命中已注册 trigger 则回复+abort，未命中直接放行",
        mode=HookMode.BLOCKING,
        order=HookOrder.EARLY,
        timeout_ms=8000,
        error_policy=ErrorPolicy.SKIP,
    )
    async def handle_dynamic_trigger(
        self, message: Optional[dict] = None, **kwargs
    ) -> Optional[Dict[str, Any]]:
        """动态触发命令的 hook 入口。

        返回 ``{"action": "abort"}`` 表示已处理 + 拦截后续主链；返回 ``None`` 放行
        让消息继续走 Command 调度与 LLM 主链。任何"不该由本插件处理"的消息
        （非群/私聊文本、不带前缀、命中内置命令、未注册 trigger）都必须返回 None。
        """
        if message is None or not isinstance(message, dict):
            return None

        text = str(message.get("processed_plain_text") or "")
        if not text:
            return None

        try:
            prefix = self.config.settings.command_prefix
        except Exception:
            return None
        if not prefix or not text.startswith(prefix):
            return None

        trigger = text[len(prefix):].strip()
        if not trigger:
            return None

        # 内置命令交给精确 pattern 的 @Command 处理，避免与同名动态 trigger 冲突
        if trigger in self._RESERVED_TRIGGER_EXACT:
            return None
        if any(trigger.startswith(p) for p in self._RESERVED_TRIGGER_PREFIXES):
            return None

        msg_info = message.get("message_info") or {}
        user_info = msg_info.get("user_info") or {}
        group_info = msg_info.get("group_info") or {}
        stream_id = str(message.get("session_id") or "")
        group_id = str(group_info.get("group_id") or "")
        user_id = str(user_info.get("user_id") or "")
        if not stream_id:
            return None

        scope_id = self._get_scope_id(group_id, user_id)
        current_scope = self._scope_resolver.resolve(scope_id)
        response_value = self._data_manager.get(trigger, current_scope)
        if response_value is None:
            return None

        # 命中已注册 trigger：发送回复 + abort 后续主链（包括 Command 调度与 LLM）
        if _looks_like_image_response(response_value):
            await self._dispatch_image_response(response_value, stream_id)
        else:
            await self.ctx.send.text(response_value, stream_id)
        return {"action": "abort"}

    async def _dispatch_image_response(self, response_value: str, stream_id: str) -> None:
        """图片回复的完整链路：路径安全 → 大小校验 → 读盘编码 → 发送。

        所有失败路径都向用户回发错误文案——hook 已经决定 abort，错误也算"已处理"。
        """
        image_path = self._resolve_safe_image_path(response_value)
        if image_path is None:
            logger.warning("检测到路径穿越尝试: '%s'", response_value)
            await self.ctx.send.text("❌ 图片路径不合法", stream_id)
            return

        if not image_path.exists():
            # 仅向用户展示文件名，不泄露服务器内部路径
            await self.ctx.send.text(
                f"❌ 找不到图片文件 '{response_value}'", stream_id,
            )
            logger.warning("图片文件不存在: %s", image_path)
            return

        # 同步 I/O（stat + read + base64 编码）丢线程池跑，避免 10MB 级图片阻塞事件循环
        max_image_size = self.config.settings.max_image_size
        b64_img_data, encode_error = await asyncio.to_thread(
            self._read_and_encode_image_sync, image_path, max_image_size,
        )
        if encode_error:
            if encode_error.startswith("OVERSIZE:"):
                try:
                    actual_size = int(encode_error.split(":", 1)[1])
                except ValueError:
                    actual_size = 0
                size_mb = actual_size / (1024 * 1024)
                limit_mb = max_image_size / (1024 * 1024)
                await self.ctx.send.text(
                    f"❌ 图片文件过大（{size_mb:.1f}MB，上限 {limit_mb:.0f}MB）",
                    stream_id,
                )
                return
            logger.error("读取图片失败: %s", encode_error)
            await self.ctx.send.text("❌ 读取图片文件时发生错误", stream_id)
            return

        try:
            await self.ctx.send.image(b64_img_data, stream_id)
        except Exception as e:
            logger.error("发送动态图片失败: %s", e)
            await self.ctx.send.text("❌ 发送图片时发生内部错误", stream_id)


def create_plugin() -> CustomCommandsPlugin:
    """创建自定义命令插件实例。"""
    return CustomCommandsPlugin()
