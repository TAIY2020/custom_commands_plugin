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
    用于在 Command 和 EventHandler 之间安全地传递“停止思考”的信号。
    """
    _instance = None
    _intercept_flags: set[str] = set()

    def __new__(cls, *args, **kwargs):
        if not cls._instance:
            cls._instance = super(ThinkingInterceptor, cls).__new__(cls)
        return cls._instance

    def set_intercept(self, stream_id: str):
        """由 Command 调用，设置一个拦截信号。"""
        self._intercept_flags.add(stream_id)

    def should_intercept_and_clear(self, stream_id: str) -> bool:
        """由 EventHandler 调用，检查并消费信号。"""
        if stream_id in self._intercept_flags:
            self._intercept_flags.remove(stream_id)
            return True
        return False


thinking_interceptor = ThinkingInterceptor()


# --- 状态管理模块 ---
class CommandDataManager:
    _instance = None
    commands: Dict[str, str] = {}
    file_path: Optional[Path] = None

    def __new__(cls, *args, **kwargs):
        if not cls._instance:
            cls._instance = super(CommandDataManager, cls).__new__(cls)
        return cls._instance

    def load(self, plugin_dir: str):
        self.file_path = Path(plugin_dir) / "custom_commands.json"
        try:
            if self.file_path.exists():
                with open(self.file_path, 'r', encoding='utf-8') as f:
                    self.commands = json.load(f)
                logger.info(f"成功加载 {len(self.commands)} 条自定义命令")
            else:
                self.save()
                logger.info(f"未找到 '{self.file_path.name}'，已创建新文件")
        except (json.JSONDecodeError, IOError) as e:
            logger.error(f"加载 '{self.file_path.name}' 失败: {e}")
            self.commands = {}

    def save(self):
        if not self.file_path:
            return
        try:
            with open(self.file_path, 'w', encoding='utf-8') as f:
                json.dump(self.commands, f, ensure_ascii=False, indent=4)
        except IOError as e:
            logger.error(f"保存自定义命令数据到 '{self.file_path.name}' 失败: {e}")

    def get(self, trigger: str) -> Optional[str]:
        return self.commands.get(trigger)

    def add(self, trigger: str, response: str):
        self.commands[trigger] = response
        self.save()

    def delete(self, trigger: str) -> bool:
        if trigger in self.commands:
            del self.commands[trigger]
            self.save()
            return True
        return False

    def get_all_triggers(self) -> List[str]:
        return list(self.commands.keys())


data_manager = CommandDataManager()


# --- 事件处理器 ---
class StopThinkingEventHandler(BaseEventHandler):
    handler_name = "custom_command_stop_thinking_handler"
    handler_description = "在命令执行成功后，阻止麦麦进入思考流程"
    event_type = EventType.ON_PLAN
    weight = 10000
    intercept_message = True

    async def execute(self, message: MaiMessages) -> Tuple[bool, bool, Optional[str], None, None]:
        # 检查信号管理器中是否有当前聊天的信号
        if message.stream_id and thinking_interceptor.should_intercept_and_clear(message.stream_id):
            logger.debug("检测到命令已处理并要求拦截，终止后续思考流程")
            return True, False, "Command intercepted thinking", None, None
        return True, True, None, None, None


# --- Command 组件定义 ---
class CustomCommandBase(BaseCommand):
    """为所有自定义命令创建一个共享基类，用于放置通用逻辑"""

    @staticmethod
    def _check_admin_permission(command: BaseCommand) -> bool:
        """将权限检查作为静态方法，方便管理"""
        admin_ids = command.get_config("settings.admin_user_ids", [])
        user_id = command.message.message_info.user_info.user_id
        return bool(admin_ids and user_id in admin_ids)


class AddCustomCommand(CustomCommandBase):
    """一个用于添加新的自定义命令的组件"""
    command_name = "custom_command_add"
    command_description = "添加一个新的自定义命令。格式：.问：触发词答：回复内容"
    command_pattern = r"^{escaped_prefix}问：(?P<trigger>.+?)答：(?P<response>.+)$"

    async def execute(self) -> Tuple[bool, Optional[str], bool]:
        # 权限检查：如果用户不在 admin_user_ids，则拒绝操作
        user_id = self.message.message_info.user_info.user_id
        if not self._check_admin_permission(self):
            await self.send_text("❌ 你没有权限执行此管理员命令")
            return False, f"用户 {user_id} 尝试添加命令失败 (无权限)", True

        trigger = self.matched_groups.get("trigger", "").strip()
        response = self.matched_groups.get("response", "").strip()

        if not trigger or not response:
            await self.send_text("❌ 命令格式错误，请使用：.问：触发词答：回复内容")
            return False, "格式错误", True

        data_manager.add(trigger, response)
        await self.send_text(f"✅ 成功添加自定义命令！\n触发词：{trigger}\n回复内容：{response}")
        # 调用信号管理器设置拦截信号
        thinking_interceptor.set_intercept(self.message.chat_stream.stream_id)
        logger.info(f"管理员 '{user_id}' 添加了自定义命令: '{trigger}' -> '{response[:50]}...'")
        return True, "添加成功", True


class DeleteCustomCommand(CustomCommandBase):
    """一个用于删除现有自定义命令的组件"""
    command_name = "custom_command_delete"
    command_description = "删除一个现有的自定义命令。格式：.删：触发词"
    command_pattern = r"^{escaped_prefix}删：(?P<trigger>.+)$"

    async def execute(self) -> Tuple[bool, Optional[str], bool]:
        user_id = self.message.message_info.user_info.user_id
        if not self._check_admin_permission(self):
            await self.send_text("❌ 你没有权限执行此管理员命令")
            return False, f"用户 {user_id} 尝试删除命令失败 (无权限)", True

        trigger = self.matched_groups.get("trigger", "").strip()
        if data_manager.delete(trigger):
            await self.send_text(f"✅ 成功删除了自定义命令：'{trigger}'")
            thinking_interceptor.set_intercept(self.message.chat_stream.stream_id)
            logger.info(f"管理员 '{user_id}' 删除了自定义命令: '{trigger}'")
            return True, "删除成功", True
        else:
            await self.send_text(f"❌ 未找到要删除的命令：'{trigger}'")
            return False, "命令未找到", True


class ListCustomCommands(CustomCommandBase):
    """一个用于列出所有可用自定义命令的组件"""
    command_name = "custom_command_list"
    command_description = "列出所有可用的自定义命令。格式：.列表"
    command_pattern = r"^{escaped_prefix}列表$"

    async def execute(self) -> Tuple[bool, Optional[str], bool]:
        triggers = data_manager.get_all_triggers()
        if not triggers:
            await self.send_text("🤷‍♀️ 还没有添加任何自定义命令")
        else:
            # 使用合并转发消息来发送列表，避免刷屏和长度限制
            prefix = self.get_config("settings.command_prefix", ".")
            bot_name = config_api.get_global_config("bot.nickname", "MaiCore")

            header_content = [(ReplyContentType.TEXT, "📋 可用的自定义命令列表：")]
            list_content = "\n".join(f"▪️ {prefix}{trigger}" for trigger in triggers)
            full_content = [(ReplyContentType.TEXT, list_content)]

            message_list_to_forward = [
                ("1", bot_name, header_content),
                ("1", bot_name, full_content)
            ]
            await self.send_forward(message_list_to_forward)

        thinking_interceptor.set_intercept(self.message.chat_stream.stream_id)
        return True, "列表已发送", True


class HandleDynamicCustomCommand(CustomCommandBase):
    """一个用于处理所有动态添加的自定义命令的组件"""
    command_name = "custom_command_handler"
    command_description = "处理所有动态添加的自定义命令"
    command_pattern = r"^{escaped_prefix}(?P<trigger>.+)$"

    async def execute(self) -> Tuple[bool, Optional[str], bool]:
        trigger = self.matched_groups.get("trigger", "").strip()
        response_value = data_manager.get(trigger)

        if response_value is None:
            return False, "非自定义命令，跳过", False

        image_extensions = ('.png', '.jpg', '.jpeg', '.gif', '.webp')
        if response_value.lower().endswith(image_extensions):
            image_base_dir = self.get_config(
                "settings.image_directory", "plugins/custom_commands_plugin/images")
            image_path = Path(image_base_dir) / response_value
            if not image_path.exists():
                await self.send_text(f"❌ 配置错误：在 '{image_base_dir}' 目录中找不到图片 '{response_value}'")
                return False, "图片文件不存在", True
            try:
                b64_img_data = base64.b64encode(image_path.read_bytes()).decode('utf-8')
                await self.send_image(image_base64=b64_img_data)
            except Exception as e:
                logger.error(f"发送动态图片失败: {e}")
                await self.send_text("❌ 发送图片时发生内部错误")
                return False, "发送图片失败", True
        else:
            await self.send_text(response_value)

        thinking_interceptor.set_intercept(self.message.chat_stream.stream_id)
        return True, "动态命令执行成功", True


# --- 注册插件 ---
@register_plugin
class CustomCommandsPlugin(BasePlugin):
    """
    CustomCommands 插件 - 通过聊天命令动态添加、删除、列出和触发自定义命令，支持文本和图片。
    """
    plugin_name: str = "custom_commands_plugin"
    enable_plugin: bool = True
    dependencies: List[str] = []
    python_dependencies: List[PythonDependency] = []
    config_file_name: str = "config.toml"

    config_schema: dict = {
        "plugin": {
            "name": ConfigField(type=str, default="custom_commands_plugin", description="插件名称"),
            "version": ConfigField(type=str, default="1.5.2", description="插件版本"),
            "enabled": ConfigField(type=bool, default=True, description="是否启用插件"),
        },
        "settings": {
            "command_prefix": ConfigField(type=str, default=".", description="所有自定义命令的前缀"),
            "admin_user_ids": ConfigField(type=list, default=[], description="拥有添加/删除命令权限的用户ID列表 (QQ号)。留空 [] 表示任何人都没有权限。"),
            "image_directory": ConfigField(
                type=str,
                default="plugins/custom_commands_plugin/images",
                description="存放自定义回复图片的目录路径（相对于主程序根目录）"
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
