"""自定义命令插件 — MaiBot SDK v2

通过聊天命令动态添加、删除、列出和触发自定义回复，支持文本和图片。
支持群组数据隔离与自定义分组映射。

所有命令使用 @Command 装饰器注册，pattern 为精确匹配，不影响其他插件。
命令前缀默认为 "."，可在 config.toml 中配置。
由于 @Command 的 pattern 在注册时静态编译，正则使用 [^\\w\\s] 匹配前缀位置，
再在 handler 内部通过 self.config 校验实际前缀。
"""

from maibot_sdk import Command, Field, MaiBotPlugin, PluginConfigBase

import asyncio
import base64
import json
import logging
import os
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger("plugin.custom_commands")

# --- 常量 ---

PLUGIN_VERSION = "2.2.1"
DEFAULT_MAX_TRIGGER_LENGTH = 50           # 触发词默认最大长度
DEFAULT_MAX_RESPONSE_LENGTH = 2000        # 回复内容默认最大长度
DEFAULT_MAX_COMMANDS_PER_SCOPE = 500      # 每个作用域默认最大命令数
DEFAULT_MAX_IMAGE_SIZE = 10 * 1024 * 1024  # 图片文件默认最大 10MB
IMAGE_EXTENSIONS = (".png", ".jpg", ".jpeg", ".gif", ".webp")


# --- 配置模型 ---

class PluginSection(PluginConfigBase):
    """插件基本配置。"""

    __ui_label__ = "插件设置"

    name: str = Field(
        default="custom_commands_plugin",
        description="插件名称",
        json_schema_extra={"disabled": True}
    )
    version: str = Field(
        default=PLUGIN_VERSION,
        description="插件版本",
        json_schema_extra={"disabled": True}
    )
    config_version: str = Field(
        default=PLUGIN_VERSION,
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
        description="所有自定义命令的前缀",
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
    group_scopes: Dict[str, List[str]] = Field(
        default={ },
        description="群组作用域映射。键为作用域名称，值为该作用域下的群号列表",
        json_schema_extra={
            "label": "群组映射",
            "hint": '在 [settings.group_scopes] 段下添加: "游戏组" = ["111111", "222222"]',
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


class CustomCommandsConfig(PluginConfigBase):
    """自定义命令插件完整配置。"""

    plugin: PluginSection = Field(default_factory=PluginSection)
    settings: SettingsSection = Field(default_factory=SettingsSection)


# --- 数据管理器 ---

class CommandDataManager:
    """自定义命令数据的加载、保存和查询。支持多作用域管理。

    所有写操作通过 asyncio.Lock 保护，防止并发数据竞争。
    文件写入使用"临时文件 + 原子重命名"模式，防止崩溃导致数据损坏。
    """

    def __init__(self) -> None:
        self.commands: Dict[str, Dict[str, str]] = {}
        self.file_path: Optional[Path] = None
        self._lock = asyncio.Lock()
        self._reverse_map: Dict[str, str] = {}  # 群号 → 作用域名 的反向索引缓存

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

    def rebuild_reverse_map(self, group_scopes: Dict[str, List[str]]) -> None:
        """重建反向索引缓存。在配置加载或热重载时调用。"""
        self._reverse_map = self._build_reverse_map(group_scopes)

    def resolve_scope(self, scope_id: str, enable_isolation: bool,
                      group_scopes: Dict[str, List[str]]) -> str:
        """解析当前 ID 对应的数据作用域。

        优先级：group_scopes 映射 > 群组隔离 > global

        使用 rebuild_reverse_map() 预构建的缓存进行 O(1) 查找。
        """
        scope_id_str = str(scope_id)
        mapped = self._reverse_map.get(scope_id_str)
        if mapped is not None:
            return mapped
        return scope_id_str if enable_isolation else "global"

    @staticmethod
    def _build_reverse_map(group_scopes: Dict[str, List[str]]) -> Dict[str, str]:
        """将 {作用域名: [群号列表]} 转换为 {群号: 作用域名} 的反向索引。"""
        reverse: Dict[str, str] = {}
        for scope_name, group_ids in group_scopes.items():
            for gid in group_ids:
                reverse[str(gid)] = scope_name
        return reverse

    def get(self, trigger: str, scope_id: str, enable_isolation: bool,
            group_scopes: Dict[str, List[str]]) -> Optional[str]:
        """获取命令回复（优先当前 Scope，回退 global）。"""
        target_scope = self.resolve_scope(scope_id, enable_isolation, group_scopes)
        if target_scope in self.commands and trigger in self.commands[target_scope]:
            return self.commands[target_scope][trigger]
        if target_scope != "global" and "global" in self.commands and trigger in self.commands["global"]:
            return self.commands["global"][trigger]
        return None

    async def add(self, trigger: str, response: str, scope_id: str,
                  enable_isolation: bool, group_scopes: Dict[str, List[str]],
                  max_per_scope: int = DEFAULT_MAX_COMMANDS_PER_SCOPE) -> str:
        """添加命令到计算出的作用域（带并发锁和数量上限）。

        Raises:
            ValueError: 当作用域命令数达到上限时抛出。
        """
        async with self._lock:
            scope = self.resolve_scope(scope_id, enable_isolation, group_scopes)
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
            return scope

    async def delete(self, trigger: str, scope_id: str, enable_isolation: bool,
                     group_scopes: Dict[str, List[str]]) -> Tuple[bool, str]:
        """从当前作用域删除命令（带并发锁）。"""
        async with self._lock:
            scope = self.resolve_scope(scope_id, enable_isolation, group_scopes)
            if scope in self.commands and trigger in self.commands[scope]:
                del self.commands[scope][trigger]
                if not self.commands[scope] and scope != "global":
                    del self.commands[scope]
                await self.save()
                return True, scope
            return False, scope

    async def delete_global(self, trigger: str) -> bool:
        """直接从 global 作用域删除命令（带并发锁）。"""
        async with self._lock:
            if "global" in self.commands and trigger in self.commands["global"]:
                del self.commands["global"][trigger]
                await self.save()
                return True
            return False

    def get_triggers_for_scope(self, scope_id: str, enable_isolation: bool,
                               group_scopes: Dict[str, List[str]]) -> List[str]:
        """获取当前环境下可见的所有触发词（本群独有 + 全局共享），已排序。"""
        triggers: set[str] = set()
        if "global" in self.commands:
            triggers.update(self.commands["global"].keys())
        target_scope = self.resolve_scope(scope_id, enable_isolation, group_scopes)
        if target_scope in self.commands:
            triggers.update(self.commands[target_scope].keys())
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
        self._plugin_dir: str = ""
        self._admin_set: set[str] = set()  # 缓存管理员集合

    async def on_load(self) -> None:
        """插件加载时初始化数据管理器和图片目录。"""
        self._plugin_dir = os.path.dirname(os.path.abspath(__file__))

        # 加载命令数据
        self._data_manager.load(self._plugin_dir)

        # 构建反向索引缓存
        self._data_manager.rebuild_reverse_map(self.config.settings.group_scopes)

        # 缓存管理员集合
        self._admin_set = {str(uid) for uid in self.config.settings.admin_user_ids}

        # 确保图片目录存在（基于插件目录解析，避免依赖文件夹的具体名称）
        try:
            self._resolve_image_dir().mkdir(parents=True, exist_ok=True)
        except OSError as e:
            logger.warning("创建图片目录失败: %s，图片功能可能不可用", e)

        logger.info("自定义命令插件(v%s)初始化完成。", PLUGIN_VERSION)

    async def on_unload(self) -> None:
        """插件卸载时执行最终保存。"""
        await self._data_manager.save()
        logger.info("自定义命令插件已卸载。")

    async def on_config_update(self, scope: str, config_data: dict, version: str) -> None:
        """配置热重载回调。config_model 会自动更新 self.config。"""
        if scope == "self":
            # 刷新管理员缓存
            self._admin_set = {str(uid) for uid in self.config.settings.admin_user_ids}
            # 刷新反向索引缓存
            self._data_manager.rebuild_reverse_map(self.config.settings.group_scopes)
            # 图片目录可能被修改，确保新目录存在
            try:
                self._resolve_image_dir().mkdir(parents=True, exist_ok=True)
            except OSError as e:
                logger.warning("热重载后创建图片目录失败: %s", e)

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
        """提取 Napcat 合并转发 API 的失败信息。"""
        if not isinstance(api_result, dict):
            return None
        if api_result.get("success") is False:
            return str(api_result.get("error") or "Napcat 合并转发调用失败")

        status = str(api_result.get("status") or "").lower()
        if status and status != "ok":
            return str(api_result.get("wording") or api_result.get("message") or "Napcat 合并转发调用失败")
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
            return False, None, False
        if not matched_groups:
            return False, "缺少匹配参数", True

        if not self._check_admin(user_id):
            await self.ctx.send.text("❌ 你没有权限执行此管理员命令", stream_id)
            return False, "用户 %s 无权限" % user_id, True

        trigger = matched_groups.get("trigger", "").strip()
        response = matched_groups.get("response", "").strip()

        if not trigger or not response:
            prefix = self.config.settings.command_prefix
            await self.ctx.send.text(
                f"❌ 命令格式错误，请使用：{prefix}问：触发词答：回复内容", stream_id,
            )
            return False, "格式错误", True

        # 输入长度校验
        if len(trigger) > self.config.settings.max_trigger_length:
            await self.ctx.send.text(
                f"❌ 触发词过长（最多 {self.config.settings.max_trigger_length} 字符）", stream_id,
            )
            return False, "触发词过长", True
        if len(response) > self.config.settings.max_response_length:
            await self.ctx.send.text(
                f"❌ 回复内容过长（最多 {self.config.settings.max_response_length} 字符）", stream_id,
            )
            return False, "回复内容过长", True

        # 图片路径安全校验：在写入前拒绝含路径穿越的回复内容
        if response.lower().endswith(IMAGE_EXTENSIONS):
            if self._resolve_safe_image_path(response) is None:
                logger.warning("添加命令时检测到路径穿越尝试: '%s'", response)
                await self.ctx.send.text("❌ 图片路径不合法，不允许包含路径穿越", stream_id)
                return False, "路径穿越被阻止", True

        scope_id = self._get_scope_id(group_id, user_id)
        isolation = self.config.settings.enable_group_isolation
        group_scopes = self.config.settings.group_scopes

        try:
            scope_used = await self._data_manager.add(
                trigger, response, scope_id, isolation, group_scopes,
                max_per_scope=self.config.settings.max_commands_per_scope,
            )
        except ValueError as exc:
            await self.ctx.send.text(f"❌ {exc}", stream_id)
            return False, "命令数量超限", True

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
        return True, "添加成功", True

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
            return False, None, False
        if not matched_groups:
            return False, "缺少匹配参数", True

        if not self._check_admin(user_id):
            await self.ctx.send.text("❌ 你没有权限执行此管理员命令", stream_id)
            return False, "用户 %s 无权限" % user_id, True

        trigger = matched_groups.get("trigger", "").strip()
        scope_id = self._get_scope_id(group_id, user_id)
        isolation = self.config.settings.enable_group_isolation
        group_scopes = self.config.settings.group_scopes
        success, scope_used = await self._data_manager.delete(
            trigger, scope_id, isolation, group_scopes,
        )

        if success:
            await self.ctx.send.text(
                f"✅ 成功删除了自定义命令（作用域: {scope_used}）：'{trigger}'",
                stream_id,
            )
            return True, "删除成功", True

        current_scope = self._data_manager.resolve_scope(scope_id, isolation, group_scopes)
        msg = f"❌ 未在当前作用域 [{current_scope}] 找到命令：'{trigger}'"

        if (
            current_scope != "global"
            and "global" in self._data_manager.commands
            and trigger in self._data_manager.commands["global"]
        ):
            prefix = self.config.settings.command_prefix
            msg += f"\n💡 提示：这是一个【全局命令】。可使用 {prefix}删全局：{trigger} 来删除。"

        await self.ctx.send.text(msg, stream_id)
        return False, "命令未找到", True

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
            return False, None, False
        if not matched_groups:
            return False, "缺少匹配参数", True

        if not self._check_admin(user_id):
            await self.ctx.send.text("❌ 你没有权限执行此管理员命令", stream_id)
            return False, "用户 %s 无权限" % user_id, True

        trigger = matched_groups.get("trigger", "").strip()
        success = await self._data_manager.delete_global(trigger)

        if success:
            await self.ctx.send.text(
                f"✅ 成功删除了全局自定义命令：'{trigger}'", stream_id,
            )
            logger.info("用户 '%s' 删除全局命令: '%s'", user_id, trigger)
            return True, "全局删除成功", True

        await self.ctx.send.text(f"❌ 未在全局作用域找到命令：'{trigger}'", stream_id)
        return False, "全局命令未找到", True

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
            return False, None, False

        scope_id = self._get_scope_id(group_id, user_id)
        isolation = self.config.settings.enable_group_isolation
        group_scopes = self.config.settings.group_scopes
        triggers = self._data_manager.get_triggers_for_scope(scope_id, isolation, group_scopes)
        current_scope = self._data_manager.resolve_scope(scope_id, isolation, group_scopes)
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
                return False, "列表发送失败", True
            except Exception as exc:
                logger.error("发送命令列表时发生异常: %s", exc, exc_info=True)
                await self.ctx.send.text("❌ 发送命令列表时发生内部错误", stream_id)
                return False, "列表发送失败", True

            if forward_error:
                logger.error("Napcat 合并转发发送失败: %s", forward_error)
                await self.ctx.send.text(f"❌ 发送命令列表失败：{forward_error}", stream_id)
                return False, "列表发送失败", True

        return True, "列表已发送", True

    @Command(
        "custom_command_trigger",
        description="处理动态自定义命令（<前缀>触发词 → 查找并回复）",
        pattern=r"^(?P<prefix>[^\w\s])(?P<trigger>.+)$",
    )
    async def handle_trigger(self, stream_id: str = "", group_id: str = "",
                             user_id: str = "", text: str = "",
                             matched_groups: Optional[dict] = None,
                             plugin_config: Optional[dict] = None, **kwargs):
        """动态触发命令：<前缀>触发词 → 查找并回复。"""
        if not self._check_prefix(text):
            return False, None, False
        if not matched_groups:
            return False, None, False

        trigger = matched_groups.get("trigger", "").strip()
        if not trigger:
            return False, None, False

        scope_id = self._get_scope_id(group_id, user_id)
        isolation = self.config.settings.enable_group_isolation
        group_scopes = self.config.settings.group_scopes
        response_value = self._data_manager.get(trigger, scope_id, isolation, group_scopes)

        if response_value is None:
            return False, None, False

        # 判断是否为图片回复
        if response_value.lower().endswith(IMAGE_EXTENSIONS):
            # 路径安全检查：防止路径穿越攻击（如 ../../etc/passwd）
            image_path = self._resolve_safe_image_path(response_value)
            if image_path is None:
                logger.warning("检测到路径穿越尝试: '%s'", response_value)
                await self.ctx.send.text("❌ 图片路径不合法", stream_id)
                return False, "路径穿越被阻止", True

            if not image_path.exists():
                # 仅向用户展示文件名，不泄露服务器内部路径
                await self.ctx.send.text(
                    f"❌ 找不到图片文件 '{response_value}'", stream_id,
                )
                logger.warning("图片文件不存在: %s", image_path)
                return False, "图片文件不存在", True

            # 检查图片文件大小
            try:
                file_size = image_path.stat().st_size
            except OSError as e:
                logger.error("读取图片文件信息失败: %s", e)
                await self.ctx.send.text("❌ 读取图片文件时发生错误", stream_id)
                return False, "读取图片失败", True

            max_image_size = self.config.settings.max_image_size
            if file_size > max_image_size:
                size_mb = file_size / (1024 * 1024)
                limit_mb = max_image_size / (1024 * 1024)
                await self.ctx.send.text(
                    f"❌ 图片文件过大（{size_mb:.1f}MB，上限 {limit_mb:.0f}MB）",
                    stream_id,
                )
                return False, "图片文件过大", True

            try:
                b64_img_data = base64.b64encode(image_path.read_bytes()).decode("utf-8")
                await self.ctx.send.image(b64_img_data, stream_id)
            except Exception as e:
                logger.error("发送动态图片失败: %s", e)
                await self.ctx.send.text("❌ 发送图片时发生内部错误", stream_id)
                return False, "发送图片失败", True
        else:
            await self.ctx.send.text(response_value, stream_id)

        return True, "动态命令执行成功", True


def create_plugin() -> CustomCommandsPlugin:
    """创建自定义命令插件实例。"""
    return CustomCommandsPlugin()
