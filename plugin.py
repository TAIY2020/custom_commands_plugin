"""自定义命令插件 — MaiBot SDK v2

通过聊天命令动态添加、删除、列出和触发自定义回复，支持文本和图片。
支持群组数据隔离与自定义分组映射。

[架构]
- 内置命令（添加/删除/列表/删全局）用 @Command 注册，pattern 是精确的"前缀+关键字"
  (如 ``^\\.列表$``)。get_components() 把 pattern 中的 [^\\w\\s] 占位重写为
  re.escape(prefix)，让 Runner 注册的正则只匹配实际配置的前缀。
- 动态触发（用户 add 的 .xxx）走 @HookHandler 接管 chat.receive.after_process，
  在 Command 调度前介入：命中已注册 trigger 才回复 + abort；未命中直接放行，
  避免抢占其他插件的 Command。

[模块拓扑]
本文件为薄入口：装配协作模块 + 生命周期 + get_components 前缀重写 + 5 个装饰器入口
（@Command×4 / @HookHandler×1，body 委托）。@Command/@HookHandler 必须定义在插件类上
才会被 collect_components 收集，故入口留此、业务下沉。具体能力拆在 ``core`` 子包：

* ``core.common``      —— 常量 / 命令关键字 / 保留词判断 / 作用域文案 / manifest 版本
* ``core.config``      —— 强类型配置 Schema（PluginSection / SettingsSection）
* ``core.scope``       —— ScopeResolver：群组作用域解析（纯，可复用）
* ``core.storage``     —— CommandDataManager：命令数据 CRUD + 原子落盘 + 孤儿判定（纯）
* ``core.images``      —— ImageStore：图片安全存取、孤儿回收、图片回复投递
* ``core.forward``     —— ListForwardSender：命令列表的 Napcat 合并转发（含纯文本降级）
* ``core.service``     —— CommandService：增删查与动态应答的统一编排（写命令唯一入口）
* ``core.dispatcher``  —— DynamicDispatcher：入站消息的动态命令路由（带图添加 / 命中应答）
"""

from maibot_sdk import Command, HookHandler, MaiBotPlugin
from maibot_sdk.types import ErrorPolicy, HookMode, HookOrder

import asyncio
import logging
import os
import re
from typing import Any, Dict, List, Optional

from .core.common import (
    KW_ADD,
    KW_ADD_ANSWER,
    KW_DELETE,
    KW_DELETE_GLOBAL,
    KW_LIST,
    PLUGIN_VERSION,
    PREFIX_PLACEHOLDER,
    is_reserved_trigger,
)
from .core.config import CustomCommandsConfig
from .core.dispatcher import DynamicDispatcher
from .core.forward import ListForwardSender
from .core.images import ImageStore
from .core.scope import ScopeResolver
from .core.service import CommandService
from .core.storage import CommandDataManager

logger = logging.getLogger(__name__)


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
        self._admin_set: set[str] = set()  # 缓存管理员集合
        self._registered_prefix: Optional[str] = None  # 注册到主程序时使用的 prefix，用于检测热改
        self._self_reload_scheduled: bool = False  # 标记是否已调度自重载任务，防重入
        # 持有自重载 task 的强引用：asyncio.create_task 返回的 task 若无人引用，
        # 可能在执行中途被 GC 回收（CPython 已知行为），这里存到实例属性兜底。
        self._reload_task: Optional["asyncio.Task[None]"] = None
        # 4 个持 plugin 弱引用的协作模块；构造仅存 self 引用，相互依赖在调用时延迟解析，
        # 故构造顺序无关（service 用 images/forward/scope/storage，dispatcher 用 service）。
        self._images = ImageStore(self)
        self._forward = ListForwardSender(self)
        self._service = CommandService(self)
        self._dispatcher = DynamicDispatcher(self)

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
            # count=1：只替换开头那个前缀占位符。当前 4 个 pattern 的占位都仅在 ^ 后出现一次，
            # 限定替换次数可防未来 pattern 在 trigger/response 段也用到 [^\w\s] 时被连带误替换。
            metadata["command_pattern"] = pattern.replace(PREFIX_PLACEHOLDER, escaped_prefix, 1)
        self._registered_prefix = prefix
        return components

    async def on_load(self) -> None:
        """插件加载时初始化数据管理器和图片目录。"""
        self._plugin_dir = os.path.dirname(os.path.abspath(__file__))

        # 加载命令数据
        self._data_manager.load(self._plugin_dir)

        # 清洗历史/手工编辑残留的"幽灵命令"：命中内置命令保留词的 trigger 永远无法
        # 被动态触发（hook 见到会让位给精确 @Command），留在库里只会污染 .列表 输出。
        ghost_removed = self._data_manager.purge_reserved_triggers(is_reserved_trigger)
        if ghost_removed:
            logger.warning("清理了 %d 条与内置命令冲突的幽灵命令", ghost_removed)
            try:
                await self._data_manager.save()
            except OSError as e:
                logger.error("保存清理后的命令数据失败: %s；幽灵命令未落盘，下次加载会再次清理", e)

        # 刷新作用域解析器（解析 group_scopes + 反向索引）
        self._scope_resolver.refresh(
            group_scopes=self.config.settings.group_scopes,
            enable_isolation=self.config.settings.enable_group_isolation,
        )

        # 缓存管理员集合
        self._admin_set = {str(uid) for uid in self.config.settings.admin_user_ids}

        # 确保图片目录存在（基于插件目录解析，避免依赖文件夹的具体名称）
        try:
            self._images.resolve_dir().mkdir(parents=True, exist_ok=True)
        except OSError as e:
            logger.warning("创建图片目录失败: %s，图片功能可能不可用", e)

        logger.info("自定义命令插件(v%s)初始化完成。", PLUGIN_VERSION)

    async def on_unload(self) -> None:
        """插件卸载时执行最终保存。

        走 ``save_locked``——与并发进行中的 add/delete 互斥，避免最终保存与
        正在写入的命令同时操作 ``self.commands`` 造成数据不一致。
        """
        try:
            await self._data_manager.save_locked()
        except OSError as e:
            logger.error("卸载时保存命令数据失败: %s", e)
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

    # ===== 装饰器入口（body 委托协作模块）=====

    @Command(
        "custom_command_add",
        description="添加自定义命令。格式：<前缀>问：触发词答：回复内容",
        # response 段用 [\s\S]+ 而非 .+：Host 编译命令正则是 re.compile(pattern) 且不带
        # re.DOTALL（见主程序 host/component_registry.py），.+ 不跨行会让「答：」后含换行的
        # 多行回复整体失配、消息漏过命令路径落入 LLM。[\s\S]+ 显式匹配含换行的任意字符以
        # 支持多行回复；trigger 段仍用 .+? 保持单行（「问：」与「答：」须在同一行）。
        pattern=rf"^{PREFIX_PLACEHOLDER}{re.escape(KW_ADD)}(?P<trigger>.+?){re.escape(KW_ADD_ANSWER)}(?P<response>[\s\S]+)$",
    )
    async def handle_add(self, stream_id: str = "", group_id: str = "",
                         user_id: str = "", text: str = "",
                         matched_groups: Optional[dict] = None,
                         plugin_config: Optional[dict] = None, **kwargs):
        """添加命令：<前缀>问：触发词答：回复内容（委托 CommandService.add_text）。"""
        return await self._service.add_text(matched_groups, stream_id, group_id, user_id)

    @Command(
        "custom_command_delete",
        description="删除自定义命令。格式：<前缀>删：触发词",
        pattern=rf"^{PREFIX_PLACEHOLDER}{re.escape(KW_DELETE)}(?P<trigger>.+)$",
    )
    async def handle_delete(self, stream_id: str = "", group_id: str = "",
                            user_id: str = "", text: str = "",
                            matched_groups: Optional[dict] = None,
                            plugin_config: Optional[dict] = None, **kwargs):
        """删除命令：<前缀>删：触发词（委托 CommandService.delete）。"""
        return await self._service.delete(matched_groups, stream_id, group_id, user_id)

    @Command(
        "custom_command_delete_global",
        description="删除全局自定义命令。格式：<前缀>删全局：触发词",
        pattern=rf"^{PREFIX_PLACEHOLDER}{re.escape(KW_DELETE_GLOBAL)}(?P<trigger>.+)$",
    )
    async def handle_delete_global(self, stream_id: str = "", group_id: str = "",
                                   user_id: str = "", text: str = "",
                                   matched_groups: Optional[dict] = None,
                                   plugin_config: Optional[dict] = None, **kwargs):
        """删除全局命令：<前缀>删全局：触发词（委托 CommandService.delete_global）。"""
        return await self._service.delete_global(matched_groups, stream_id, user_id)

    @Command(
        "custom_command_list",
        description="列出所有可用的自定义命令。格式：<前缀>列表",
        pattern=rf"^{PREFIX_PLACEHOLDER}{re.escape(KW_LIST)}$",
    )
    async def handle_list(self, stream_id: str = "", group_id: str = "",
                          user_id: str = "", text: str = "",
                          plugin_config: Optional[dict] = None,
                          **kwargs):
        """列出命令：<前缀>列表（委托 CommandService.build_list）。"""
        return await self._service.build_list(stream_id, group_id, user_id)

    @HookHandler(
        "chat.receive.after_process",
        name="custom_command_dynamic_dispatcher",
        description="动态自定义命令分发：在 Command 调度前接管前缀消息，命中已注册 trigger 则回复+abort，未命中直接放行",
        mode=HookMode.BLOCKING,
        order=HookOrder.EARLY,
        timeout_ms=20000,
        error_policy=ErrorPolicy.SKIP,
        include_binary_data=True,  # 前向声明，当前 Host 未消费此 metadata；带图数据来源见下方 docstring
    )
    async def handle_dynamic_trigger(
        self, message: Optional[dict] = None, **kwargs
    ) -> Optional[Dict[str, Any]]:
        """动态触发命令的 hook 入口（委托 DynamicDispatcher.dispatch）。

        返回 ``{"action": "abort"}`` 表示已处理 + 拦截后续主链；返回 ``None`` 放行。

        带图添加依赖入站 message 的 image/emoji 段携带 ``binary_data_base64``。该字段由 Host
        对 chat.receive.* hook 默认序列化提供（主程序 hook_payloads.serialize_session_message
        固定按 include_binary_data=True 序列化 message），**与上面装饰器里声明的
        include_binary_data=True 无关**——当前 Host 并不读取该 metadata。保留这一声明仅为前向
        表意：若未来 Host 改为按 metadata 决定是否下发二进制数据，本 hook 已声明所需、行为不变。
        """
        return await self._dispatcher.dispatch(message)


def create_plugin() -> CustomCommandsPlugin:
    """创建自定义命令插件实例。"""
    return CustomCommandsPlugin()
