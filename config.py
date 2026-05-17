"""全局配置：从 .env 加载，启动校验，隐私提示。"""

import os
import re
import sys
from pathlib import Path
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
except Exception:
    pass
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
except Exception:
    pass
EMBEDDING_API_KEY = _EMBEDDING_API_KEY
EMBEDDING_BASE_URL = os.getenv("EMBEDDING_BASE_URL", "https://api.siliconflow.cn/v1")
EMBEDDING_MODEL = os.getenv("EMBEDDING_MODEL", "BAAI/bge-m3")

# 对话配置
ARCHIVE_THRESHOLD = 20
RECENT_SESSIONS = 3

# RAG 配置
DEFAULT_TOP_K = 10
RAG_THRESHOLD = 0.3
CHUNK_TURNS = 5
CHUNK_OVERLAP = 1

# 日志
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")
LOG_DIR = PROJECT_DIR / "logs"

# 部署模式
SINGLE_USER_MODE = os.getenv("SINGLE_USER_MODE", "false").lower() in ("1", "true", "yes")
DISABLE_REGISTRATION = os.getenv("DISABLE_REGISTRATION", "false").lower() in ("1", "true", "yes")
TRUSTED_PROXY = os.getenv("TRUSTED_PROXY", "false").lower() in ("1", "true", "yes")

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
    return EXES_DIR / slug


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
