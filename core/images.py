"""图片资源模块：自定义命令的图片存取、安全路径与投递。

``ImageStore`` 持 plugin 弱引用，经 ``self._plugin.config`` / ``ctx`` / ``_plugin_dir``
访问依赖。封装一条咬合的链：image_directory 解析 → 路径穿越防御 → 内容 hash 落盘
（同图去重）→ 孤儿回收 → 读盘 base64 编码 → 把图片回复发出去（含各类失败回执）。
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import logging
import os
import re
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Any, AsyncIterator, Optional, Tuple

from .common import IMAGE_EXTENSIONS

if TYPE_CHECKING:
    from ..plugin import CustomCommandsPlugin

logger = logging.getLogger(__name__)

# 带图添加自动落盘的图片文件名格式：cc_<16位 sha256 前缀><扩展名>（见 _save_bytes_sync）。
# 仅这类「插件自动生成」的文件才在命令删除/覆盖后做孤儿回收；用户手动放进 image_directory
# 的图片（如 hello.png）不匹配此模式，永远不会被自动删除。
_MANAGED_IMAGE_FILE_RE = re.compile(r"^cc_[0-9a-f]{16}\.(?:png|jpe?g|gif|webp)$")


class ImageStore:
    """图片资源的安全存取与投递。"""

    def __init__(self, plugin: "CustomCommandsPlugin") -> None:
        self._plugin = plugin
        # 托管图片的文件级锁，按文件名串行化同一 hash 图片的保存/绑定/清理；配套使用者计数，
        # 在最后一个使用者退出后连同锁一并回收，避免该表随历史上出现过的不同图片无界增长。
        self._managed_file_locks: dict[str, asyncio.Lock] = {}
        self._managed_file_lock_users: dict[str, int] = {}
        self._warned_absolute_image_dir: str = ""

    @asynccontextmanager
    async def managed_file_lock(self, filename: str) -> AsyncIterator[None]:
        """同一托管图片文件的保存、命令绑定与孤儿清理必须共用这把锁。

        锁按需创建并做使用者计数：进入时登记、退出时注销，计数归零即连同锁一起从表中移除，
        使 ``_managed_file_locks`` 不会随出现过的不同图片无界增长。计数的增减都在 await 边界
        之外完成（asyncio 单线程内即原子），故进入时的"取锁+登记"与退出时的"注销+回收"各自
        不可分割：等待同一把锁的后到协程必然已先完成登记，计数不会在仍有等待者时归零，因此
        不存在"锁被提前回收、后到协程另建新锁导致失去互斥"的竞态。
        """
        lock = self._managed_file_locks.get(filename)
        if lock is None:
            lock = asyncio.Lock()
            self._managed_file_locks[filename] = lock
        self._managed_file_lock_users[filename] = self._managed_file_lock_users.get(filename, 0) + 1
        try:
            async with lock:
                yield
        finally:
            remaining = self._managed_file_lock_users.get(filename, 0) - 1
            if remaining > 0:
                self._managed_file_lock_users[filename] = remaining
            else:
                self._managed_file_lock_users.pop(filename, None)
                self._managed_file_locks.pop(filename, None)

    def resolve_dir(self) -> Path:
        """将配置中的 image_directory 解析为绝对 Path。
        相对路径基于插件目录解析，绝对路径直接使用。
        """
        configured = self._plugin.config.settings.image_directory
        path = Path(configured)
        if path.is_absolute():
            normalized = str(path)
            if normalized != self._warned_absolute_image_dir:
                logger.warning(
                    "image_directory 当前使用绝对路径 %s；请勿配置为磁盘根目录、系统目录或大型共享目录",
                    normalized,
                )
                self._warned_absolute_image_dir = normalized
        if not path.is_absolute():
            base = Path(self._plugin._plugin_dir) if self._plugin._plugin_dir else Path.cwd()
            path = base / path
        return path.resolve()

    def safe_path(self, response: str) -> Optional[Path]:
        """将回复内容解析为 image_directory 内的安全路径。

        Returns:
            合法时返回解析后的绝对 Path；包含路径穿越或越界时返回 None。
        """
        image_base_dir = self.resolve_dir()
        image_path = (image_base_dir / response).resolve()
        try:
            image_path.relative_to(image_base_dir)
        except ValueError:
            return None
        return image_path

    @staticmethod
    def _is_managed_file(value: str) -> bool:
        """判断回复内容是否为带图添加自动落盘的图片文件名。

        仅匹配 ``cc_<hash><ext>`` 这类插件生成的文件；用户手动放进 image_directory
        的图片（如 ``hello.png``）不匹配，避免孤儿回收误删用户自带资源。
        """
        return bool(_MANAGED_IMAGE_FILE_RE.match(value or ""))

    @staticmethod
    def guess_extension(data: bytes, url_hint: str = "") -> str:
        """优先按图片二进制魔数判断扩展名，回退 URL 后缀，再回退 .png。

        返回值始终落在 IMAGE_EXTENSIONS 内，确保后续 looks_like_image_response 能识别。
        """
        if data[:8] == b"\x89PNG\r\n\x1a\n":
            return ".png"
        if data[:3] == b"\xff\xd8\xff":
            return ".jpg"
        if data[:6] in (b"GIF87a", b"GIF89a"):
            return ".gif"
        if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
            return ".webp"
        hint = (url_hint or "").lower()
        for ext in IMAGE_EXTENSIONS:
            if ext in hint:
                return ".jpg" if ext == ".jpeg" else ext
        return ".png"

    @staticmethod
    def has_image_magic(data: bytes) -> bool:
        """``data`` 是否以已知图片格式的魔数开头（PNG / JPEG / GIF / WebP）。

        带图添加的二进制理应是真实图片，用它在落盘前拦截非图片内容，避免把未知字节
        按 ``guess_extension`` 的兜底回退存成 ``.png``（换适配器或适配器异常时可能发生）。
        与 ``guess_extension`` 共用同一组魔数判断，两者须同步维护。
        """
        return (
            data[:8] == b"\x89PNG\r\n\x1a\n"
            or data[:3] == b"\xff\xd8\xff"
            or data[:6] in (b"GIF87a", b"GIF89a")
            or (data[:4] == b"RIFF" and data[8:12] == b"WEBP")
        )

    def managed_filename_for(self, data: bytes, url_hint: str = "") -> str:
        """按图片内容生成托管文件名，供调用方在保存前先获取文件级锁。"""
        ext = self.guess_extension(data, url_hint)
        digest = hashlib.sha256(data).hexdigest()[:16]
        return f"cc_{digest}{ext}"

    def _save_bytes_sync(self, data: bytes, filename: str) -> str:
        """把图片字节落盘到 image_directory，文件名按内容 hash 生成（同图去重）。

        同步 I/O，须经 asyncio.to_thread 调用。返回相对文件名（存入 commands 作 response）。
        采用"临时文件 + 原子重命名"，避免写入中途崩溃留下半截文件。
        """
        image_dir = self.resolve_dir()
        image_dir.mkdir(parents=True, exist_ok=True)
        target = image_dir / filename
        # 临时文件名加入随机后缀，避免同图并发保存时多个任务争用同一个 .tmp。
        tmp_path = image_dir / f".{filename}.{uuid.uuid4().hex}.tmp"
        try:
            with open(tmp_path, "wb") as f:
                f.write(data)
                f.flush()
                os.fsync(f.fileno())
            tmp_path.replace(target)
        except OSError:
            try:
                tmp_path.unlink(missing_ok=True)
            except OSError:
                pass
            raise
        return filename

    async def store_prepared(self, image_bytes: bytes, filename: str) -> str:
        """在调用方已持有 ``managed_file_lock(filename)`` 时落盘指定托管文件名。"""
        return await asyncio.to_thread(self._save_bytes_sync, image_bytes, filename)

    async def _send_error_text(self, text: str, stream_id: str, *, context: str) -> bool:
        """发送图片错误提示并吞掉发送异常，避免错误路径再次抛出。"""
        try:
            send_ok = await self._plugin.ctx.send.text(text, stream_id)
        except Exception as exc:
            logger.warning("%s发送异常: %s", context, exc, exc_info=True)
            return False
        if send_ok is False:
            logger.warning("%s发送失败：send.text 返回 False（可能被风控或连接异常）", context)
            return False
        return True

    async def cleanup_orphan_locked(
        self, filename: str, data_manager: Any, *, file_lock_held: bool = False,
    ) -> None:
        """带图添加超限失败时的孤儿回收：在数据写锁内原子完成"判断无引用 → 删除"。

        只回收插件托管（``cc_<hash>``）且路径安全的文件，并把文件级锁、引用判断与删除
        合并起来：先串行化同一文件名的保存/绑定/清理，再在
        ``data_manager`` 的写锁内执行 ``cleanup_if_unreferenced``，消除并发添加同一张图时
        "判断未引用"与"删除"之间被插入引用而误删的 TOCTOU 窗口。删除失败仅记日志。
        ``data_manager`` 即 ``CommandDataManager`` 实例（duck typing，仅调 cleanup_if_unreferenced）。
        """
        if not filename or not self._is_managed_file(filename):
            return
        image_path = self.safe_path(filename)
        if image_path is None:
            return

        async def _cleanup_after_file_lock() -> None:
            def _unlink() -> None:
                # 删除在写锁内同步执行（单文件 unlink 极快）；自吞 OSError 不让异常穿透写锁。
                try:
                    image_path.unlink(missing_ok=True)
                except OSError as exc:
                    logger.warning("清理孤儿图片文件 '%s' 失败: %s", filename, exc)

            deleted = await data_manager.cleanup_if_unreferenced(filename, _unlink)
            if deleted:
                logger.info("已清理无引用的孤儿图片文件: %s", filename)

        if file_lock_held:
            await _cleanup_after_file_lock()
            return
        async with self.managed_file_lock(filename):
            await _cleanup_after_file_lock()

    @staticmethod
    def _read_and_encode_sync(
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

    async def dispatch_response(self, response_value: str, stream_id: str) -> None:
        """图片回复的完整链路：路径安全 → 存在 → 大小校验 → 读盘编码 → 发送。

        所有失败路径都向用户回发错误文案——hook 已经决定 abort，错误也算"已处理"。
        """
        p = self._plugin
        image_path = self.safe_path(response_value)
        if image_path is None:
            logger.warning("检测到路径穿越尝试: '%s'", response_value)
            await self._send_error_text("❌ 图片路径不合法", stream_id, context="图片路径非法提示")
            return

        if not image_path.exists():
            # 仅向用户展示文件名，不泄露服务器内部路径
            await self._send_error_text(
                f"❌ 找不到图片文件 '{response_value}'", stream_id,
                context="图片不存在提示",
            )
            logger.warning("图片文件不存在: %s", image_path)
            return

        # 同步 I/O（stat + read + base64 编码）丢线程池跑，避免 10MB 级图片阻塞事件循环
        max_image_size = p.config.settings.max_image_size
        b64_img_data, encode_error = await asyncio.to_thread(
            self._read_and_encode_sync, image_path, max_image_size,
        )
        if encode_error:
            if encode_error.startswith("OVERSIZE:"):
                try:
                    actual_size = int(encode_error.split(":", 1)[1])
                except ValueError:
                    actual_size = 0
                size_mb = actual_size / (1024 * 1024)
                limit_mb = max_image_size / (1024 * 1024)
                await self._send_error_text(
                    f"❌ 图片文件过大（{size_mb:.1f}MB，上限 {limit_mb:.0f}MB）",
                    stream_id,
                    context="图片过大提示",
                )
                return
            logger.error("读取图片失败: %s", encode_error)
            await self._send_error_text("❌ 读取图片文件时发生错误", stream_id, context="图片读取失败提示")
            return

        try:
            send_ok = await p.ctx.send.image(b64_img_data, stream_id)
        except Exception as e:
            logger.error("发送动态图片失败: %s", e)
            await self._send_error_text("❌ 发送图片时发生内部错误", stream_id, context="图片发送异常提示")
            return
        # ctx.send.image 业务失败时返回 False 而非抛异常（见 SDK context.py
        # _BOOLEAN_SUCCESS_CAPABILITIES）；此时连接通常正常、错误文案能发出，显式告知用户，
        # 避免「图没发出去、也没有任何反馈」的静默失败。仅在明确返回 False 时提示，
        # 其余返回形态（True / 兼容旧 Host 的原始结果）按成功处理，不误报。
        if send_ok is False:
            logger.warning("发送动态图片失败：send.image 返回 False（可能被风控或格式不受支持）")
            await self._send_error_text(
                "❌ 图片发送失败，可能被风控或格式不受支持", stream_id,
                context="图片发送失败提示",
            )
