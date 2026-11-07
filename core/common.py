"""自定义命令插件的通用常量、纯函数与 manifest 版本读取。

跨模块共享的无状态知识：命令关键字、各类上限默认值、图片相关常量、
保留词判断、作用域 ID 解析与三态文案。不依赖 SDK ctx，是 core 子包的最底层
模块——其余模块按需 import 本模块，本模块不反向依赖任何 core 模块。
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


# --- 版本 ---


def _load_manifest_version() -> str:
    """从 _manifest.json 读取版本号，保持插件元数据单一来源。

    common.py 位于 core/ 子目录，_manifest.json 在插件根目录，故须 ``parent.parent``。
    """
    try:
        manifest_path = Path(__file__).resolve().parent.parent / "_manifest.json"
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


# --- 上限默认值 ---

DEFAULT_MAX_TRIGGER_LENGTH = 50           # 触发词默认最大长度
DEFAULT_MAX_RESPONSE_LENGTH = 2000        # 回复内容默认最大长度
DEFAULT_MAX_COMMANDS_PER_SCOPE = 500      # 每个作用域默认最大命令数
DEFAULT_MAX_IMAGE_SIZE = 10 * 1024 * 1024  # 图片文件默认最大 10MB


# --- 图片相关 ---

IMAGE_EXTENSIONS = (".png", ".jpg", ".jpeg", ".gif", ".webp")

# 适配器图片/表情下载失败时的降级文本占位符——见 napcat 适配器
# codecs/inbound/message_codec.py::_build_image_like_segment：图片 url 下载失败时，
# image/emoji 段会被替换成这两个文本段。带图添加时若发生下载失败，消息里就不再有
# 携带 binary_data_base64 的图片段，会绕过带图添加逻辑、落到纯文本添加路径，
# 把占位符本身当作回复内容存成「幽灵图片命令」。添加校验需显式拦截。
ADAPTER_IMAGE_FALLBACK_TEXTS = ("[image]", "[emoji]")


def looks_like_image_response(response: str) -> bool:
    """裸文件名形式的图片回复检测——避免把"详情见 chart.png"这类含空格句子，
    或 ``https://x/a.png`` 这类图片直链误判为本地图片协议。

    README 约定的图片语法是 ``.问：xx答：hello.png``——即整个 response 就是一个文件名，
    不带空格/换行、也不含 URL scheme。任何含空白字符或 ``://`` 的 response 都视为纯文本，
    即便以 ``.png`` 结尾：含空白的多半是句子，含 ``://`` 的几乎必然是图片直链，
    二者都应原样作为文本回复发出，而不是去 image_directory 里找一个不存在的"文件"。
    """
    if not response or "://" in response:
        return False
    # 先判后缀（C 级 O(1)，绝大多数文本回复不以图片后缀结尾即可在此短路），再扫空白字符
    return response.lower().endswith(IMAGE_EXTENSIONS) and not any(c.isspace() for c in response)


def is_persistable_text(value: str) -> bool:
    """字符串能否按 UTF-8 落盘。

    JSON 里的 ``\\udXXX`` 转义能被 ``json.load`` 还原成孤立代理项，这类字符串在
    ``json.dump(ensure_ascii=False)`` 写盘时会抛 ``UnicodeEncodeError``；添加与加载时
    都用本函数拦截，避免内存已改、磁盘写不进去。
    """
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        return False
    return True


# --- 命令关键字 ---

# Command pattern 中前缀占位符——装饰器声明阶段无法访问 self.config，
# 这里先用通配占位，get_components() 阶段再用 re.escape(配置前缀) 重写为精确匹配。
PREFIX_PLACEHOLDER = r"[^\w\s]"

# 内置命令关键字——@Command 的正则 pattern 与动态触发"保留词"判断的单一来源。
# pattern 用这些常量拼接，is_reserved_trigger 也由它们派生：改一处即同步两处，
# 避免新增/调整内置命令时 pattern 与保留词列表漂移（漂移会让同名动态 trigger 与
# 内置命令互相抢占，或在数据里产生永不触发的幽灵命令）。
# 这些关键字会被拼进 @Command 的正则 pattern 与带图添加的 re.match，但拼接处统一用
# re.escape() 包裹（见 plugin.py 各 pattern 与 dispatcher.py 的 re.match），故即便值中含
# 正则元字符也安全；调整关键字时无需再担心正则转义。
KW_ADD = "问："
KW_ADD_ANSWER = "答："  # add 命令中 trigger 与 response 的分隔符
KW_DELETE = "删："
KW_DELETE_GLOBAL = "删全局："
KW_LIST = "列表"


# --- 内置命令 pattern（占位符形态）---

# 四个内置命令的正则单一来源：@Command 装饰器声明与「纯文本段重新匹配」共用。
# response 段用 [\s\S]+ 而非 .+：Host 编译命令正则是 re.compile(pattern) 且不带 re.DOTALL，
# .+ 不跨行会让「答：」后含换行的多行回复整体失配；trigger 段仍用 .+? 保持单行。
BUILTIN_PATTERN_ADD = (
    rf"^{PREFIX_PLACEHOLDER}{re.escape(KW_ADD)}(?P<trigger>.+?)"
    rf"{re.escape(KW_ADD_ANSWER)}(?P<response>[\s\S]+)$"
)
BUILTIN_PATTERN_DELETE = rf"^{PREFIX_PLACEHOLDER}{re.escape(KW_DELETE)}(?P<trigger>.+)$"
BUILTIN_PATTERN_DELETE_GLOBAL = rf"^{PREFIX_PLACEHOLDER}{re.escape(KW_DELETE_GLOBAL)}(?P<trigger>.+)$"
BUILTIN_PATTERN_LIST = rf"^{PREFIX_PLACEHOLDER}{re.escape(KW_LIST)}$"

# 四个内置命令的组件名：@Command 声明、组件启停查询与 Host 命令 ID（``<plugin_id>.<组件名>``）共用。
COMMAND_NAME_ADD = "custom_command_add"
COMMAND_NAME_DELETE = "custom_command_delete"
COMMAND_NAME_DELETE_GLOBAL = "custom_command_delete_global"
COMMAND_NAME_LIST = "custom_command_list"
# 声明为 Host 受保护命令（permission="operator"）的组件，配置页据此统计按命令放行的人数。
PROTECTED_COMMAND_NAMES = (COMMAND_NAME_ADD, COMMAND_NAME_DELETE, COMMAND_NAME_DELETE_GLOBAL)

# kind → 占位符 pattern。顺序即「纯文本重新匹配」的尝试顺序：delete_global 须先于 delete，
# 虽然「删全局：」与「删：」并不互为前缀，但显式排序可防未来关键字调整后产生歧义。
BUILTIN_PATTERNS: Dict[str, str] = {
    "add": BUILTIN_PATTERN_ADD,
    "delete_global": BUILTIN_PATTERN_DELETE_GLOBAL,
    "delete": BUILTIN_PATTERN_DELETE,
    "list": BUILTIN_PATTERN_LIST,
}


def compile_builtin_patterns(prefix: str) -> Dict[str, "re.Pattern[str]"]:
    """把占位符 pattern 按实际前缀编译成正则，供纯文本段重新匹配使用。"""
    escaped = re.escape(prefix)
    return {
        kind: re.compile(pattern.replace(PREFIX_PLACEHOLDER, escaped, 1))
        for kind, pattern in BUILTIN_PATTERNS.items()
    }


# --- 入站消息段解析 ---


def extract_text_and_images(raw_message: Any) -> Tuple[str, List[Dict[str, Any]]]:
    """从 raw_message 段列表中拼接纯文本、收集图片段。

    不依赖 processed_plain_text——后者会把引用消息原文、@昵称、图片占位符一并渲染进去
    并用空格拼接（Host message.py::process），拿它做命令匹配会让被引用消息的内容参与匹配。
    这里只认 text 段原文与 image/emoji 段。
    """
    text_parts: List[str] = []
    image_segs: List[Dict[str, Any]] = []
    if isinstance(raw_message, list):
        for seg in raw_message:
            if not isinstance(seg, dict):
                continue
            seg_type = seg.get("type")
            if seg_type == "text":
                data = seg.get("data")
                if isinstance(data, str):
                    text_parts.append(data)
                elif isinstance(data, dict):
                    text = data.get("text")
                    if isinstance(text, str):
                        text_parts.append(text)
            elif seg_type in ("image", "emoji"):
                image_segs.append(seg)
    return "".join(text_parts), image_segs


# --- 消息路由上下文 ---

# 旧格式路由身份的候选键，与 Host platform_io/route_key_factory.py::RouteKeyFactory 保持一致。
_ACCOUNT_ID_KEYS = ("platform_io_account_id", "account_id", "self_id", "bot_account")
_SCOPE_KEYS = ("platform_io_scope", "route_scope", "adapter_scope", "connection_id")


@dataclass(frozen=True)
class MessageRoute:
    """一条入站消息的路由上下文：发往哪个会话、经由哪个账号/连接。"""

    stream_id: str = ""
    platform: str = "qq"
    group_id: str = ""
    user_id: str = ""
    account_id: str = ""
    scope: str = ""

    @property
    def chat_type(self) -> str:
        return "group" if self.group_id else "private"


def _pick_string(mapping: Dict[str, Any], keys: Tuple[str, ...]) -> str:
    for key in keys:
        value = mapping.get(key)
        if value is None:
            continue
        text = str(value).strip()
        if text:
            return text
    return ""


def extract_route_identity(message: Dict[str, Any]) -> Tuple[str, str]:
    """取入站消息的路由身份 (account_id, scope)，口径与 Host ``RouteKeyFactory`` 一致。

    消息顶层带 ``account_id`` 键即为正式格式（SnowLuma 等适配器把 bot 账号与连接 ID 写在顶层），
    此时只认顶层的 account_id/scope，值为 None 时视为空；否则按旧格式对 account 与 scope 各自
    依次从消息顶层、message_info、additional_config 中按候选键查找。只读 additional_config 会让
    顶层写法的适配器拿到空身份。
    """
    if "account_id" in message:
        return str(message.get("account_id") or "").strip(), str(message.get("scope") or "").strip()
    msg_info = message.get("message_info") if isinstance(message.get("message_info"), dict) else {}
    additional = msg_info.get("additional_config") if isinstance(msg_info.get("additional_config"), dict) else {}
    sources = (message, msg_info, additional)
    account_id = next((value for value in (_pick_string(s, _ACCOUNT_ID_KEYS) for s in sources) if value), "")
    scope = next((value for value in (_pick_string(s, _SCOPE_KEYS) for s in sources) if value), "")
    return account_id, scope


def build_message_route(
    message: Optional[Dict[str, Any]],
    *,
    stream_id: str = "",
    group_id: str = "",
    user_id: str = "",
) -> MessageRoute:
    """从 Host 下发的 message dict 构造路由上下文；显式入参优先于 message 内字段。"""
    msg = message if isinstance(message, dict) else {}
    msg_info = msg.get("message_info") or {}
    if not isinstance(msg_info, dict):
        msg_info = {}
    user_info = msg_info.get("user_info") or {}
    group_info = msg_info.get("group_info") or {}
    account_id, scope = extract_route_identity(msg)
    return MessageRoute(
        stream_id=str(stream_id or msg.get("session_id") or ""),
        platform=str(msg.get("platform") or "qq"),
        group_id=str(group_id or (group_info.get("group_id") if isinstance(group_info, dict) else "") or ""),
        user_id=str(user_id or (user_info.get("user_id") if isinstance(user_info, dict) else "") or ""),
        account_id=account_id,
        scope=scope,
    )


# --- 保留词判断 ---

# 内置命令保留词——以这些片段开头或完全相等的 trigger 留给精确 pattern 的 @Command
# 处理（否则用户 add 名为"列表"/"问：x答：y"的 trigger 会与内置命令冲突）。
# 由命令关键字常量派生，与 @Command 的 pattern 共享单一来源。
_RESERVED_TRIGGER_PREFIXES = (KW_ADD, KW_DELETE, KW_DELETE_GLOBAL)
_RESERVED_TRIGGER_EXACT = (KW_LIST,)


def is_reserved_trigger(trigger: str) -> bool:
    """判断 trigger 是否与内置命令关键字冲突（即"保留词"）。

    命中保留词的 trigger 即便写入数据也是"幽灵数据"：动态分发 hook 见到
    ``<prefix><reserved>`` 会 return None 让位给精确 @Command，永不触发。
    添加前校验、加载时清洗、动态分发放行三处共用本判断，避免逻辑漂移。
    """
    return trigger in _RESERVED_TRIGGER_EXACT or any(
        trigger.startswith(prefix) for prefix in _RESERVED_TRIGGER_PREFIXES
    )


# --- 作用域辅助（无状态文案 / ID 解析）---


def resolve_scope_id(group_id: str, user_id: str) -> str:
    """获取当前上下文的作用域 ID：群聊用 group_id，私聊用 user_id。

    与 ``ScopeResolver.resolve`` 的调用契约固定为两行——先 ``resolve_scope_id`` 取
    原始 ID，再 ``ScopeResolver.resolve`` 映射到数据分区名。
    """
    return group_id if group_id else user_id


def build_scope_desc(scope_id: str, scope_used: str) -> str:
    """构造"添加成功"提示里的作用域说明后缀。

    与 build_list_header_text 的三态判断同源：全局共享 / 映射分组 / ID 独享。
    文本添加与带图添加共用，避免两处文案各自漂移。
    """
    if scope_used == "global":
        return "（全局共享）"
    if scope_id != scope_used:
        return f"（映射分组: {scope_used}）"
    return f"（ID: {scope_used} 独享）"


def build_list_header_text(scope_id: str, current_scope: str) -> str:
    """构造列表头部文本。三态：全局共享 / 自定义映射分组 / 独立隔离。"""
    header_text = f"📋 自定义命令列表\n当前ID: {scope_id}\n对应作用域: {current_scope}"
    if current_scope == "global":
        header_text += "\n(全局共享模式)"
    elif scope_id != current_scope:
        header_text += "\n(自定义映射分组模式)"
    else:
        header_text += "\n(独立隔离模式)"
    return header_text
