"""全局配置：从 .env 加载，提供统一的配置访问接口。"""

import os
import re
from pathlib import Path
from dotenv import load_dotenv

# 项目根目录
PROJECT_DIR = Path(__file__).resolve().parent
load_dotenv(PROJECT_DIR / ".env")

# 数据目录
EXES_DIR = PROJECT_DIR / "exes"

# LLM 配置（优先 LLM_API_KEY，回退 DEEPSEEK_API_KEY）
LLM_API_KEY = os.getenv("LLM_API_KEY", "") or os.getenv("DEEPSEEK_API_KEY", "")
LLM_BASE_URL = os.getenv("LLM_BASE_URL", "https://api.siliconflow.cn/v1")
LLM_MODEL = os.getenv("LLM_MODEL", "deepseek-ai/DeepSeek-V3")
LLM_TEMPERATURE = float(os.getenv("LLM_TEMPERATURE", "0.8"))
LLM_TOP_P = float(os.getenv("LLM_TOP_P", "0.9"))
LLM_FREQUENCY_PENALTY = float(os.getenv("LLM_FREQUENCY_PENALTY", "0.6"))

# Embedding 配置
EMBEDDING_API_KEY = os.getenv("EMBEDDING_API_KEY", LLM_API_KEY)
EMBEDDING_BASE_URL = os.getenv("EMBEDDING_BASE_URL", LLM_BASE_URL)
EMBEDDING_MODEL = os.getenv("EMBEDDING_MODEL", "BAAI/bge-m3")

# 对话配置
DEFAULT_TALK_LENGTH = 20  # 默认对话轮数
ARCHIVE_THRESHOLD = 20    # 触发归档询问的轮次数
RECENT_SESSIONS = 3       # 启动时加载的最近 session 数量

# RAG 配置
DEFAULT_TOP_K = 10        # 默认检索条数
CHUNK_TURNS = 5           # 每个 chunk 包含的对话轮次
CHUNK_OVERLAP = 1         # chunk 之间的重叠轮次


def get_llm_config() -> dict:
    """返回 LLM 调用所需的配置字典。"""
    return {
        "api_key": LLM_API_KEY,
        "base_url": LLM_BASE_URL,
        "model": LLM_MODEL,
        "temperature": LLM_TEMPERATURE,
        "top_p": LLM_TOP_P,
        "frequency_penalty": LLM_FREQUENCY_PENALTY,
    }


def get_embedding_config() -> dict:
    """返回 Embedding 调用所需的配置字典。"""
    return {
        "api_key": EMBEDDING_API_KEY,
        "base_url": EMBEDDING_BASE_URL,
        "model": EMBEDDING_MODEL,
    }


def get_ex_dir(slug: str) -> Path:
    """返回指定前任的数据目录。"""
    return EXES_DIR / slug


def get_collection_name(slug: str) -> str:
    """生成 ChromaDB 安全的集合名（仅允许 a-zA-Z0-9._-）。"""
    # 将非 ASCII 字符替换为其 UTF-8 编码的十六进制
    safe = slug.encode('utf-8').hex()
    return f"ex_{safe}_memories"


def ensure_ex_dirs(slug: str) -> Path:
    """创建前任数据目录结构，返回根路径。"""
    ex_dir = EXES_DIR / slug
    for sub in ["chroma_db", "sessions", "versions"]:
        (ex_dir / sub).mkdir(parents=True, exist_ok=True)
    return ex_dir
