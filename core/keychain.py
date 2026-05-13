"""macOS Keychain 密钥管理：安全存储 API Key，不回退到明文 .env 泄露。"""

import subprocess
import logging
import sys
from typing import Optional

logger = logging.getLogger("ex-memory")

SERVICE_NAME = "ex-memory"
KEY_ACCOUNTS = {
    "LLM_API_KEY": "llm_api_key",
    "EMBEDDING_API_KEY": "embedding_api_key",
}


def _run_security(args: list[str], timeout: int = 5) -> subprocess.CompletedProcess:
    """调用 macOS security 命令。"""
    return subprocess.run(
        ["security"] + args,
        capture_output=True, text=True, timeout=timeout,
    )


def get_key(account: str) -> Optional[str]:
    """从 Keychain 获取密钥。"""
    try:
        result = _run_security([
            "find-generic-password", "-s", SERVICE_NAME, "-a", account, "-w",
        ])
        if result.returncode == 0 and result.stdout.strip():
            logger.info("从 Keychain 读取密钥: %s", account)
            return result.stdout.strip()
    except Exception:
        pass
    return None


def set_key(account: str, password: str) -> bool:
    """将密钥写入 Keychain。如已有同名项则更新。"""
    try:
        existing = get_key(account)
        if existing:
            _run_security([
                "delete-generic-password", "-s", SERVICE_NAME, "-a", account,
            ])
        _run_security([
            "add-generic-password", "-s", SERVICE_NAME, "-a", account,
            "-w", password, "-U",
        ])
        logger.info("密钥已写入 Keychain: %s", account)
        return True
    except Exception as e:
        logger.error("写入 Keychain 失败 (%s): %s", account, e)
        return False


def delete_key(account: str) -> bool:
    """从 Keychain 删除密钥。"""
    try:
        _run_security([
            "delete-generic-password", "-s", SERVICE_NAME, "-a", account,
        ])
        logger.info("密钥已从 Keychain 删除: %s", account)
        return True
    except Exception:
        return False


def load_keys_from_keychain() -> dict[str, str]:
    """从 Keychain 加载所有已知密钥，返回 {ENV_KEY: value} 映射。"""
    keys = {}
    for env_key, account in KEY_ACCOUNTS.items():
        value = get_key(account)
        if value:
            keys[env_key] = value
    return keys


def cmd_keychain(args: str = ""):
    """/keychain CLI 指令处理。"""
    parts = args.strip().split()
    action = parts[0].lower() if parts else ""

    if action == "set":
        if len(parts) < 3:
            print("用法: /keychain set {llm|embedding} {key}")
            return
        target = parts[1].lower()
        key_value = parts[2]
        if target == "llm":
            set_key("llm_api_key", key_value)
            print("LLM API Key 已保存到 Keychain。重启后生效。")
        elif target == "embedding":
            set_key("embedding_api_key", key_value)
            print("Embedding API Key 已保存到 Keychain。重启后生效。")
        else:
            print(f"未知目标: {target}。可选: llm / embedding")
    elif action == "delete":
        if len(parts) < 2:
            print("用法: /keychain delete {llm|embedding}")
            return
        target = parts[1].lower()
        account = {"llm": "llm_api_key", "embedding": "embedding_api_key"}.get(target)
        if account:
            delete_key(account)
            print(f"已从 Keychain 删除 {target} 密钥。")
    elif action == "status":
        llm_key = get_key("llm_api_key")
        emb_key = get_key("embedding_api_key")
        print(f"LLM API Key:    {'已配置' if llm_key else '未配置'}")
        print(f"Embedding Key:  {'已配置' if emb_key else '未配置'}")
    else:
        print("""
[Keychain 密钥管理]
  /keychain status              查看密钥配置状态
  /keychain set llm {key}       设置 LLM API Key
  /keychain set embedding {key} 设置 Embedding API Key
  /keychain delete {llm|embedding}  删除密钥
""")
