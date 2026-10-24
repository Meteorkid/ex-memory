"""账号级模型配置；数据库和公开响应都不持有明文凭据。"""

import base64
import binascii
import os
import re

from server.auth import _get_conn


PROVIDERS = {
    "deepseek": "https://api.deepseek.com",
    "siliconflow": "https://api.siliconflow.cn/v1",
    "openai": "https://api.openai.com/v1",
}
ALLOWED_PROVIDERS = {
    "llm": frozenset({*PROVIDERS, "custom"}),
    "embedding": frozenset({"siliconflow", "openai", "custom"}),
}
_MODEL_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}\Z")
_LOCK_REASON = "已有向量索引使用此模型；可更新 Key，模型变更需要重建索引。"


class ModelSettingsError(Exception):
    """配置错误只携带可公开的信息，不拼接输入或底层加密异常。"""


class MissingModelSettings(ModelSettingsError):
    pass


MissingModelKey = MissingModelSettings


class SettingsUnavailable(ModelSettingsError):
    pass


class SettingsConflict(ModelSettingsError):
    pass


class EmbeddingModelLocked(ModelSettingsError):
    pass


class SettingsValidationError(ModelSettingsError):
    pass


def _check_user_id(user_id: int) -> None:
    if type(user_id) is not int or user_id <= 0:
        raise SettingsValidationError("账号身份无效")


def _default_settings() -> dict:
    return {
        "revision": 0,
        "llm_provider": "deepseek",
        "llm_base_url": PROVIDERS["deepseek"],
        "llm_model": "deepseek-chat",
        "llm_key_ciphertext": None,
        "llm_key_last4": None,
        "embedding_provider": "siliconflow",
        "embedding_base_url": PROVIDERS["siliconflow"],
        "embedding_model": "BAAI/bge-m3",
        "embedding_key_ciphertext": None,
        "embedding_key_last4": None,
        "embedding_locked": 0,
        "updated_at": None,
    }


def _read_settings(conn, user_id: int) -> dict:
    row = conn.execute(
        "SELECT * FROM user_model_settings WHERE user_id = ?", (user_id,)
    ).fetchone()
    return dict(row) if row else _default_settings()


def _cipher():
    import config

    try:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM

        encoded = getattr(config, "MODEL_SETTINGS_ENCRYPTION_KEY", "")
        key = base64.b64decode(encoded, altchars=b"-_", validate=True)
        if len(key) != 32:
            raise ValueError("invalid key length")
        return AESGCM(key)
    except (ImportError, ValueError, TypeError, binascii.Error):
        raise SettingsUnavailable("模型配置安全存储暂不可用，请联系管理员") from None


def _aad(user_id: int, kind: str, provider: str, base_url: str) -> bytes:
    return f"ex-memory:model-key:v1:{user_id}:{kind}:{provider}:{base_url}".encode(
        "utf-8"
    )


def _encrypt(
    api_key: str, user_id: int, kind: str, provider: str, base_url: str
) -> str:
    nonce = os.urandom(12)
    encrypted = _cipher().encrypt(
        nonce, api_key.encode("utf-8"), _aad(user_id, kind, provider, base_url)
    )
    return "v1." + base64.urlsafe_b64encode(nonce + encrypted).decode("ascii")


def _decrypt(token: str, user_id: int, kind: str, provider: str, base_url: str) -> str:
    cipher = _cipher()
    from cryptography.exceptions import InvalidTag

    try:
        if not token.startswith("v1."):
            raise ValueError("unknown envelope")
        payload = base64.b64decode(token[3:], altchars=b"-_", validate=True)
        if len(payload) < 29:
            raise ValueError("truncated envelope")
        return cipher.decrypt(
            payload[:12], payload[12:], _aad(user_id, kind, provider, base_url)
        ).decode("utf-8")
    except (InvalidTag, ValueError, TypeError, UnicodeError, binascii.Error):
        raise SettingsUnavailable("保存的模型 Key 无法读取，请重新保存 Key") from None


def _public_settings(row: dict) -> dict:
    try:
        _cipher()
        storage_available = True
    except SettingsUnavailable:
        storage_available = False
    result = {
        "revision": row["revision"],
        "updated_at": row["updated_at"],
        "storage_available": storage_available,
    }
    for kind in ALLOWED_PROVIDERS:
        has_key = bool(row[f"{kind}_key_ciphertext"])
        result[kind] = {
            "provider": row[f"{kind}_provider"],
            "base_url": row[f"{kind}_base_url"],
            "model": row[f"{kind}_model"],
            "has_key": has_key,
            "key_last4": row[f"{kind}_key_last4"] if has_key else None,
        }
    result["embedding"].update(
        locked=bool(row["embedding_locked"]),
        locked_reason=_LOCK_REASON if row["embedding_locked"] else None,
    )
    return result


def get_public_settings(user_id: int) -> dict:
    _check_user_id(user_id)
    with _get_conn() as conn:
        return _public_settings(_read_settings(conn, user_id))


def get_settings_revision(user_id: int) -> int:
    _check_user_id(user_id)
    with _get_conn() as conn:
        row = conn.execute(
            "SELECT revision FROM user_model_settings WHERE user_id = ?", (user_id,)
        ).fetchone()
    return int(row["revision"]) if row else 0


def get_model_config(user_id: int, kind: str) -> dict:
    """仅在实际调用模型时解密；有账号身份的调用永不回退全局 Key。"""
    _check_user_id(user_id)
    if kind not in ALLOWED_PROVIDERS:
        raise SettingsValidationError("未知模型配置类型")
    with _get_conn() as conn:
        row = _read_settings(conn, user_id)
    provider = row[f"{kind}_provider"]
    if provider not in ALLOWED_PROVIDERS[kind]:
        raise SettingsUnavailable("保存的模型服务商无效，请重新配置")
    token = row[f"{kind}_key_ciphertext"]
    if not token:
        name = "LLM" if kind == "llm" else "Embedding"
        raise MissingModelSettings(f"请先在模型设置中保存自己的 {name} Key")
    result = {
        "api_key": _decrypt(token, user_id, kind, provider, row[f"{kind}_base_url"]),
        "base_url": row[f"{kind}_base_url"],
        "provider": provider,
        "model": row[f"{kind}_model"],
        "revision": row["revision"],
    }
    if kind == "embedding":
        result["locked"] = bool(row["embedding_locked"])
    return result


def _apply_update(row: dict, user_id: int, kind: str, update: dict) -> None:
    from core.provider_transport import normalize_base_url

    if set(update) - {"provider", "base_url", "model", "api_key", "delete_key"}:
        raise SettingsValidationError("模型配置包含未知字段")
    old_provider = row[f"{kind}_provider"]
    old_base_url = row[f"{kind}_base_url"]
    provider = update.get("provider", old_provider)
    model = update.get("model", row[f"{kind}_model"])
    if not isinstance(provider, str) or provider not in ALLOWED_PROVIDERS[kind]:
        raise SettingsValidationError("不支持此模型服务商")
    base_url = update.get(
        "base_url",
        old_base_url if provider == old_provider else PROVIDERS.get(provider),
    )
    try:
        base_url = normalize_base_url(base_url)
    except (TypeError, ValueError):
        raise SettingsValidationError("请填写有效的公网 HTTPS 模型服务地址") from None
    if provider != "custom" and base_url != PROVIDERS[provider]:
        raise SettingsValidationError("预设服务商的地址不可更改，请选择自定义服务商")
    if not isinstance(model, str) or not _MODEL_PATTERN.fullmatch(model):
        raise SettingsValidationError("模型名称格式无效或超过 128 个字符")
    api_key = update.get("api_key")
    if api_key is not None and not isinstance(api_key, str):
        raise SettingsValidationError("Key 必须是文本")
    api_key = (api_key or "").strip()
    if api_key and (
        not 8 <= len(api_key) <= 4096
        or any(ord(char) < 33 or ord(char) > 126 for char in api_key)
    ):
        raise SettingsValidationError("Key 长度应为 8–4096，且不能包含空白或控制字符")
    delete_key = update.get("delete_key", False)
    if type(delete_key) is not bool:
        raise SettingsValidationError("删除 Key 标记必须是布尔值")
    if delete_key and api_key:
        raise SettingsValidationError("不能同时保存和删除 Key")
    if (
        (provider, base_url) != (old_provider, old_base_url)
        and not api_key
        and not delete_key
    ):
        raise SettingsValidationError(
            "更换服务商或地址时请提供新 Key，或明确删除原 Key"
        )
    row[f"{kind}_provider"] = provider
    row[f"{kind}_base_url"] = base_url
    row[f"{kind}_model"] = model
    if delete_key:
        row[f"{kind}_key_ciphertext"] = None
        row[f"{kind}_key_last4"] = None
    elif api_key:
        row[f"{kind}_key_ciphertext"] = _encrypt(
            api_key, user_id, kind, provider, base_url
        )
        row[f"{kind}_key_last4"] = api_key[-4:]


def update_model_settings(user_id: int, expected_revision: int, **updates) -> dict:
    """CAS 同时保护并发编辑和入库锁；不会因保存配置而创建失效账号。"""
    _check_user_id(user_id)
    if type(expected_revision) is not int or expected_revision < 0:
        raise SettingsValidationError("配置版本无效")
    if set(updates) - set(ALLOWED_PROVIDERS):
        raise SettingsValidationError("模型配置包含未知字段")
    with _get_conn() as conn:
        row = _read_settings(conn, user_id)
        if row["revision"] != expected_revision:
            raise SettingsConflict("配置已被其他页面修改，请刷新后重试")
        old_embedding = (
            row["embedding_provider"],
            row["embedding_base_url"],
            row["embedding_model"],
        )
        for kind, update in updates.items():
            if not isinstance(update, dict):
                raise SettingsValidationError("模型配置必须是对象")
            _apply_update(row, user_id, kind, update)
        embedding_changed = old_embedding != (
            row["embedding_provider"],
            row["embedding_base_url"],
            row["embedding_model"],
        )
        if embedding_changed and row["embedding_locked"]:
            raise EmbeddingModelLocked(_LOCK_REASON)
        # INSERT SELECT 让认证后并发删号也不会重新造出孤立的凭据行。
        conn.execute(
            "INSERT INTO user_model_settings (user_id) "
            "SELECT id FROM users WHERE id = ? ON CONFLICT (user_id) DO NOTHING",
            (user_id,),
        )
        fields = [
            f"{kind}_{field}"
            for kind in ALLOWED_PROVIDERS
            for field in (
                "provider",
                "base_url",
                "model",
                "key_ciphertext",
                "key_last4",
            )
        ]
        assignments = ", ".join(f"{field} = ?" for field in fields)
        lock_guard = " AND embedding_locked = 0" if embedding_changed else ""
        changed = conn.execute(
            f"UPDATE user_model_settings SET {assignments}, revision = revision + 1, "
            "updated_at = datetime('now') WHERE user_id = ? AND revision = ?"
            + lock_guard,
            tuple(row[field] for field in fields) + (user_id, expected_revision),
        ).rowcount
        if not changed:
            raise SettingsConflict("配置或索引状态已改变，请刷新后重试")
        saved = _read_settings(conn, user_id)
        conn.commit()
    return _public_settings(saved)


def lock_embedding_model(user_id: int, expected_revision: int) -> None:
    """在写入首个向量前锁定同一配置快照，避免与模型设置修改交错。"""
    _check_user_id(user_id)
    with _get_conn() as conn:
        changed = conn.execute(
            "UPDATE user_model_settings SET embedding_locked = 1 "
            "WHERE user_id = ? AND revision = ? AND embedding_key_ciphertext IS NOT NULL",
            (user_id, expected_revision),
        ).rowcount
        if not changed:
            raise SettingsConflict("Embedding 配置已改变，请重新开始导入")
        conn.commit()
