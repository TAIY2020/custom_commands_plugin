"""图片资源模块：自定义命令的图片存取、安全路径与投递。

``ImageStore`` 持 plugin 弱引用，经 ``self._plugin.config`` / ``ctx`` / ``_plugin_dir``
访问依赖。封装一条咬合的链：image_directory 解析 → 路径穿越防御 → 内容 hash 落盘
（同图去重）→ 孤儿回收 → 读盘 base64 编码 → 把图片回复发出去（含各类失败回执）。

文件身份：托管文件（``cc_<hash>.<ext>``）在引用比较、文件级锁、孤儿判定与最终删除四处
统一使用 ``canonical_managed_name`` 规范化后的纯文件名，``./cc_x.png`` / ``CC_X.PNG`` /
``<image_dir>/cc_x.png`` 都视为同一文件；删除路径在数据写锁内、按删除时刻的目录配置计算。
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

from .common import IMAGE_EXTENSIONS, looks_like_image_response

if TYPE_CHECKING:
    from ..plugin import CustomCommandsPlugin

logger = logging.getLogger(__name__)

# 带图添加自动落盘的图片文件名格式：cc_<16位 sha256 前缀><扩展名>（见 _save_bytes_sync）。
# 仅这类「插件自动生成」的文件才在命令删除/覆盖后做孤儿回收；用户手动放进 image_directory
# 的图片（如 hello.png）不匹配此模式，永远不会被自动删除。
_MANAGED_IMAGE_FILE_RE = re.compile(r"^cc_[0-9a-f]{16}\.(?:png|jpe?g|gif|webp)$")


def normalize_relative_image_path(value: str) -> str:
    """回复内容里的图片路径做纯字符串规范化：去首尾空白、统一分隔符、折叠 ``./`` 与 ``sub/..``。

    不碰文件系统。所有"文件身份"判断（托管名识别、引用键、锁键、清理）都先经此函数，
    保证 ``cc_x.png`` / ``./cc_x.png`` / ``sub/../cc_x.png`` 得到同一结果。
    """
    text = (value or "").strip().replace("\\", "/")
    if not text:
        return ""
    return os.path.normpath(text)


def canonical_managed_name(value: str) -> Optional[str]:
    """把回复内容规范成托管文件名；非托管文件返回 None。纯字符串运算，不碰文件系统。

    ``./cc_x.png``、``.\\cc_x.png``、``sub/../cc_x.png``、``CC_X.PNG`` → ``cc_x.png``。
    规范化后仍含目录分量的一律视为非托管（托管文件永远直接落在 image_directory 根下）。
    """
    normalized = normalize_relative_image_path(value)
    if not normalized or os.sep in normalized or "/" in normalized:
        return None
    lowered = normalized.lower()
    return lowered if _MANAGED_IMAGE_FILE_RE.match(lowered) else None


class ImageStore:
    """图片资源的安全存取与投递。"""

    def __init__(self, plugin: "CustomCommandsPlugin") -> None:
        self._plugin = plugin
        # 托管图片的文件级锁，按规范化文件名串行化同一 hash 图片的保存/绑定/清理；配套使用者计数，
        # 在最后一个使用者退出后连同锁一并回收，避免该表随历史上出现过的不同图片无界增长。
        self._managed_file_locks: dict[str, asyncio.Lock] = {}
        self._managed_file_lock_users: dict[str, int] = {}
        self._warned_absolute_image_dir: str = ""
        # resolve_dir 结果缓存：键为 (配置值, 数据目录)，配置热更新改了 image_directory 键即失效。
        # Path.resolve() 在网络盘上是多次系统调用，引用比较/清理/发送都会用到目录，不能每次都解析。
        self._resolved_dir_cache: Optional[Tuple[Tuple[str, str], Path]] = None

    @asynccontextmanager
    async def managed_file_lock(self, filename: str) -> AsyncIterator[None]:
        """同一托管图片文件的保存、命令绑定与孤儿清理必须共用这把锁（按规范化文件名）。

        锁按需创建并做使用者计数：进入时登记、退出时注销，计数归零即连同锁一起从表中移除，
        使 ``_managed_file_locks`` 不会随出现过的不同图片无界增长。计数的增减都在 await 边界
        之外完成（asyncio 单线程内即原子），故进入时的"取锁+登记"与退出时的"注销+回收"各自
        不可分割：等待同一把锁的后到协程必然已先完成登记，计数不会在仍有等待者时归零，因此
        不存在"锁被提前回收、后到协程另建新锁导致失去互斥"的竞态。
        """
        key = canonical_managed_name(filename) or filename
        lock = self._managed_file_locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            self._managed_file_locks[key] = lock
        self._managed_file_lock_users[key] = self._managed_file_lock_users.get(key, 0) + 1
        try:
            async with lock:
                yield
        finally:
            remaining = self._managed_file_lock_users.get(key, 0) - 1
            if remaining > 0:
                self._managed_file_lock_users[key] = remaining
            else:
                self._managed_file_lock_users.pop(key, None)
                self._managed_file_locks.pop(key, None)

    # ===== 目录与路径 =====

    def resolve_dir(self) -> Path:
        """将配置中的 image_directory 解析为绝对 Path（带缓存）。
        相对路径基于插件持久数据目录（ctx.paths.data_dir）解析，绝对路径直接使用。
        """
        configured = self._plugin.config.settings.image_directory
        base_dir = self._plugin._data_dir or self._plugin._plugin_dir or str(Path.cwd())
        cache_key = (configured, base_dir)
        if self._resolved_dir_cache is not None and self._resolved_dir_cache[0] == cache_key:
            return self._resolved_dir_cache[1]

        path = Path(configured)
        if path.is_absolute():
            normalized = str(path)
            if normalized != self._warned_absolute_image_dir:
                logger.warning(
                    "image_directory 当前使用绝对路径 %s；请勿配置为磁盘根目录、系统目录或大型共享目录",
                    normalized,
                )
                self._warned_absolute_image_dir = normalized
        else:
            path = Path(base_dir) / path
        resolved = path.resolve()
        self._resolved_dir_cache = (cache_key, resolved)
        return resolved

    def safe_path(self, response: str, *, allow_links: bool = True) -> Optional[Path]:
        """将回复内容解析为 image_directory 内的安全路径（用于读取/存在性检查）。

        ``resolve()`` 会跟随符号链接，故指向目录外的链接会被 ``relative_to`` 拒绝。
        新增引用使用 ``allow_links=False``，拒绝文件链接和目录链接；历史引用仍可读取，
        孤儿清理另做实际文件身份复核，不自动改写历史命令。
        删除操作不要用本方法返回的路径（会删到链接目标），见 ``cleanup_orphan_locked``。

        Returns:
            合法时返回解析后的绝对 Path；包含路径穿越或越界时返回 None。
        """
        image_base_dir = self.resolve_dir()
        unresolved_path = image_base_dir / response
        image_path = unresolved_path.resolve()
        try:
            image_path.relative_to(image_base_dir)
        except ValueError:
            return None
        if not allow_links:
            for candidate in (unresolved_path, *unresolved_path.parents):
                if candidate == image_base_dir:
                    break
                is_junction = getattr(candidate, "is_junction", None)
                if candidate.is_symlink() or (is_junction is not None and is_junction()):
                    return None
        return image_path

    def _strip_base_dir(self, value: str) -> str:
        """绝对路径若落在当前图片目录内，剥掉目录前缀得到相对写法；相对路径原样返回。

        目录来自 ``resolve_dir()`` 的缓存（配置不变时不碰文件系统）。
        """
        text = (value or "").strip()
        if not text or not os.path.isabs(text):
            return text
        base = os.path.normcase(str(self.resolve_dir()))
        normalized = os.path.normcase(os.path.normpath(text))
        prefix = base if base.endswith(os.sep) else base + os.sep
        if normalized.startswith(prefix):
            return normalized[len(prefix):]
        return text

    def reference_key(self, value: str) -> str:
        """回复内容的引用比较键，供 ``CommandDataManager._is_referenced`` 使用。

        纯字符串运算（配置不变时不解析文件系统，可在写锁内对全库逐条调用）：
        - 非图片回复：键即自身。
        - 托管图片：``managed:<规范化文件名>``，``cc_x.png`` / ``./cc_x.png`` / ``sub/../cc_x.png`` /
          绝对路径写法同键。
        - 其它图片：``img:<normcase(规范化相对路径)>``，兼容 Windows 大小写与分隔符差异。
        前缀保证图片键永远不会与某条纯文本回复碰撞。
        """
        if not looks_like_image_response(value):
            return value
        relative = self._strip_base_dir(value)
        managed = canonical_managed_name(relative)
        if managed is not None:
            return "managed:" + managed
        return "img:" + os.path.normcase(normalize_relative_image_path(relative))

    # ===== 图片格式 =====

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
        """按图片内容生成托管文件名（已是规范形态），供调用方在保存前先获取文件级锁。"""
        ext = self.guess_extension(data, url_hint)
        digest = hashlib.sha256(data).hexdigest()[:16]
        return f"cc_{digest}{ext}"

    # ===== 落盘 =====

    def _save_bytes_sync(self, data: bytes, filename: str, image_dir: Path) -> str:
        """把图片字节落盘到 ``image_dir``，文件名按内容 hash 生成（同图去重）。

        同步 I/O，须经 asyncio.to_thread 调用。返回相对文件名（存入 commands 作 response）。
        采用"临时文件 + 原子重命名"，避免写入中途崩溃留下半截文件。
        ``image_dir`` 由调用方在事件循环里解析后传入，调用方据此知道文件实际落在哪个目录，
        绑定命令前可与当时的目录配置比对（目录热更新期间的在途保存不会被静默绑定到旧目录）。

        目标已是普通文件（非符号链接）且内容与本次输入逐字节一致时跳过重写：文件名由内容
        hash 决定，正常情况下同名即同内容，重写只是无谓 I/O，且 Windows 下若恰有
        dispatch_response 正在读同一文件，replace 会撞上共享冲突。仅凭 exists() 不够——现有
        文件可能被手动替换或损坏，故须比对内容；符号链接不复用，replace 会把链接本身换成
        独立普通文件，不触碰链接目标。
        """
        image_dir.mkdir(parents=True, exist_ok=True)
        target = image_dir / filename
        if self._is_identical_regular_file(target, data):
            return filename
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

    @staticmethod
    def _is_identical_regular_file(target: Path, data: bytes) -> bool:
        """``target`` 是否为非链接的普通文件且内容与 ``data`` 完全一致（大小先筛，再逐字节比对）。"""
        try:
            if target.is_symlink() or not target.is_file():
                return False
            if target.stat().st_size != len(data):
                return False
            return target.read_bytes() == data
        except OSError:
            return False

    async def store_prepared(self, image_bytes: bytes, filename: str) -> Tuple[str, Path]:
        """在调用方已持有 ``managed_file_lock(filename)`` 时落盘指定托管文件名。

        Returns:
            (文件名, 实际写入的图片目录)。调用方在绑定命令前应比对该目录与当时的
            ``resolve_dir()``，不一致说明保存期间配置被热更新。
        """
        image_dir = self.resolve_dir()
        saved = await asyncio.to_thread(self._save_bytes_sync, image_bytes, filename, image_dir)
        return saved, image_dir

    # ===== 孤儿回收 =====

    @staticmethod
    def _has_resolved_reference_sync(
        image_directory: Path, candidate_path: Path, responses: Tuple[str, ...],
    ) -> bool:
        """只在字符串引用判定为孤儿后复核实际目标；解析失败时保守保留文件。"""
        try:
            # 非严格解析可能吞掉访问拒绝并返回未解析路径，不能据此断言两个文件不同。
            resolved_candidate = candidate_path.resolve(strict=True)
            for response in responses:
                if not looks_like_image_response(response):
                    continue
                try:
                    resolved_reference = (image_directory / response).resolve(strict=True)
                except FileNotFoundError:
                    # 允许先建命令再放图片；确定不存在的路径不引用这个已经存在的候选文件。
                    continue
                if resolved_reference == resolved_candidate:
                    return True
        except (OSError, RuntimeError, ValueError) as exc:
            logger.warning("无法确认图片 %s 的全部历史引用，跳过清理: %s", candidate_path.name, exc)
            return True
        return False

    async def cleanup_orphan_locked(
        self, filename: str, data_manager: Any, *, file_lock_held: bool = False,
    ) -> None:
        """孤儿回收：在数据写锁内原子完成"判断无引用 → 删除"。

        只回收插件托管（``cc_<hash>``）的文件，文件身份先经 ``canonical_managed_name``
        规范化：``./cc_x.png`` 这类别名作为最后一条引用被删除时同样能回收。
        把文件级锁、引用判断与删除合并起来：先串行化同一文件名的保存/绑定/清理，再在
        ``data_manager`` 的写锁内执行 ``cleanup_if_unreferenced``，消除并发添加同一张图时
        "判断未引用"与"删除"之间被插入引用而误删的 TOCTOU 窗口。
        目录在本次操作中固定；异步复核历史链接时持数据锁，随后复查代际和目录是否变化。
        复核放在线程池里，常规字符串引用扫描不做文件系统查询。目标自身是链接时仍跳过删除。
        """
        image_directory = self.resolve_dir()
        canonical = canonical_managed_name(self._strip_base_dir(filename)) if filename else None
        if canonical is None:
            return
        image_path = image_directory / canonical

        async def _cleanup_after_file_lock() -> None:
            async def _has_other_reference(responses: Tuple[str, ...]) -> bool:
                return await asyncio.to_thread(
                    self._has_resolved_reference_sync, image_directory, image_path, responses,
                )

            def _unlink() -> bool:
                if self.resolve_dir() != image_directory:
                    logger.info("图片目录已变更，放弃本次孤儿清理: %s", canonical)
                    return False
                try:
                    if image_path.is_symlink():
                        logger.warning("孤儿图片 '%s' 是符号链接，跳过删除以免误删链接目标", canonical)
                        return False
                    image_path.unlink(missing_ok=True)
                except OSError as exc:
                    logger.warning("清理孤儿图片文件 '%s' 失败: %s", canonical, exc)
                    return False
                return True

            deleted = await data_manager.cleanup_if_unreferenced(
                canonical, _unlink, additional_reference_check=_has_other_reference,
            )
            if deleted:
                logger.info("已清理无引用的孤儿图片文件: %s", canonical)

        if file_lock_held:
            await _cleanup_after_file_lock()
            return
        async with self.managed_file_lock(canonical):
            await _cleanup_after_file_lock()

    # ===== 投递 =====

    async def _send_error_text(self, text: str, stream_id: str, *, context: str) -> bool:
        """发送图片错误提示并吞掉发送异常，避免错误路径再次抛出。"""
        try:
            send_ok = await self._plugin.ctx.send.text(text, stream_id)
        except Exception as exc:
            logger.warning("%s发送异常: %s", context, exc, exc_info=True)
            return False
        if send_ok is False:
            logger.warning("%s发送失败：send.text 返回 False（可能被风控或连接异常）", context)
            self._plugin._service.note_send_failure(stream_id)
            return False
        return True

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
            p._service.note_send_failure(stream_id)
            await self._send_error_text(
                "❌ 图片发送失败，可能被风控或格式不受支持", stream_id,
                context="图片发送失败提示",
            )
