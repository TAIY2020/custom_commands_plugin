import re
import json
import base64
from typing import List, Tuple, Type, Dict, Optional, Any
from pathlib import Path

from src.plugin_system import (
    BasePlugin,
    register_plugin,
    BaseCommand,
    BaseEventHandler,
    ComponentInfo,
    ConfigField,
    EventType,
    MaiMessages,
    PythonDependency,
    ReplyContentType,
    config_api,
)
from src.common.logger import get_logger

logger = get_logger("custom_commands_plugin")


# --- 信号管理器 ---
class ThinkingInterceptor:
    """
    一个简单的单例信号管理器。
    用于在 Command 执行成功后，向 EventHandler 传递“停止后续思考流程”的信号。
    """
    _instance = None
    _intercept_flags: set[str] = set()

    def __new__(cls, *args, **kwargs):
        if not cls._instance:
            cls._instance = super(ThinkingInterceptor, cls).__new__(cls)
        return cls._instance

    def set_intercept(self, stream_id: str):
        """设置拦截信号 (由 Command 调用)"""
        self._intercept_flags.add(stream_id)

    def should_intercept_and_clear(self, stream_id: str) -> bool:
        """检查并消费拦截信号 (由 EventHandler 调用)"""
        if stream_id in self._intercept_flags:
            self._intercept_flags.remove(stream_id)
            return True
        return False


thinking_interceptor = ThinkingInterceptor()


# --- 状态管理模块 ---
class CommandDataManager:
    """
    负责自定义命令数据的加载、保存和查询。
    支持多作用域 (Scope) 管理，实现群组隔离与映射。
    """
    _instance = None
    commands: Dict[str, Dict[str, str]] = {}
    file_path: Optional[Path] = None

    def __new__(cls, *args, **kwargs):
        if not cls._instance:
            cls._instance = super(CommandDataManager, cls).__new__(cls)
        return cls._instance

    def load(self, plugin_dir: str):
        """加载数据文件，并处理旧版本数据迁移"""
        self.file_path = Path(plugin_dir) / "custom_commands.json"
        try:
            if self.file_path.exists():
                with open(self.file_path, 'r', encoding='utf-8') as f:
                    data = json.load(f)

                # [自动迁移] 检测旧版扁平结构数据，迁移到 "global" 作用域
                if data and isinstance(list(data.values())[0], str):
                    logger.warning("检测到旧版命令数据格式，正在迁移到Global作用域...")
                    self.commands = {"global": data}
                    self.save()
                else:
                    self.commands = data

                total_cmds = sum(len(scope)
                                 for scope in self.commands.values())
                logger.info(
                    f"成功加载 {total_cmds} 条自定义命令 (涵盖 {len(self.commands)} 个作用域)")
            else:
                self.commands = {"global": {}}
                self.save()
                logger.info(f"未找到 '{self.file_path.name}'，已创建新文件")
        except (json.JSONDecodeError, IOError, IndexError, AttributeError) as e:
            logger.error(f"加载 '{self.file_path.name}' 失败: {e}")
            self.commands = {"global": {}}

    def save(self):
        if not self.file_path:
            return
        try:
            with open(self.file_path, 'w', encoding='utf-8') as f:
                json.dump(self.commands, f, ensure_ascii=False, indent=4)
        except IOError as e:
            logger.error(f"保存自定义命令数据到 '{self.file_path.name}' 失败: {e}")

    def _resolve_scope(self, scope_id: str, enable_isolation: bool, group_scopes: Dict[str, str]) -> str:
        """
        核心逻辑：解析当前 ID 应该对应哪个数据作用域 (Scope)。
        优先级：
        1. 配置文件中的 group_scopes 映射 (自定义分组)
        2. 如果开启隔离 -> 使用 scope_id (通常是群号)
        3. 如果关闭隔离 -> 使用 "global"
        """
        scope_id_str = str(scope_id)

        for map_id, map_scope in group_scopes.items():
            if str(map_id) == scope_id_str:
                return map_scope

        return scope_id_str if enable_isolation else "global"

    def get(self, trigger: str, scope_id: str, enable_isolation: bool, group_scopes: Dict[str, str]) -> Optional[str]:
        """
        获取命令回复。
        策略：优先查找当前 Scope (群组/分组) 的命令，如果没有，自动回退查找 global 全局命令。
        """
        target_scope = self._resolve_scope(
            scope_id, enable_isolation, group_scopes)

        if target_scope in self.commands and trigger in self.commands[target_scope]:
            return self.commands[target_scope][trigger]

        if target_scope != "global":
            if "global" in self.commands and trigger in self.commands["global"]:
                return self.commands["global"][trigger]

        return None

    def add(self, trigger: str, response: str, scope_id: str, enable_isolation: bool, group_scopes: Dict[str, str]):
        """添加命令到计算出的作用域"""
        scope = self._resolve_scope(scope_id, enable_isolation, group_scopes)

        if scope not in self.commands:
            self.commands[scope] = {}

        self.commands[scope][trigger] = response
        self.save()
        return scope

    def delete(self, trigger: str, scope_id: str, enable_isolation: bool, group_scopes: Dict[str, str]) -> Tuple[bool, str]:
        scope = self._resolve_scope(scope_id, enable_isolation, group_scopes)

        if scope in self.commands and trigger in self.commands[scope]:
            del self.commands[scope][trigger]
            if not self.commands[scope] and scope != "global":
                del self.commands[scope]
            self.save()
            return True, scope
        return False, scope

    def get_triggers_for_scope(self, scope_id: str, enable_isolation: bool, group_scopes: Dict[str, str]) -> List[str]:
        """获取当前环境下可见的所有触发词 (包含本群独有 + 全局共享)"""
        triggers = set()

        if "global" in self.commands:
            triggers.update(self.commands["global"].keys())

        target_scope = self._resolve_scope(
            scope_id, enable_isolation, group_scopes)
        if target_scope in self.commands:
            triggers.update(self.commands[target_scope].keys())

        return list(triggers)


data_manager = CommandDataManager()


# --- 事件处理器 ---
class StopThinkingEventHandler(BaseEventHandler):
    """
    负责在自定义命令执行后，拦截消息，防止进入后续的 LLM 思考流程。
    """
    handler_name = "custom_command_stop_thinking_handler"
    handler_description = "在命令执行成功后，阻止麦麦进入思考流程"
    event_type = EventType.ON_PLAN
    weight = 10000
    intercept_message = True

    async def execute(self, message: MaiMessages) -> Tuple[bool, bool, Optional[str], None, None]:
        # 检查信号管理器中是否有当前聊天的信号
        if message.stream_id and thinking_interceptor.should_intercept_and_clear(message.stream_id):
            logger.debug("检测到自定义命令已执行，终止后续 LLM 思考流程")
            return True, False, "Command intercepted thinking", None, None
        return True, True, None, None, None


# --- Command 组件定义 ---
class CustomCommandBase(BaseCommand):
    """为所有自定义命令组件提供的通用基类"""

    def _get_real_id(self) -> str:
        """
        获取真实的 ID (group_id 或 user_id)，而不是 stream_id。
        因为 stream_id 是系统内部连接标识，而用户配置使用的是 QQ 群号/账号。
        """
        if self.message.message_info.group_info and self.message.message_info.group_info.group_id:
            return str(self.message.message_info.group_info.group_id)

        if self.message.message_info.user_info and self.message.message_info.user_info.user_id:
            return str(self.message.message_info.user_info.user_id)

        return str(self.message.chat_stream.stream_id)

    def _check_admin_permission(self) -> bool:
        """检查发送者是否有管理员权限"""
        admin_ids = self.get_config("settings.admin_user_ids", [])
        admin_ids_str = [str(uid) for uid in admin_ids]

        current_user_id = str(self.message.message_info.user_info.user_id)
        return current_user_id in admin_ids_str

    def _get_context_config(self):
        """辅助方法：一次性获取当前上下文所需的配置参数"""
        return (
            self._get_real_id(),
            self.get_config("settings.enable_group_isolation", False),
            self.get_config("settings.group_scopes", {})
        )


class AddCustomCommand(CustomCommandBase):
    """一个用于添加新的自定义命令的组件"""
    command_name = "custom_command_add"
    command_description = "添加一个新的自定义命令。格式：.问：触发词答：回复内容"
    command_pattern = r"^{escaped_prefix}问：(?P<trigger>.+?)答：(?P<response>.+)$"

    async def execute(self) -> Tuple[bool, Optional[str], int]:
        # 权限检查：如果用户不在 admin_user_ids，则拒绝操作
        user_id = self.message.message_info.user_info.user_id
        if not self._check_admin_permission():
            await self.send_text("❌ 你没有权限执行此管理员命令")
            return False, f"用户 {user_id} 尝试添加命令失败 (无权限)", 2

        trigger = self.matched_groups.get("trigger", "").strip()
        response = self.matched_groups.get("response", "").strip()

        if not trigger or not response:
            await self.send_text("❌ 命令格式错误，请使用：.问：触发词答：回复内容")
            return False, "格式错误", 2

        real_id, isolation, group_scopes = self._get_context_config()

        scope_used = data_manager.add(
            trigger, response, real_id, isolation, group_scopes)

        if scope_used == "global":
            scope_desc = "（全局共享）"
        elif real_id != scope_used:
            scope_desc = f"（映射分组: {scope_used}）"
        else:
            scope_desc = f"（ID: {scope_used} 独享）"

        await self.send_text(f"✅ 成功添加自定义命令{scope_desc}！\n触发词：{trigger}\n回复内容：{response}")

        # 调用信号管理器设置拦截信号
        thinking_interceptor.set_intercept(self.message.chat_stream.stream_id)

        user_id = self.message.message_info.user_info.user_id
        logger.info(f"用户 '{user_id}' 在作用域 '{scope_used}' 添加命令: '{trigger}'")
        return True, "添加成功", 2


class DeleteCustomCommand(CustomCommandBase):
    """一个用于删除现有自定义命令的组件"""
    command_name = "custom_command_delete"
    command_description = "删除一个现有的自定义命令。格式：.删：触发词"
    command_pattern = r"^{escaped_prefix}删：(?P<trigger>.+)$"

    async def execute(self) -> Tuple[bool, Optional[str], int]:
        user_id = self.message.message_info.user_info.user_id
        if not self._check_admin_permission():
            await self.send_text("❌ 你没有权限执行此管理员命令")
            return False, f"用户 {user_id} 尝试删除命令失败 (无权限)", 2

        trigger = self.matched_groups.get("trigger", "").strip()
        real_id, isolation, group_scopes = self._get_context_config()

        success, scope_used = data_manager.delete(
            trigger, real_id, isolation, group_scopes)

        if success:
            await self.send_text(f"✅ 成功删除了自定义命令（作用域: {scope_used}）：'{trigger}'")
            thinking_interceptor.set_intercept(
                self.message.chat_stream.stream_id)
            return True, "删除成功", 2
        else:
            current_scope = data_manager._resolve_scope(
                real_id, isolation, group_scopes)
            msg = f"❌ 未在当前作用域 [{current_scope}] 找到命令：'{trigger}'"

            if current_scope != "global" and data_manager.get(trigger, "global_dummy", False, {}) is not None:
                msg += "\n💡 提示：这是一个【全局命令】。若要删除它，请关闭本群隔离模式，或在未映射分组的群聊中操作。"

            await self.send_text(msg)
            return False, "命令未找到", 2


class ListCustomCommands(CustomCommandBase):
    """一个用于列出所有可用自定义命令的组件"""
    command_name = "custom_command_list"
    command_description = "列出所有可用的自定义命令。格式：.列表"
    command_pattern = r"^{escaped_prefix}列表$"

    async def execute(self) -> Tuple[bool, Optional[str], int]:
        real_id, isolation, group_scopes = self._get_context_config()

        triggers = data_manager.get_triggers_for_scope(
            real_id, isolation, group_scopes)

        current_scope = data_manager._resolve_scope(
            real_id, isolation, group_scopes)

        if not triggers:
            await self.send_text(f"🤷‍♀️ 当前作用域 [{current_scope}] 下没有可用的自定义命令")
        else:
            prefix = self.get_config("settings.command_prefix", ".")
            bot_name = config_api.get_global_config("bot.nickname", "MaiCore")

            header_text = f"📋 自定义命令列表\n当前ID: {real_id}\n对应作用域: {current_scope}"
            if current_scope == "global":
                header_text += "\n(全局共享模式)"
            elif real_id != current_scope:
                header_text += "\n(自定义映射分组模式)"
            else:
                header_text += "\n(独立隔离模式)"

            header_content = [(ReplyContentType.TEXT, header_text)]
            list_content = "\n".join(
                f"▪️ {prefix}{trigger}" for trigger in sorted(triggers))
            full_content = [(ReplyContentType.TEXT, list_content)]

            await self.send_forward([
                ("1", bot_name, header_content),
                ("1", bot_name, full_content)
            ])

        thinking_interceptor.set_intercept(self.message.chat_stream.stream_id)
        return True, "列表已发送", 2


class HandleDynamicCustomCommand(CustomCommandBase):
    """一个用于处理所有动态添加的自定义命令的组件"""
    command_name = "custom_command_handler"
    command_description = "处理所有动态添加的自定义命令"
    command_pattern = r"^{escaped_prefix}(?P<trigger>.+)$"

    async def execute(self) -> Tuple[bool, Optional[str], int]:
        trigger = self.matched_groups.get("trigger", "").strip()
        real_id, isolation, group_scopes = self._get_context_config()

        response_value = data_manager.get(trigger, real_id, isolation, group_scopes)

        if response_value is None:
            return False, "非自定义命令，跳过", 0

        image_extensions = ('.png', '.jpg', '.jpeg', '.gif', '.webp')
        if response_value.lower().endswith(image_extensions):
            image_base_dir = self.get_config(
                "settings.image_directory", "plugins/custom_commands_plugin/images")
            image_path = Path(image_base_dir) / response_value

            if not image_path.exists():
                await self.send_text(f"❌ 配置错误：在 '{image_base_dir}' 目录中找不到图片 '{response_value}'")
                return False, "图片文件不存在", 2

            try:
                b64_img_data = base64.b64encode(image_path.read_bytes()).decode('utf-8')
                await self.send_image(image_base64=b64_img_data)
            except Exception as e:
                logger.error(f"发送动态图片失败: {e}")
                await self.send_text("❌ 发送图片时发生内部错误")
                return False, "发送图片失败", 2
        else:
            await self.send_text(response_value)

        thinking_interceptor.set_intercept(self.message.chat_stream.stream_id)
        return True, "动态命令执行成功", 2


# --- 注册插件 ---
@register_plugin
class CustomCommandsPlugin(BasePlugin):
    """
    CustomCommands 插件 - 通过聊天命令动态添加、删除、列出和触发自定义命令，支持文本和图片。
    支持群组数据隔离与自定义分组映射。
    """
    plugin_name: str = "custom_commands_plugin"
    enable_plugin: bool = True
    dependencies: List[str] = []
    python_dependencies: List[PythonDependency] = []
    config_file_name: str = "config.toml"

    config_schema: dict = {
        "plugin": {
            "name": ConfigField(type=str, default="custom_commands_plugin", description="插件名称", disabled=True),
            "version": ConfigField(type=str, default="1.6.1", description="插件版本", disabled=True),
            "enabled": ConfigField(type=bool, default=True, description="是否启用插件", label="启用插件"),
        },
        "settings": {
            "command_prefix": ConfigField(
                type=str,
                default=".",
                description="所有自定义命令的前缀",
                label="命令前缀",
                input_type="text"
            ),
            "admin_user_ids": ConfigField(
                type=list,
                default=[],
                description="拥有添加/删除命令权限的用户ID列表 (QQ号)。留空 [] 表示任何人都没有权限。",
                label="管理员列表",
                input_type="list"
            ),
            "image_directory": ConfigField(
                type=str,
                default="plugins/custom_commands_plugin/images",
                description="存放自定义回复图片的目录路径（相对于主程序根目录）",
                label="图片目录",
                input_type="text"
            ),
            "enable_group_isolation": ConfigField(
                type=bool,
                default=False,
                description="是否开启群组隔离默认模式。若开启，未配置映射的群组将使用各自独立的命令库。",
                label="启用群组隔离",
                input_type="checkbox"
            ),
            "group_scopes": ConfigField(
                type=dict,
                default={ },
                description="群组作用域映射。键为群号(group_id)，值为自定义的作用域名称。作用域名称相同的群组将共享同一套命令。",
                label="群组作用域映射",
                example='{ "123456" = "gaming_group", "654321" = "gaming_group" }'
            )
        },
    }

    config_section_descriptions = {
        "plugin": "插件基本信息",
        "settings": "命令基本设置",
    }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        image_dir_path = self.get_config("settings.image_directory", "plugins/custom_commands_plugin/images")
        Path(image_dir_path).mkdir(parents=True, exist_ok=True)
        data_manager.load(self.plugin_dir)

    def get_plugin_components(self) -> List[Tuple[ComponentInfo, Type]]:
        """
        返回所有 Command 组件：添加、删除、列出和处理。
        """
        prefix = self.get_config("settings.command_prefix", ".")
        escaped = re.escape(prefix)

        # 动态地将前缀插入到每个命令的正则表达式中
        AddCustomCommand.command_pattern = AddCustomCommand.command_pattern.format(escaped_prefix=escaped)
        DeleteCustomCommand.command_pattern = DeleteCustomCommand.command_pattern.format(escaped_prefix=escaped)
        ListCustomCommands.command_pattern = ListCustomCommands.command_pattern.format(escaped_prefix=escaped)
        HandleDynamicCustomCommand.command_pattern = HandleDynamicCustomCommand.command_pattern.format(escaped_prefix=escaped)

        return [
            (AddCustomCommand.get_command_info(), AddCustomCommand),
            (DeleteCustomCommand.get_command_info(), DeleteCustomCommand),
            (ListCustomCommands.get_command_info(), ListCustomCommands),
            (StopThinkingEventHandler.get_handler_info(), StopThinkingEventHandler),
            (HandleDynamicCustomCommand.get_command_info(), HandleDynamicCustomCommand),
        ]
