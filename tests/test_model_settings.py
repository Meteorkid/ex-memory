"""账号凭据隔离、加密、并发编辑及向量模型锁的回归测试。"""

import base64
import json
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest

from server import model_settings as settings


@pytest.fixture
def accounts(tmp_path, monkeypatch):
    import config
    from server import auth

    monkeypatch.setattr(auth, "DB_PATH", tmp_path / "users.db")
    monkeypatch.setattr(auth, "DB_DIR", tmp_path)
    monkeypatch.setattr(config, "DATABASE_URL", "")
    monkeypatch.setattr(
        config,
        "MODEL_SETTINGS_ENCRYPTION_KEY",
        base64.urlsafe_b64encode(b"t" * 32).decode(),
        raising=False,
    )
    auth.init_db()
    for name in ("model_alice", "model_bob"):
        assert auth.register_user(name, "test-password") is None
    return (1, 2)


def _save(user_id, revision=0, **updates):
    return settings.update_model_settings(user_id, revision, **updates)


def test_default_settings_are_public_and_do_not_create_rows(accounts):
    from server.auth import _get_conn

    result = settings.get_public_settings(accounts[0])
    assert result["revision"] == 0
    assert result["storage_available"] is True
    assert result["llm"] == {
        "provider": "deepseek",
        "model": "deepseek-chat",
        "base_url": "https://api.deepseek.com",
        "has_key": False,
        "key_last4": None,
    }
    with _get_conn() as conn:
        assert (
            conn.execute("SELECT COUNT(*) AS n FROM user_model_settings").fetchone()[
                "n"
            ]
            == 0
        )


@pytest.mark.parametrize(
    ("provider", "kind", "protocol", "address"),
    [
        ("anthropic", "llm", "anthropic", "https://api.anthropic.com/v1"),
        ("custom_anthropic", "llm", "anthropic", "https://models.example.com/claude/v1"),
        ("gemini", "llm", "gemini", "https://generativelanguage.googleapis.com/v1beta"),
        ("gemini", "embedding", "gemini", "https://generativelanguage.googleapis.com/v1beta"),
        ("custom_gemini", "llm", "gemini", "https://models.example.com/google/v1beta"),
        ("custom_gemini", "embedding", "gemini", "https://models.example.com/google/v1beta"),
        ("custom", "llm", "openai", "https://models.example.com/v1"),
    ],
)
def test_native_protocol_configs_keep_owner_credentials_isolated(accounts, provider, kind, protocol, address):
    key = "synthetic-protocol-key-1234"
    model = "models/gemini-test" if protocol == "gemini" else "chat-test"
    result = _save(accounts[0], **{kind: {
        "provider": provider, "base_url": address, "model": model, "api_key": key,
    }})
    actual = settings.get_model_config(accounts[0], kind)
    assert actual["protocol"] == protocol
    assert actual["base_url"] == address
    assert actual["api_key"] == key
    assert actual["model"] == ("gemini-test" if protocol == "gemini" else model)
    assert not settings.get_public_settings(accounts[1])[kind]["has_key"]
    assert key not in json.dumps(result)


@pytest.mark.parametrize("provider", ["anthropic", "custom_anthropic"])
def test_messages_protocol_is_not_offered_as_an_embedding_api(accounts, provider):
    with pytest.raises(settings.SettingsValidationError):
        _save(accounts[0], embedding={"provider": provider, "api_key": "synthetic-key-1234"})
    assert settings.get_settings_revision(accounts[0]) == 0


def test_changing_protocol_at_same_address_requires_new_key_and_reencrypts(accounts):
    user_id = accounts[0]
    address = "https://models.example.com/v1"
    _save(user_id, llm={"provider": "custom", "base_url": address, "api_key": "old-secret-1234"})
    with pytest.raises(settings.SettingsValidationError):
        _save(user_id, 1, llm={"provider": "custom_anthropic", "base_url": address})
    _save(user_id, 1, llm={"provider": "custom_anthropic", "base_url": address, "api_key": "new-secret-5678"})
    actual = settings.get_model_config(user_id, "llm")
    assert actual["protocol"] == "anthropic"
    assert actual["api_key"] == "new-secret-5678"


def test_embedding_protocol_cannot_change_after_indexing(accounts):
    user_id = accounts[0]
    _save(user_id, embedding={"api_key": "synthetic-key-1234"})
    settings.lock_embedding_model(user_id, 1)
    with pytest.raises(settings.EmbeddingModelLocked):
        _save(user_id, 1, embedding={
            "provider": "custom_gemini", "base_url": "https://models.example.com/v1beta",
            "model": "embedding-test", "api_key": "different-key-5678",
        })


@pytest.mark.parametrize("model", ["models/../secret", "a/b", "a:generateContent", "../other"])
def test_gemini_model_name_cannot_inject_an_api_path(accounts, model):
    with pytest.raises(settings.SettingsValidationError):
        _save(accounts[0], llm={"provider": "gemini", "model": model, "api_key": "synthetic-key-1234"})


def test_keys_are_encrypted_and_bound_to_owner_and_kind(accounts):
    from server.auth import _get_conn

    alice, bob = accounts
    key = "sk-alice-private-1234"
    result = _save(alice, llm={"api_key": key}, embedding={"api_key": key})
    assert result["llm"]["key_last4"] == "1234"
    assert key not in json.dumps(result)
    assert "ciphertext" not in json.dumps(result)
    assert settings.get_model_config(alice, "llm")["api_key"] == key
    assert settings.get_public_settings(bob)["llm"]["has_key"] is False
    with _get_conn() as conn:
        row = dict(
            conn.execute(
                "SELECT * FROM user_model_settings WHERE user_id = ?", (alice,)
            ).fetchone()
        )
        assert key not in json.dumps(row)
        assert row["llm_key_ciphertext"] != row["embedding_key_ciphertext"]
        _save(bob, llm={"api_key": "sk-bob-private-5678"})
        conn.execute(
            "UPDATE user_model_settings SET llm_key_ciphertext = ? WHERE user_id = ?",
            (row["llm_key_ciphertext"], bob),
        )
        conn.commit()
    with pytest.raises(settings.SettingsUnavailable):
        settings.get_model_config(bob, "llm")
    with _get_conn() as conn:
        conn.execute(
            "UPDATE user_model_settings SET embedding_key_ciphertext = ? WHERE user_id = ?",
            (row["llm_key_ciphertext"], alice),
        )
        conn.commit()
    with pytest.raises(settings.SettingsUnavailable):
        settings.get_model_config(alice, "embedding")


def test_blank_key_preserves_delete_is_explicit_and_provider_change_needs_key(accounts):
    user_id = accounts[0]
    _save(user_id, llm={"api_key": "sk-first-secret-1234"})
    _save(user_id, 1, llm={"api_key": "  ", "model": "deepseek-reasoner"})
    assert (
        settings.get_model_config(user_id, "llm")["api_key"] == "sk-first-secret-1234"
    )
    with pytest.raises(settings.SettingsValidationError):
        _save(user_id, 2, llm={"provider": "openai", "model": "gpt-test"})
    _save(
        user_id,
        2,
        llm={
            "provider": "openai",
            "model": "gpt-test",
            "api_key": "sk-next-secret-5678",
        },
    )
    assert (
        settings.get_model_config(user_id, "llm")["base_url"]
        == "https://api.openai.com/v1"
    )
    deleted = _save(user_id, 3, llm={"delete_key": True})
    assert deleted["llm"]["key_last4"] is None
    with pytest.raises(settings.MissingModelSettings):
        settings.get_model_config(user_id, "llm")


@pytest.mark.parametrize(
    "master_key", ["", "bad", "é", base64.b64encode(b"too-short").decode()]
)
def test_missing_master_key_fails_closed_but_allows_deletion(
    accounts, monkeypatch, master_key
):
    user_id = accounts[0]
    _save(user_id, llm={"api_key": "sk-first-secret-1234"})
    monkeypatch.setattr("config.MODEL_SETTINGS_ENCRYPTION_KEY", master_key)
    monkeypatch.setattr("config.LLM_API_KEY", "owner-key-must-not-be-used")
    public = settings.get_public_settings(user_id)
    assert public["storage_available"] is False
    assert public["llm"]["has_key"] is True
    with pytest.raises(settings.SettingsUnavailable):
        settings.get_model_config(user_id, "llm")
    with pytest.raises(settings.SettingsUnavailable):
        _save(user_id, 1, llm={"api_key": "new-key-5678"})
    assert _save(user_id, 1, llm={"delete_key": True})["llm"]["has_key"] is False


def test_missing_account_key_never_uses_global_key(accounts, monkeypatch):
    monkeypatch.setattr("config.LLM_API_KEY", "owner-key-must-not-be-used")
    with pytest.raises(settings.MissingModelSettings):
        settings.get_model_config(accounts[0], "llm")


def test_tampered_ciphertext_and_changed_master_key_fail_closed(accounts, monkeypatch):
    from server.auth import _get_conn

    user_id = accounts[0]
    _save(user_id, llm={"api_key": "sk-first-secret-1234"})
    original = settings.get_public_settings(user_id)
    monkeypatch.setattr(
        "config.MODEL_SETTINGS_ENCRYPTION_KEY", base64.b64encode(b"x" * 32).decode()
    )
    with pytest.raises(settings.SettingsUnavailable):
        settings.get_model_config(user_id, "llm")
    with _get_conn() as conn:
        conn.execute(
            "UPDATE user_model_settings SET llm_key_ciphertext = 'v1.invalid' WHERE user_id = ?",
            (user_id,),
        )
        conn.commit()
    with pytest.raises(settings.SettingsUnavailable):
        settings.get_model_config(user_id, "llm")
    assert settings.get_public_settings(user_id)["revision"] == original["revision"]


def test_two_concurrent_edits_have_one_winner(accounts, monkeypatch):
    barrier = Barrier(2)
    original = settings._apply_update

    def apply_then_wait(*args):
        original(*args)
        barrier.wait(timeout=5)

    monkeypatch.setattr(settings, "_apply_update", apply_then_wait)

    def edit(key):
        try:
            return _save(accounts[0], llm={"api_key": key})
        except settings.SettingsConflict:
            return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(
            pool.map(edit, ("sk-first-secret-1111", "sk-second-secret-2222"))
        )
    assert sum(result is not None for result in results) == 1
    assert settings.get_settings_revision(accounts[0]) == 1


def test_embedding_lock_preserves_model_and_allows_key_rotation(accounts):
    user_id = accounts[0]
    _save(user_id, embedding={"api_key": "sk-embedding-key-1234"})
    settings.lock_embedding_model(user_id, 1)
    assert settings.get_settings_revision(user_id) == 1
    assert settings.get_model_config(user_id, "embedding")["locked"] is True
    assert settings.get_public_settings(user_id)["embedding"]["locked_reason"]
    with pytest.raises(settings.EmbeddingModelLocked):
        _save(user_id, 1, embedding={"model": "another-model"})
    with pytest.raises(settings.SettingsConflict):
        settings.lock_embedding_model(user_id, 0)
    _save(user_id, 1, embedding={"api_key": "sk-embedding-key-5678"})
    assert settings.get_model_config(user_id, "embedding")["api_key"].endswith("5678")
    _save(user_id, 2, embedding={"delete_key": True})
    assert settings.get_public_settings(user_id)["embedding"]["locked"] is True


def test_embedding_lock_racing_with_model_update_wins_in_database(
    accounts, monkeypatch
):
    user_id = accounts[0]
    _save(user_id, embedding={"api_key": "sk-embedding-key-1234"})
    original = settings._apply_update

    def apply_then_lock(*args):
        original(*args)
        settings.lock_embedding_model(user_id, 1)

    monkeypatch.setattr(settings, "_apply_update", apply_then_lock)
    with pytest.raises(settings.SettingsConflict):
        _save(user_id, 1, embedding={"model": "new-model"})
    public = settings.get_public_settings(user_id)
    assert public["embedding"]["locked"] is True
    assert public["embedding"]["model"] == "BAAI/bge-m3"


def test_settings_cannot_recreate_a_deleted_account(accounts):
    from server.auth import _get_conn

    with _get_conn() as conn:
        conn.execute("DELETE FROM users WHERE id = ?", (accounts[0],))
        conn.commit()
    with pytest.raises(settings.SettingsConflict):
        _save(accounts[0], llm={"api_key": "sk-first-secret-1234"})
    with _get_conn() as conn:
        assert (
            conn.execute("SELECT COUNT(*) AS n FROM user_model_settings").fetchone()[
                "n"
            ]
            == 0
        )


def test_custom_url_is_preserved_and_address_change_requires_new_key(accounts):
    user_id = accounts[0]
    _save(
        user_id,
        llm={
            "provider": "custom",
            "base_url": "https://models.example.com/v1/",
            "model": "custom-model",
            "api_key": "sk-custom-secret-1234",
        },
    )
    cfg = settings.get_model_config(user_id, "llm")
    assert cfg["base_url"] == "https://models.example.com/v1"
    _save(user_id, 1, llm={"api_key": "sk-custom-rotated-5678"})
    assert settings.get_model_config(user_id, "llm")["base_url"] == cfg["base_url"]
    with pytest.raises(settings.SettingsValidationError):
        _save(user_id, 2, llm={"base_url": "https://another.example.com/v1"})
    changed = _save(
        user_id,
        2,
        llm={
            "base_url": "https://another.example.com/v1",
            "delete_key": True,
        },
    )
    assert changed["llm"]["base_url"] == "https://another.example.com/v1"
    assert changed["llm"]["has_key"] is False


def test_custom_key_is_bound_to_exact_service_address(accounts):
    from server.auth import _get_conn

    user_id = accounts[0]
    _save(
        user_id,
        llm={
            "provider": "custom",
            "base_url": "https://models.example.com/v1",
            "api_key": "sk-custom-secret-1234",
        },
    )
    with _get_conn() as conn:
        conn.execute(
            "UPDATE user_model_settings SET llm_base_url = ? WHERE user_id = ?",
            ("https://another.example.com/v1", user_id),
        )
        conn.commit()
    with pytest.raises(settings.SettingsUnavailable):
        settings.get_model_config(user_id, "llm")


def test_embedding_address_cannot_change_after_lock(accounts):
    user_id = accounts[0]
    _save(
        user_id,
        embedding={
            "provider": "custom",
            "base_url": "https://models.example.com/v1",
            "api_key": "sk-custom-secret-1234",
        },
    )
    settings.lock_embedding_model(user_id, 1)
    with pytest.raises(settings.EmbeddingModelLocked):
        _save(
            user_id,
            1,
            embedding={
                "base_url": "https://another.example.com/v1",
                "api_key": "sk-new-service-key-5678",
            },
        )


@pytest.mark.parametrize(
    "update",
    [
        {"provider": "custom", "api_key": "sk-test-key-1234"},
        {"base_url": "http://127.0.0.1"},
        {"api_key": "short"},
        {"api_key": "sk-key-with\nnewline"},
        {"api_key": "sk-key-value-1234", "delete_key": True},
    ],
)
def test_unsupported_or_ambiguous_updates_are_rejected(accounts, update):
    with pytest.raises(settings.SettingsValidationError):
        _save(accounts[0], llm=update)
    assert settings.get_settings_revision(accounts[0]) == 0
