"""命令业务层：增删查与动态应答的统一编排（写命令的唯一入口）。

``CommandService`` 执行式——持 plugin 弱引用，直接 ``ctx.send`` 发回执，返回
``(ok, log, intercept)`` 三元组供 @Command handler 透传。编排 scope/storage/images/forward
四个能力模块；admin 校验与作用域解析在此统一，消除文本添加(add_text)与带图添加
(add_image)两条写路径在编排层面的重复。

⚠️ 三元组第三项是 Host 的 ``intercept_message_level``（不是"成本/优先级"，勿照字面误解）：
Host 按 ``bool()`` 解释它——见主程序 ``chat/message_receive/bot.py``::
``continue_process = not bool(intercept_message_level)``。本插件内置命令处理完即应拦截后续主链
（命令已自行回执，不该再走 LLM/Maisaka 回复），故所有返回恒取 ``1``（拦截）。若改成 ``0``，
命令执行后消息会继续落入 LLM 被"二次 AI 回复"。
"""

from __future__ import annotations

import base64
import logging
import re
from typing import TYPE_CHECKING, Optional, Tuple

from .common import (
    ADAPTER_IMAGE_FALLBACK_TEXTS,
    build_list_header_text,
    build_scope_desc,
    is_reserved_trigger,
    looks_like_image_response,
    resolve_scope_id,
)

if TYPE_CHECKING:
    from ..plugin import CustomCommandsPlugin

logger = logging.getLogger(__name__)


class CommandService:
    """自定义命令的增删查与动态应答业务。"""

    def __init__(self, plugin: "CustomCommandsPlugin") -> None:
        self._plugin = plugin

    def _resolve_scope(self, group_id: str, user_id: str) -> Tuple[str, str]:
        """两路共用的作用域解析：返回 (原始 scope_id, 映射后的数据分区名)。"""
        scope_id = resolve_scope_id(group_id, user_id)
        return scope_id, self._plugin._scope_resolver.resolve(scope_id)

    async def _send_text(self, text: str, stream_id: str, *, context: str = "回执") -> bool:
        """发送文本并统一记录失败；调用方仍按原业务语义返回。"""
        try:
            send_ok = await self._plugin.ctx.send.text(text, stream_id)
        except Exception as exc:
            logger.warning("%s发送异常: %s", context, exc, exc_info=True)
            return False
        if send_ok is False:
            logger.warning("%s发送失败：send.text 返回 False（可能被风控或连接异常）", context)
            return False
        return True

    # ===== 添加 =====

    async def add_text(self, matched_groups: Optional[dict], stream_id: str,
                       group_id: str, user_id: str) -> Tuple[bool, str, int]:
        """添加文本命令：<前缀>问：触发词答：回复内容。"""
        p = self._plugin
        # 能进入命令路径即说明含前缀的 pattern 已匹配，无需再校验前缀；matched_groups 异常时兜底。
        if not matched_groups:
            return False, "缺少匹配参数", 1

        if not p._check_admin(user_id):
            await self._send_text("❌ 你没有权限执行此管理员命令", stream_id, context="无权限提示")
            return False, f"用户 {user_id} 无权限", 1

        trigger = matched_groups.get("trigger", "").strip()
        response = matched_groups.get("response", "").strip()

        if not trigger or not response:
            prefix = p.config.settings.command_prefix
            await self._send_text(
                f"❌ 命令格式错误，请使用：{prefix}问：触发词答：回复内容", stream_id,
                context="格式错误提示",
            )
            return False, "格式错误", 1

        # 图片下载失败拦截：带图添加时若适配器下载图片失败，image/emoji 段会降级成
        # "[image]"/"[emoji]" 文本，绕过带图逻辑落到这条纯文本添加路径。此时 response 恰为
        # 占位符本身，说明用户本意是发图而非发这段字面文本，拦截并提示重试，避免存成幽灵命令。
        if response in ADAPTER_IMAGE_FALLBACK_TEXTS:
            await self._send_text(
                "❌ 图片获取失败（可能下载超时或被风控），请重新发送「添加命令 + 图片」", stream_id,
                context="图片获取失败提示",
            )
            return False, "图片下载失败占位符", 1

        # 输入长度校验
        if len(trigger) > p.config.settings.max_trigger_length:
            await self._send_text(
                f"❌ 触发词过长（最多 {p.config.settings.max_trigger_length} 字符）", stream_id,
                context="触发词过长提示",
            )
            return False, "触发词过长", 1
        if len(response) > p.config.settings.max_response_length:
            await self._send_text(
                f"❌ 回复内容过长（最多 {p.config.settings.max_response_length} 字符）", stream_id,
                context="回复内容过长提示",
            )
            return False, "回复内容过长", 1

        # 触发词命中 hook 的 reserved 列表时，写入会变成幽灵数据：
        # hook 见到 `.<reserved>` 直接 return None 让给 @Command，动态分发永不触达。
        if is_reserved_trigger(trigger):
            await self._send_text(
                f"❌ 触发词「{trigger}」与内置命令冲突，请换一个", stream_id,
                context="保留词冲突提示",
            )
            return False, "触发词为保留词", 1

        # 图片路径安全校验：在写入前拒绝含路径穿越的回复内容；
        # 路径合法但文件尚不存在时不阻止添加（允许"先建命令、后放图"），仅在回执里追加提醒，
        # 避免用户拼错文件名后、直到触发命令时才发现"找不到图片"。
        missing_image_hint = ""
        if looks_like_image_response(response):
            image_path = p._images.safe_path(response)
            if image_path is None:
                logger.warning("添加命令时检测到路径穿越尝试: '%s'", response)
                await self._send_text("❌ 图片路径不合法，不允许包含路径穿越", stream_id, context="路径非法提示")
                return False, "路径穿越被阻止", 1
            if not image_path.exists():
                missing_image_hint = (
                    f"\n⚠️ 注意：图片「{response}」当前不在图片目录中，"
                    "请将其放入图片目录后再触发，否则会提示找不到图片。"
                )

        scope_id, scope_used = self._resolve_scope(group_id, user_id)

        try:
            orphan = await p._data_manager.add(
                trigger, response, scope_used,
                max_per_scope=p.config.settings.max_commands_per_scope,
            )
        except ValueError as exc:
            await self._send_text(f"❌ {exc}", stream_id, context="命令数量超限提示")
            return False, "命令数量超限", 1
        except OSError as exc:
            logger.error("保存命令数据失败: %s", exc, exc_info=True)
            await self._send_text(
                "❌ 命令保存失败（磁盘写入异常），未生效，请稍后重试", stream_id,
                context="保存失败提示",
            )
            return False, "保存失败", 1

        # 覆盖旧命令导致旧图片文件失去全部引用时顺手回收（仅清理插件自动生成的 cc_ 文件）
        if orphan:
            await p._images.cleanup_orphan_locked(orphan, p._data_manager)

        scope_desc = build_scope_desc(scope_id, scope_used)

        await self._send_text(
            f"✅ 成功添加自定义命令{scope_desc}！\n触发词：{trigger}\n回复内容：{response}{missing_image_hint}",
            stream_id,
            context="添加命令成功提示",
        )
        logger.info("用户 '%s' 在作用域 '%s' 添加命令: '%s'", user_id, scope_used, trigger)
        return True, "添加成功", 1

    async def add_image(self, trigger: str, b64_data: str, url_hint: str,
                        stream_id: str, group_id: str, user_id: str,
                        ignored_image_count: int = 0) -> Tuple[bool, str, int]:
        """添加图片命令：把消息内图片落盘并绑定触发词。

        ``trigger`` / ``b64_data`` / ``url_hint`` 由 DynamicDispatcher 从入站消息解析后传入。
        触发词校验与 add_text 同源（长度 + 保留词）；图片字节的解码/空判定/大小校验在此完成。
        触发词已存在时直接覆盖（与 add_text 语义一致）。``ignored_image_count`` 为同条消息中
        被忽略的额外图片数（仅取第一张），>0 时在成功回执里提示用户。
        """
        p = self._plugin
        if not p._check_admin(user_id):
            await self._send_text("❌ 你没有权限执行此管理员命令", stream_id, context="无权限提示")
            return False, f"用户 {user_id} 无权限", 1

        if not trigger:
            await self._send_text("❌ 触发词不能为空", stream_id, context="触发词为空提示")
            return False, "触发词为空", 1
        if len(trigger) > p.config.settings.max_trigger_length:
            await self._send_text(
                f"❌ 触发词过长（最多 {p.config.settings.max_trigger_length} 字符）", stream_id,
                context="触发词过长提示",
            )
            return False, "触发词过长", 1
        if is_reserved_trigger(trigger):
            await self._send_text(
                f"❌ 触发词「{trigger}」与内置命令冲突，请换一个", stream_id,
                context="保留词冲突提示",
            )
            return False, "触发词为保留词", 1

        if not b64_data:
            await self._send_text(
                "❌ 没能获取到图片数据，请重试，或改用「文件名」方式添加", stream_id,
                context="无图片数据提示",
            )
            return False, "无图片数据", 1

        max_size = p.config.settings.max_image_size
        # 去除全部空白（含内部换行）而非仅首尾：validate=True 会拒绝任何非 base64 字母表字符。
        # 当前 napcat 给的是无换行标准 base64，但若换用按 76 字符折行（MIME 风格）的来源，仅
        # strip 首尾会让中间换行触发解码失败；统一清空白做前向加固，不改变现有 napcat 行为。
        normalized_b64 = re.sub(r"\s", "", b64_data)
        # 解码前按 base64 长度粗筛超大图，避免对明显超限的大字符串做无谓的 b64decode（省内存/CPU）；
        # 真正的精确校验由下方解码后的 len(image_bytes) > max_size 兜底。
        # base64 每 4 字符编码 3 字节 → 解码后字节数 ≈ len(b64) * 3 // 4，此估算比真实值最多高估 2
        # （末尾 1~2 个 "=" padding）；+2 即补偿该高估，确保只在确定超限时才拒绝、不误杀临界图片。
        if (len(normalized_b64) * 3) // 4 > max_size + 2:
            limit_mb = max_size / (1024 * 1024)
            await self._send_text(
                f"❌ 图片文件过大（上限 {limit_mb:.0f}MB）", stream_id,
                context="图片过大提示",
            )
            return False, "图片过大", 1

        try:
            image_bytes = base64.b64decode(normalized_b64, validate=True)
        except Exception:
            await self._send_text("❌ 图片数据解码失败", stream_id, context="图片解码失败提示")
            return False, "图片解码失败", 1
        if not image_bytes:
            await self._send_text("❌ 图片数据为空，请重试", stream_id, context="图片数据为空提示")
            return False, "图片数据为空", 1

        if len(image_bytes) > max_size:
            size_mb = len(image_bytes) / (1024 * 1024)
            limit_mb = max_size / (1024 * 1024)
            await self._send_text(
                f"❌ 图片文件过大（{size_mb:.1f}MB，上限 {limit_mb:.0f}MB）", stream_id,
                context="图片过大提示",
            )
            return False, "图片过大", 1

        scope_id, scope_used = self._resolve_scope(group_id, user_id)
        filename = p._images.managed_filename_for(image_bytes, url_hint)
        orphan: Optional[str] = None
        async with p._images.managed_file_lock(filename):
            try:
                filename = await p._images.store_prepared(image_bytes, filename)
            except Exception as exc:
                logger.error("保存带图命令的图片失败: %s", exc, exc_info=True)
                await self._send_text("❌ 保存图片时发生内部错误", stream_id, context="保存图片失败提示")
                return False, "保存图片失败", 1

            try:
                orphan = await p._data_manager.add(
                    trigger, filename, scope_used,
                    max_per_scope=p.config.settings.max_commands_per_scope,
                )
            except ValueError as exc:
                # 命令数超限：本次已落盘的图未能写入任何命令，若不被现有命令引用则回收，避免孤儿堆积。
                # 同 hash 图可能已被其它触发词引用，故须判断引用计数；这里仍持有文件级锁，避免清理
                # 与另一个同 hash 图片的保存/绑定交错。
                await p._images.cleanup_orphan_locked(filename, p._data_manager, file_lock_held=True)
                await self._send_text(f"❌ {exc}", stream_id, context="命令数量超限提示")
                return False, "命令数量超限", 1
            except OSError as exc:
                # 保存失败：add 已回滚内存（命令未写入），本次落盘图成孤儿，按超限同样回收；
                # 仍持文件级锁，回收与并发同 hash 保存/绑定串行化。
                await p._images.cleanup_orphan_locked(filename, p._data_manager, file_lock_held=True)
                logger.error("保存图片命令数据失败: %s", exc, exc_info=True)
                await self._send_text(
                    "❌ 命令保存失败（磁盘写入异常），未生效，请稍后重试", stream_id,
                    context="保存失败提示",
                )
                return False, "保存失败", 1

        # 覆盖旧图片命令时回收失去引用的旧图；同图 hash 相同则 old==new，不会误删本次刚存的图
        if orphan:
            await p._images.cleanup_orphan_locked(orphan, p._data_manager)

        scope_desc = build_scope_desc(scope_id, scope_used)

        cmd_prefix = p.config.settings.command_prefix
        # 带图添加仅取第一张：用户同条消息附带多张有效图片时，在成功回执里明确提示，
        # 避免误以为多张都已绑定（ignored_image_count 由 DynamicDispatcher 统计后传入）。
        multi_image_hint = (
            f"\n⚠️ 检测到 {ignored_image_count + 1} 张图片，仅保存了第一张"
            if ignored_image_count > 0 else ""
        )
        await self._send_text(
            f"✅ 成功添加图片命令{scope_desc}！\n"
            f"触发词：{trigger}\n"
            f"发送 {cmd_prefix}{trigger} 即可获取这张图片{multi_image_hint}",
            stream_id,
            context="添加图片命令成功提示",
        )
        logger.info(
            "用户 '%s' 在作用域 '%s' 通过消息内图片添加命令: '%s' -> %s",
            user_id, scope_used, trigger, filename,
        )
        return True, "添加成功", 1

    # ===== 删除 =====

    async def delete(self, matched_groups: Optional[dict], stream_id: str,
                     group_id: str, user_id: str) -> Tuple[bool, str, int]:
        """删除命令：<前缀>删：触发词。"""
        p = self._plugin
        if not matched_groups:
            return False, "缺少匹配参数", 1

        if not p._check_admin(user_id):
            await self._send_text("❌ 你没有权限执行此管理员命令", stream_id, context="无权限提示")
            return False, f"用户 {user_id} 无权限", 1

        trigger = matched_groups.get("trigger", "").strip()
        _, current_scope = self._resolve_scope(group_id, user_id)
        try:
            success, orphan = await p._data_manager.delete(trigger, current_scope)
        except OSError as exc:
            logger.error("保存命令数据失败: %s", exc, exc_info=True)
            await self._send_text(
                "❌ 命令删除失败（磁盘写入异常），未生效，请稍后重试", stream_id,
                context="保存失败提示",
            )
            return False, "保存失败", 1

        if success:
            if orphan:
                await p._images.cleanup_orphan_locked(orphan, p._data_manager)
            await self._send_text(
                f"✅ 成功删除了自定义命令（作用域: {current_scope}）：'{trigger}'",
                stream_id,
                context="删除命令成功提示",
            )
            return True, "删除成功", 1

        msg = f"❌ 未在当前作用域 [{current_scope}] 找到命令：'{trigger}'"

        if (
            current_scope != "global"
            and p._data_manager.has_global(trigger)
        ):
            prefix = p.config.settings.command_prefix
            msg += f"\n💡 提示：这是一个【全局命令】。可使用 {prefix}删全局：{trigger} 来删除。"

        await self._send_text(msg, stream_id, context="命令未找到提示")
        return False, "命令未找到", 1

    async def delete_global(self, matched_groups: Optional[dict], stream_id: str,
                            user_id: str) -> Tuple[bool, str, int]:
        """删除全局命令：<前缀>删全局：触发词。"""
        p = self._plugin
        if not matched_groups:
            return False, "缺少匹配参数", 1

        if not p._check_admin(user_id):
            await self._send_text("❌ 你没有权限执行此管理员命令", stream_id, context="无权限提示")
            return False, f"用户 {user_id} 无权限", 1

        trigger = matched_groups.get("trigger", "").strip()
        try:
            success, orphan = await p._data_manager.delete_global(trigger)
        except OSError as exc:
            logger.error("保存命令数据失败: %s", exc, exc_info=True)
            await self._send_text(
                "❌ 命令删除失败（磁盘写入异常），未生效，请稍后重试", stream_id,
                context="保存失败提示",
            )
            return False, "保存失败", 1

        if success:
            if orphan:
                await p._images.cleanup_orphan_locked(orphan, p._data_manager)
            await self._send_text(
                f"✅ 成功删除了全局自定义命令：'{trigger}'", stream_id,
                context="删除全局命令成功提示",
            )
            logger.info("用户 '%s' 删除全局命令: '%s'", user_id, trigger)
            return True, "全局删除成功", 1

        await self._send_text(
            f"❌ 未在全局作用域找到命令：'{trigger}'", stream_id,
            context="全局命令未找到提示",
        )
        return False, "全局命令未找到", 1

    # ===== 列表 =====

    async def build_list(self, stream_id: str, group_id: str,
                         user_id: str) -> Tuple[bool, str, int]:
        """列出命令：<前缀>列表。优先合并转发，任何失败降级纯文本。"""
        p = self._plugin
        scope_id, current_scope = self._resolve_scope(group_id, user_id)
        triggers = p._data_manager.get_triggers_for_scope(current_scope)
        prefix = p.config.settings.command_prefix

        if not triggers:
            await self._send_text(
                f"🤷‍♀️ 当前作用域 [{current_scope}] 下没有可用的自定义命令",
                stream_id,
                context="空列表提示",
            )
            return True, "列表已发送", 1

        header_text = build_list_header_text(scope_id, current_scope)
        # triggers 已在 get_triggers_for_scope 中排序
        list_content = "\n".join(f"▪️ {prefix}{trigger}" for trigger in triggers)
        # 优先用 Napcat 合并转发；任何失败（目标 ID 非法、转发被风控、内部异常）
        # 都降级为纯文本列表，保证用户至少能拿到命令清单。
        forward_failure: Optional[str] = None
        try:
            forward_failure = await p._forward.send_list(
                header_text, list_content, group_id, user_id,
                triggers=triggers, prefix=prefix,
            )
        except ValueError as exc:
            logger.warning("发送命令列表时目标 ID 非法: %s", exc)
            forward_failure = str(exc)
        except Exception as exc:
            logger.error("发送命令列表时发生异常: %s", exc, exc_info=True)
            forward_failure = "内部错误"

        if forward_failure:
            logger.warning("合并转发发送失败(%s)，降级为纯文本列表", forward_failure)
            try:
                text_ok = await p._forward.send_as_text(header_text, list_content, stream_id)
            except Exception as exc:
                logger.error("纯文本列表降级发送也失败: %s", exc, exc_info=True)
                await self._send_text("❌ 发送命令列表失败", stream_id, context="列表失败提示")
                return False, "列表发送失败", 1
            # send.text 业务失败时返回 False 而非抛异常；合并转发与纯文本两条路径都没送达列表，
            # 此时再发任何文案大概率同样失败，故仅记日志并以失败结果收尾，避免静默成功。
            if text_ok is False:
                logger.warning("纯文本列表降级发送返回 False（可能被风控），列表未送达")
                return False, "列表发送失败", 1

        return True, "列表已发送", 1

    # ===== 动态命中应答 =====

    async def respond(self, trigger: str, stream_id: str,
                      group_id: str, user_id: str) -> bool:
        """动态触发命中后的应答：图片走 ImageStore.dispatch_response，文本走 ctx.send。

        Returns:
            bool: 命中并已应答返回 True（调用方据此 abort）；未注册返回 False（放行）。
        """
        p = self._plugin
        _, current_scope = self._resolve_scope(group_id, user_id)
        response_value = p._data_manager.get(trigger, current_scope)
        if response_value is None:
            return False

        if looks_like_image_response(response_value):
            try:
                await p._images.dispatch_response(response_value, stream_id)
            except Exception as exc:
                logger.error("动态命令 '%s' 图片回复发送异常: %s", trigger, exc, exc_info=True)
        else:
            # 命中即 abort；发送异常也不能让 hook 被 ErrorPolicy.SKIP 放行到 LLM 主链。
            await self._send_text(response_value, stream_id, context=f"动态命令 '{trigger}' 文本回复")
        return True
