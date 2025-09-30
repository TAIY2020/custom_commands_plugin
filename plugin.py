import re
import json
import base64
from typing import List, Tuple, Type, Dict, Any
from pathlib import Path

from src.plugin_system import (
    BasePlugin,
    register_plugin,
    BaseCommand,
    ComponentInfo,
    ConfigField,
)
from src.common.logger import get_logger

logger = get_logger("custom_commands_plugin")

# --- 存储动态命令的全局变量 ---
# 我们将动态加载的问答对存储在这里，以便所有 Command 实例共享
custom_commands: Dict[str, str] = {}
commands_file_path: Path = Path()


def _save_commands():
    """将内存中的问答对保存到 JSON 文件"""
    global custom_commands, commands_file_path
    try:
        with open(commands_file_path, 'w', encoding='utf-8') as f:
            json.dump(custom_commands, f, ensure_ascii=False, indent=4)
    except Exception as e:
        logger.error(f"保存自定义命令数据失败: {e}")

# --- Command 组件定义 ---
class AddCustomCommand(BaseCommand):
    """一个用于添加新的自定义命令的组件"""
    command_name = "custom_command_add"
    command_description = "添加一个新的自定义命令。格式：.问：触发词答：回复内容"
    command_pattern = r"^{escaped_prefix}问：(?P<trigger>.+?)答：(?P<response>.+)$"

    async def execute(self) -> Tuple[bool, str, bool]:
        global custom_commands

        # 权限检查
        admin_ids = self.get_config("settings.admin_user_ids", [])
        user_id = self.message.message_info.user_info.user_id
        # 如果 admin_ids 不为空且用户不在其中，则拒绝操作
        if admin_ids and user_id not in admin_ids:
            await self.send_text("❌ 你没有权限执行此操作。")
            return False, "无权限", True

        trigger = self.matched_groups.get("trigger", "").strip()
        response = self.matched_groups.get("response", "").strip()

        if not trigger or not response:
            await self.send_text("❌ 命令格式错误，请使用：.问：触发词答：回复内容")
            return False, "格式错误", True

        custom_commands[trigger] = response
        _save_commands()  # 保存到文件

        await self.send_text(f"✅ 成功添加自定义命令！\n触发词：{trigger}\n回复内容：{response}")
        return True, "添加成功", True

class DeleteCustomCommand(BaseCommand):
    """一个用于删除现有自定义命令的组件"""
    command_name = "custom_command_delete"
    command_description = "删除一个现有的自定义命令。格式：.删：触发词"
    command_pattern = r"^{escaped_prefix}删：(?P<trigger>.+)$"

    async def execute(self) -> Tuple[bool, str, bool]:
        global custom_commands
        admin_ids = self.get_config("settings.admin_user_ids", [])
        user_id = self.message.message_info.user_info.user_id
        if admin_ids and user_id not in admin_ids:
            await self.send_text("❌ 你没有权限执行此操作。")
            return False, "无权限", True

        trigger = self.matched_groups.get("trigger", "").strip()
        if trigger in custom_commands:
            del custom_commands[trigger]
            _save_commands()
            await self.send_text(f"✅ 成功删除了自定义命令：'{trigger}'")
            return True, "删除成功", True
        else:
            await self.send_text(f"❌ 未找到要删除的命令：'{trigger}'")
            return False, "命令未找到", True

class ListCustomCommands(BaseCommand):
    """一个用于列出所有可用自定义命令的组件"""
    command_name = "custom_command_list"
    command_description = "列出所有可用的自定义命令。格式：.列表"
    command_pattern = r"^{escaped_prefix}列表$"

    async def execute(self) -> Tuple[bool, str, bool]:
        global custom_commands
        command_prefix = self.get_config("settings.command_prefix", ".")

        if not custom_commands:
            await self.send_text("🤷‍♀️ 还没有添加任何自定义命令。")
            return True, "列表为空", True

        # 构建回复消息
        reply_message = "📋 可用的自定义命令列表：\n\n"
        for trigger in custom_commands.keys():
            reply_message += f"▪️ {command_prefix}{trigger}\n"

        await self.send_text(reply_message.strip())
        return True, "列表已发送", True


class HandleDynamicCustomCommand(BaseCommand):
    """一个用于处理所有动态添加的自定义命令"""
    command_name = "custom_command_handler"
    command_description = "处理所有动态添加的自定义命令"
    command_pattern = r"^{escaped_prefix}(?P<trigger>.+)$"

    async def execute(self) -> Tuple[bool, str, bool]:
        global custom_commands
        trigger = self.matched_groups.get("trigger", "").strip()

        if trigger in custom_commands:
            response_value = custom_commands[trigger]
            is_image = response_value.lower().endswith(('.png', '.jpg', '.jpeg', '.gif'))

            if is_image:
                image_dir = Path("data/images")
                image_path = image_dir / response_value
                if not image_path.exists():
                    await self.send_text(f"❌ 配置错误：在 data/images/ 中找不到图片文件 '{response_value}'")
                    return False, "图片文件不存在", True
                try:
                    b64_img_data = base64.b64encode(image_path.read_bytes()).decode('utf-8')
                    await self.send_image(image_base64=b64_img_data)
                    return True, "动态图片命令执行成功", True
                except Exception as e:
                    logger.error(f"发送动态图片失败: {e}")
                    await self.send_text("❌ 发送图片时发生内部错误。")
                    return False, "发送图片失败", True
            else:
                await self.send_text(response_value)
                return True, "动态文本命令执行成功", True

        return False, "未找到自定义命令", False

# --- 注册插件 ---
@register_plugin
class CustomCommandsPlugin(BasePlugin):
    """
    CustomCommands 插件 - 通过聊天命令动态添加、删除、列出和触发自定义命令，支持文本和图片。
    """
    plugin_name: str = "custom_commands_plugin"
    enable_plugin: bool = True
    dependencies: list[str] = []
    python_dependencies: list[str] = []
    config_file_name: str = "config.toml"

    config_schema: dict = {
        "plugin": {
            "name": ConfigField(type=str, default="custom_commands_plugin", description="插件名称"),
            "version": ConfigField(type=str, default="1.3.0", description="插件版本"),
            "enabled": ConfigField(type=bool, default=True, description="是否启用插件"),
        },
        "settings": {
            "command_prefix": ConfigField(type=str, default=".", description="所有自定义命令的前缀"), # (一致性优化)
            "admin_user_ids": ConfigField(
                type=list,
                default=["12345678"],
                description="拥有添加/删除命令权限的用户ID列表 (QQ号)。留空 [] 表示任何人都可以添加，为了安全强烈建议填写！"
            ),
        },
    }

    config_section_descriptions = {
        "plugin": "插件基本信息",
        "settings": "命令基本设置",
    }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        global custom_commands, commands_file_path

        # 初始化并加载动态回复数据
        commands_file_path = Path(self.plugin_dir) / "custom_commands.json"
        try:
            if commands_file_path.exists():
                with open(commands_file_path, 'r', encoding='utf-8') as f:
                    custom_commands = json.load(f)
                logger.info(f"成功加载 {len(custom_commands)} 条自定义命令。")
            else:
                # 如果文件不存在，则创建一个空文件
                _save_commands()
                logger.info("未找到 'custom_commands.json'，已创建新文件。")
        except Exception as e:
            logger.error(f"加载 'custom_commands.json' 失败: {e}")
            custom_commands = {}


    def get_plugin_components(self) -> List[Tuple[ComponentInfo, Type]]:
        """
        返回所有 Command 组件：添加、删除、列出和处理。
        """
        command_prefix = self.get_config("settings.command_prefix", ".")
        escaped_prefix = re.escape(command_prefix)

        # 动态地将前缀插入到每个命令的正则表达式中
        AddCustomCommand.command_pattern = AddCustomCommand.command_pattern.format(escaped_prefix=escaped_prefix)
        DeleteCustomCommand.command_pattern = DeleteCustomCommand.command_pattern.format(escaped_prefix=escaped_prefix)
        ListCustomCommands.command_pattern = ListCustomCommands.command_pattern.format(escaped_prefix=escaped_prefix)
        HandleDynamicCustomCommand.command_pattern = HandleDynamicCustomCommand.command_pattern.format(escaped_prefix=escaped_prefix)

        return [
            (AddCustomCommand.get_command_info(), AddCustomCommand),
            (DeleteCustomCommand.get_command_info(), DeleteCustomCommand),
            (ListCustomCommands.get_command_info(), ListCustomCommands),
            (HandleDynamicCustomCommand.get_command_info(), HandleDynamicCustomCommand),
        ]
