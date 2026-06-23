"""自定义命令插件的强类型配置 Schema。

两大 Section（plugin / settings）聚合到 ``CustomCommandsConfig``，由
``CustomCommandsPlugin.config_model`` 绑定。``SettingsSection`` 的旧格式迁移
委托给 ``ScopeResolver.migrate_legacy``，与运行时解析共享同一份逻辑。
"""

from __future__ import annotations

from typing import Any, List

from maibot_sdk import Field, PluginConfigBase
from pydantic import model_validator

from .common import (
    DEFAULT_MAX_COMMANDS_PER_SCOPE,
    DEFAULT_MAX_IMAGE_SIZE,
    DEFAULT_MAX_RESPONSE_LENGTH,
    DEFAULT_MAX_TRIGGER_LENGTH,
)
from .scope import ScopeResolver

# 配置 schema 版本（与插件版本独立，仅在配置字段结构变更时手动上调）
CONFIG_SCHEMA_VERSION = "2.4.0"


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
        description=(
            "存放自定义回复图片的目录路径（相对路径基于插件目录解析，也可填写绝对路径）。"
            "不建议配置为磁盘根目录、系统目录、用户根目录或大型共享目录。"
            "注意：修改目录后需手动迁移已有图片文件，插件不会自动搬运，否则历史图片命令会找不到文件。"
        ),
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
