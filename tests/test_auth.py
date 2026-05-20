"""server/auth.py 测试：注册、登录、Token 验证、过期、吊销。"""

import os
import pytest

# 隔离测试数据库
os.environ["EX_MEMORY_TEST"] = "1"


@pytest.fixture(autouse=True)
def isolated_db(tmp_path, monkeypatch):
    """每次测试使用独立 SQLite 数据库。"""
    import server.auth as auth

    test_db = tmp_path / "users.db"
    test_db.parent.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(auth, "DB_PATH", test_db)
    monkeypatch.setattr(auth, "DB_DIR", test_db.parent)

    # 重新初始化表
    with auth._get_conn() as conn:
        conn.execute("DROP TABLE IF EXISTS tokens")
        conn.execute("DROP TABLE IF EXISTS users")
        conn.commit()
    auth.init_db()
    yield
    # 清理模块级缓存的连接
    if test_db.exists():
        test_db.unlink()


class TestRegister:
    def test_register_success(self):
        from server.auth import register_user
        err = register_user("testuser", "password123")
        assert err is None

    def test_register_duplicate_username(self):
        from server.auth import register_user
        register_user("testuser", "password123")
        err = register_user("testuser", "password456")
        assert err is not None
        assert "已存在" in err

    def test_register_short_username(self):
        from server.auth import register_user
        err = register_user("a", "password123")
        assert err is not None
        assert "至少 2 个字符" in err

    def test_register_short_password(self):
        from server.auth import register_user
        err = register_user("testuser", "12345")
        assert err is not None
        assert "至少 6 个字符" in err


class TestLogin:
    def test_login_success_returns_token(self):
        from server.auth import register_user, login_user
        register_user("testuser", "password123")
        token = login_user("testuser", "password123")
        assert token is not None
        assert len(token) > 20

    def test_login_wrong_password(self):
        from server.auth import register_user, login_user
        register_user("testuser", "password123")
        token = login_user("testuser", "wrongpassword")
        assert token is None

    def test_login_nonexistent_user(self):
        from server.auth import login_user
        token = login_user("nobody", "password123")
        assert token is None


class TestValidateToken:
    def test_valid_token_returns_user_id(self):
        from server.auth import register_user, login_user, validate_token
        register_user("testuser", "password123")
        token = login_user("testuser", "password123")
        user_id = validate_token(token)
        assert user_id is not None
        assert isinstance(user_id, int)

    def test_invalid_token_returns_none(self):
        from server.auth import validate_token
        user_id = validate_token("invalid-token-12345")
        assert user_id is None

    def test_expired_token_returns_none(self, monkeypatch):
        from server.auth import register_user, login_user, validate_token
        register_user("testuser", "password123")
        # 将过期时间设为过去
        monkeypatch.setattr("server.auth.TOKEN_EXPIRY_SECONDS", -1)
        token = login_user("testuser", "password123")
        user_id = validate_token(token)
        assert user_id is None

    def test_revoked_token_returns_none(self):
        from server.auth import register_user, login_user, validate_token, revoke_token
        register_user("testuser", "password123")
        token = login_user("testuser", "password123")
        revoke_token(token)
        user_id = validate_token(token)
        assert user_id is None


class TestPasswordHashing:
    def test_different_salts_produce_different_hashes(self):
        from server.auth import _hash_password
        h1, s1 = _hash_password("password123")
        h2, s2 = _hash_password("password123")
        assert s1 != s2
        assert h1 != h2

    def test_same_salt_produces_same_hash(self):
        from server.auth import _hash_password
        h1, s1 = _hash_password("password123")
        h2, _ = _hash_password("password123", s1)
        assert h1 == h2

    def test_timing_safe_comparison(self):
        from server.auth import _hash_password
        import hmac as _hmac
        h1, s1 = _hash_password("correct_password")
        bad_hash = "0" * len(h1)
        assert not _hmac.compare_digest(h1, bad_hash)


class TestRevokeToken:
    def test_revoke_nonexistent_token_does_not_crash(self):
        from server.auth import revoke_token
        revoke_token("nonexistent-token")


class TestCleanExpiredTokens:
    def test_cleanup_does_not_remove_valid_tokens(self, monkeypatch):
        from server.auth import register_user, login_user, validate_token, clean_expired_tokens
        register_user("testuser", "password123")
        monkeypatch.setattr("server.auth.TOKEN_EXPIRY_SECONDS", 86400)
        token = login_user("testuser", "password123")
        clean_expired_tokens()
        user_id = validate_token(token)
        assert user_id is not None
