"""全局配置：从 .env 加载，启动校验，隐私提示。"""

import logging
import os
import re
import sys
from pathlib import Path
from typing import Iterator, Optional
from dotenv import load_dotenv

from core.logging import setup_logging

# 项目根目录
PROJECT_DIR = Path(__file__).resolve().parent
load_dotenv(PROJECT_DIR / ".env")

# 数据目录
EXES_DIR = PROJECT_DIR / "exes"

# LLM 配置（优先级：Keychain > LLM_API_KEY env > DEEPSEEK_API_KEY env）
_LLM_API_KEY = os.getenv("LLM_API_KEY", "") or os.getenv("DEEPSEEK_API_KEY", "")
try:
    from core.keychain import get_key

    _kc_llm = get_key("llm_api_key")
    if _kc_llm:
        _LLM_API_KEY = _kc_llm
except ImportError as e:
    # Keychain 不可用时回退到环境变量；此处早于日志初始化，只能用 root logger
    logging.getLogger("ex-memory").debug("Keychain 模块不可用，回退环境变量: %s", e)
LLM_API_KEY = _LLM_API_KEY
LLM_BASE_URL = os.getenv("LLM_BASE_URL", "https://api.deepseek.com")
LLM_MODEL = os.getenv("LLM_MODEL", "deepseek-chat")
LLM_TEMPERATURE = float(os.getenv("LLM_TEMPERATURE", "0.8"))
LLM_TOP_P = float(os.getenv("LLM_TOP_P", "0.9"))
LLM_FREQUENCY_PENALTY = float(os.getenv("LLM_FREQUENCY_PENALTY", "0.6"))
LLM_MAX_TOKENS = int(os.getenv("LLM_MAX_TOKENS", "4096"))
LLM_MAX_CONTEXT_CHARS = int(os.getenv("LLM_MAX_CONTEXT_CHARS", "50000"))

# Embedding 配置（优先级：Keychain > EMBEDDING_API_KEY env）
_EMBEDDING_API_KEY = os.getenv("EMBEDDING_API_KEY", "")
try:
    from core.keychain import get_key as _kc_get_key

    _kc_emb = _kc_get_key("embedding_api_key")
    if _kc_emb:
        _EMBEDDING_API_KEY = _kc_emb
except ImportError as e:
    logging.getLogger("ex-memory").debug("Keychain 模块不可用，回退环境变量: %s", e)
EMBEDDING_API_KEY = _EMBEDDING_API_KEY
EMBEDDING_BASE_URL = os.getenv("EMBEDDING_BASE_URL", "https://api.siliconflow.cn/v1")
EMBEDDING_MODEL = os.getenv("EMBEDDING_MODEL", "BAAI/bge-m3")

# 对话配置
ARCHIVE_THRESHOLD = 20
RECENT_SESSIONS = 3

# 对话留存天数：超过留存期的记录由 /cleanup 命令（配 cron）清理
CONVERSATION_RETENTION_DAYS = int(os.getenv("CONVERSATION_RETENTION_DAYS", "90"))

# 准入门槛。默认开启——实名与年龄确认是合规底线，不能靠默认值失守。
# 自托管/开发环境可显式关闭。
REQUIRE_PHONE_VERIFICATION = os.getenv(
    "REQUIRE_PHONE_VERIFICATION", "true"
).lower() in ("1", "true", "yes")
REQUIRE_AGE_CONFIRMATION = os.getenv("REQUIRE_AGE_CONFIRMATION", "true").lower() in (
    "1",
    "true",
    "yes",
)

# 共享状态后端（FR-035）。为空时用进程内实现——仅适用于单副本部署。
# 多副本必须配置，否则限流额度按副本数翻倍、用量统计对不上。
REDIS_URL = os.getenv("REDIS_URL", "")

# 单次请求的总 token 预算（FR-039 / D-08）。
# 原先 LLM_MAX_CONTEXT_CHARS 只约束 system prompt，history 上限是
# 100 轮 x 4000 字符 ≈ 26 万 tokens，远超模型上下文；超限必然报错，
# 而重试还会把这个必然失败放大三倍。
LLM_TOTAL_TOKEN_BUDGET = int(os.getenv("LLM_TOTAL_TOKEN_BUDGET", "48000"))

# 进程内缓存上限（FR-041 / D-06 / D-07）。原为无上限裸 dict，
# 每个引擎持有数万字符人格文本与向量库客户端，长期运行必然 OOM。
ENGINE_CACHE_SIZE = int(os.getenv("ENGINE_CACHE_SIZE", "64"))
ENGINE_CACHE_TTL_SECONDS = int(os.getenv("ENGINE_CACHE_TTL_SECONDS", "1800"))
SESSION_COUNTER_CACHE_SIZE = int(os.getenv("SESSION_COUNTER_CACHE_SIZE", "2048"))
SESSION_COUNTER_TTL_SECONDS = int(os.getenv("SESSION_COUNTER_TTL_SECONDS", "86400"))

# 账号注销时 safety_events 的处置方式："anonymize" 或 "delete"。
#
# 两条路都已实现，这是一个法务裁量点而非工程取舍，所以做成配置项：
# 法务无论怎么裁都不必改代码。默认 anonymize 的理由见
# docs/COMPLIANCE_DECISIONS.md 第 1 节。
SAFETY_EVENT_DELETION_MODE = os.getenv("SAFETY_EVENT_DELETION_MODE", "anonymize")
if SAFETY_EVENT_DELETION_MODE not in ("anonymize", "delete"):
    raise ValueError("SAFETY_EVENT_DELETION_MODE 只能是 anonymize 或 delete")

# 使用强度保护（FR-023）。默认 3 小时/日 + 1 小时冷静期。
# 数值是保守起点，正式阈值应由产品结合真实分布决定。
DAILY_USAGE_LIMIT_SECONDS = int(os.getenv("DAILY_USAGE_LIMIT_SECONDS", str(3 * 3600)))
USAGE_COOLDOWN_SECONDS = int(os.getenv("USAGE_COOLDOWN_SECONDS", str(3600)))
# 两次请求间隔小于此值时按连续使用计入时长，超过则按一次新交互计 30 秒
USAGE_GAP_THRESHOLD_SECONDS = int(os.getenv("USAGE_GAP_THRESHOLD_SECONDS", "300"))

# 协议版本。改版后旧同意自动失效，用户须重新勾选——
# 只记「同意过」而不记版本等于没有留痕。
THIRD_PARTY_DATA_POLICY_VERSION = os.getenv("THIRD_PARTY_DATA_POLICY_VERSION", "v1")

# RAG 配置
DEFAULT_TOP_K = 10
RAG_THRESHOLD = 0.3
# 3 轮/重叠 1 的 Recall@5=91.5%，优于 5 轮/重叠 1 的 71.1%（见 docs/eval_report.md 第 1 节）
CHUNK_TURNS = 3
CHUNK_OVERLAP = 1

# 日志
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")
LOG_DIR = PROJECT_DIR / "logs"

# 部署模式
SINGLE_USER_MODE = os.getenv("SINGLE_USER_MODE", "false").lower() in (
    "1",
    "true",
    "yes",
)
DISABLE_REGISTRATION = os.getenv("DISABLE_REGISTRATION", "false").lower() in (
    "1",
    "true",
    "yes",
)
TRUSTED_PROXY = os.getenv("TRUSTED_PROXY", "false").lower() in ("1", "true", "yes")
_trusted_ips_str = os.getenv("TRUSTED_PROXY_IPS", "")
TRUSTED_PROXY_IPS = (
    {ip.strip() for ip in _trusted_ips_str.split(",") if ip.strip()}
    if _trusted_ips_str
    else set()
)
METEOR_STORE_SSO_ENABLED = os.getenv("METEOR_STORE_SSO_ENABLED", "false").lower() in (
    "1",
    "true",
    "yes",
)
METEOR_STORE_PROXY_TOKEN = os.getenv("METEOR_STORE_PROXY_TOKEN", "")
_public_base_path = os.getenv("PUBLIC_BASE_PATH", "").strip("/")
if _public_base_path and not re.fullmatch(r"[A-Za-z0-9/_-]+", _public_base_path):
    raise ValueError("PUBLIC_BASE_PATH 只能包含字母、数字、/、_ 和 -")
PUBLIC_BASE_PATH = f"/{_public_base_path}" if _public_base_path else ""

# macOS 本地微信导出助手发布信息（安装包由站点自己的 HTTPS 地址提供）
LOCAL_WECHAT_HELPER_ENABLED = os.getenv(
    "LOCAL_WECHAT_HELPER_ENABLED", "false"
).lower() in ("1", "true", "yes")
LOCAL_WECHAT_HELPER_VERSION = os.getenv("LOCAL_WECHAT_HELPER_VERSION", "")
LOCAL_WECHAT_HELPER_MIN_API_VERSION = int(
    os.getenv("LOCAL_WECHAT_HELPER_MIN_API_VERSION", "1")
)
LOCAL_WECHAT_HELPER_ARM64_URL = os.getenv("LOCAL_WECHAT_HELPER_ARM64_URL", "")
LOCAL_WECHAT_HELPER_ARM64_SHA256 = os.getenv("LOCAL_WECHAT_HELPER_ARM64_SHA256", "")
LOCAL_WECHAT_HELPER_X64_URL = os.getenv("LOCAL_WECHAT_HELPER_X64_URL", "")
LOCAL_WECHAT_HELPER_X64_SHA256 = os.getenv("LOCAL_WECHAT_HELPER_X64_SHA256", "")

# 隐私确认标记
_privacy_confirmed = False


def init_app():
    """应用初始化：日志 + 配置校验。"""
    logger = setup_logging(LOG_DIR, LOG_LEVEL)
    logger.info("ex-memory 启动")

    errors = []
    if not LLM_API_KEY:
        errors.append("LLM_API_KEY 未配置（请在 .env 中设置或导出 DEEPSEEK_API_KEY）")

    if errors:
        for e in errors:
            logger.error(e)
        print("\n配置错误：")
        for e in errors:
            print(f"  ✗ {e}")
        sys.exit(1)

    logger.info("LLM: %s @ %s", LLM_MODEL, LLM_BASE_URL)
    if EMBEDDING_API_KEY:
        logger.info("Embedding: %s @ %s", EMBEDDING_MODEL, EMBEDDING_BASE_URL)
    else:
        logger.info("Embedding 未配置，RAG 检索不可用")

    return logger


def require_privacy_consent():
    """首次启动隐私提示，用户确认后方可继续。"""
    global _privacy_confirmed
    if _privacy_confirmed:
        return

    consent_file = PROJECT_DIR / ".privacy_consent"
    if consent_file.exists():
        _privacy_confirmed = True
        return

    print("""
╔══════════════════════════════════════════╗
║            ⚠️  隐私提醒                  ║
╠══════════════════════════════════════════╣
║ 你的聊天记录、性格分析等数据将发送至：    ║
║                                          ║
║  • DeepSeek API (LLM 对话生成)           ║
║  • 硅基流动 API (Embedding 向量化)       ║
║                                          ║
║ 这些数据将经过第三方服务器处理。          ║
║ 所有数据仅存储在本地，不上传至项目方。    ║
║                                          ║
║ 输入 'yes' 确认继续:                     ║
╚══════════════════════════════════════════╝
""")

    try:
        ans = input("> ").strip().lower()
        if ans == "yes":
            consent_file.write_text("confirmed", encoding="utf-8")
            _privacy_confirmed = True
        else:
            print("已取消。")
            sys.exit(0)
    except (EOFError, KeyboardInterrupt):
        print("\n已取消。")
        sys.exit(0)


_llm_client = None


def get_llm_client():
    """获取共享的 OpenAI 兼容客户端（懒初始化 + 超时配置）。"""
    global _llm_client
    if _llm_client is None:
        from openai import OpenAI

        _llm_client = OpenAI(
            api_key=LLM_API_KEY,
            base_url=LLM_BASE_URL,
            timeout=60.0,
        )
    return _llm_client


def get_llm_config() -> dict:
    return {
        "api_key": LLM_API_KEY,
        "base_url": LLM_BASE_URL,
        "model": LLM_MODEL,
        "temperature": LLM_TEMPERATURE,
        "top_p": LLM_TOP_P,
        "frequency_penalty": LLM_FREQUENCY_PENALTY,
        "max_tokens": LLM_MAX_TOKENS,
    }


def get_embedding_config() -> dict:
    return {
        "api_key": EMBEDDING_API_KEY,
        "base_url": EMBEDDING_BASE_URL,
        "model": EMBEDDING_MODEL,
    }


def get_ex_dir(slug: str) -> Path:
    # 仅用于不含 owner 上下文的存量子流程（CLI、后台清理、测试）。
    return EXES_DIR / slug


def get_ex_dir_owned(slug: str, owner: int) -> Path:
    """按账号隔离的镜像目录：exes/<owner>/<slug>。

    多用户模式下镜像按 owner 归位，避免跨用户 slug 冲突，
    也不再用一个全局 slug 泄露其他用户的镜像名存在性。
    """
    return EXES_DIR / str(owner) / slug


def resolve_ex_dir(slug: str, owner: Optional[int] = None) -> Path:
    """解析便于访问的镜像目录：优先嵌套（新的按账号布局），回退扁平（存量布局）。

    owner 为空时无法定位嵌套目录，退化为扁平目录（存量镜像 / 无人格的 CLI、后台任务）。
    安全性始终由 meta.json 里的 owner_user_id 校验把关，目录布局只是命名空间，不是唯一防线。
    """
    if owner is not None:
        owned = get_ex_dir_owned(slug, owner)
        if owned.exists():
            return owned
    flat = get_ex_dir(slug)
    if flat.exists():
        return flat
    return get_ex_dir_owned(slug, owner) if owner is not None else flat


def ensure_ex_dirs_owned(slug: str, owner: Optional[int]) -> Path:
    """在按账号隔离的目录下创建镜像骨架：exes/<owner>/<slug>。

    owner 为空（单用户 CLI / Gradio 无账号上下文）时退化为扁平目录，
    避免误建 exes/None/<slug> 这种伪命名空间。
    """
    if owner is None:
        return ensure_ex_dirs(slug)
    ex_dir = get_ex_dir_owned(slug, owner)
    for sub in ["chroma_db", "sessions", "versions"]:
        (ex_dir / sub).mkdir(parents=True, exist_ok=True)
    return ex_dir


def iter_exe_dirs(
    require_meta: bool = True,
) -> Iterator[tuple[str, Optional[int], Path]]:
    """遍历全部镜像，兼容扁平（exes/<slug>）与嵌套（exes/<owner>/<slug>）两种布局。

    平台级任务（过期清理、盘点）必须走这个入口：直接 EXES_DIR.iterdir()
    在嵌套布局下会把 owner 目录当成镜像本身，从而漏掉其下所有真实镜像。

    Args:
        require_meta: 是否只认带 meta.json 的目录。展示类场景用默认值；
            留存清理这类合规任务应传 False —— meta.json 损坏或缺失的残缺
            镜像同样要被清理，漏删比多删风险更大。

    Yields:
        (slug, owner, path)，扁平布局的 owner 为 None
    """
    if not EXES_DIR.exists():
        return
    for top in sorted(EXES_DIR.iterdir()):
        if not top.is_dir():
            continue
        # 扁平镜像优先按 meta.json 判定，这样名字恰好是数字的存量镜像（如 exes/1）
        # 不会被误当成 owner 命名空间
        if (top / "meta.json").exists():
            yield top.name, None, top
            continue
        # owner 目录名恒为账号 ID（str(owner)）
        if top.name.isdigit():
            for sub in sorted(top.iterdir()):
                if sub.is_dir() and (not require_meta or (sub / "meta.json").exists()):
                    yield sub.name, int(top.name), sub
            continue
        if not require_meta:
            yield top.name, None, top


def find_ex_dir(slug: str) -> Optional[Path]:
    """无 owner 上下文时定位镜像目录（CLI / Gradio 用）。

    扁平布局优先。嵌套布局下若多个账号有同名镜像，抛错而不是随便选一个——
    CLI 没有身份上下文，猜错等于操作了别人的镜像。

    Raises:
        ValueError: 多个账号存在同名镜像，无法在无身份上下文下消歧
    """
    flat = get_ex_dir(slug)
    if flat.exists():
        return flat
    matches = [path for s, _owner, path in iter_exe_dirs() if s == slug]
    if len(matches) > 1:
        raise ValueError(f"镜像 [{slug}] 在多个账号下存在，请在 Web 端操作")
    return matches[0] if matches else None


def find_ex_dir_with_owner(slug: str) -> tuple[Optional[Path], Optional[int]]:
    """定位镜像目录并给出其所属账号（CLI / Gradio 这类无身份上下文的入口用）。

    owner 由目录布局推出：嵌套布局的父目录名即账号 ID，扁平布局为 None。
    调用方拿到 owner 后要继续传给 pipeline / factory，否则它们会退回扁平目录。

    Returns:
        (目录, owner)；镜像不存在时为 (None, None)
    """
    path = find_ex_dir(slug)
    if path is None:
        return None, None
    parent = path.parent
    if parent != EXES_DIR and parent.name.isdigit():
        return path, int(parent.name)
    return path, None


def get_collection_name(slug: str) -> str:
    safe = slug.encode("utf-8").hex()
    # ChromaDB collection name 限制 63 字符，截断 hex 部分
    prefix = "ex_"
    suffix = "_memories"
    max_hex = 63 - len(prefix) - len(suffix)
    if len(safe) > max_hex:
        safe = safe[:max_hex]
    return f"{prefix}{safe}{suffix}"


def ensure_ex_dirs(slug: str) -> Path:
    ex_dir = EXES_DIR / slug
    for sub in ["chroma_db", "sessions", "versions"]:
        (ex_dir / sub).mkdir(parents=True, exist_ok=True)
    return ex_dir
