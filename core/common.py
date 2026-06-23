"""自定义命令插件的通用常量、纯函数与 manifest 版本读取。

跨模块共享的无状态知识：命令关键字、各类上限默认值、图片相关常量、
保留词判断、作用域 ID 解析与三态文案。不依赖 SDK ctx，是 core 子包的最底层
模块——其余模块按需 import 本模块，本模块不反向依赖任何 core 模块。
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

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
