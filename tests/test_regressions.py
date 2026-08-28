"""针对已修复缺陷的回归测试。

每个用例都对应一个真实存在过的 bug，防止再次退化。
"""

import json
import time
import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client(tmp_path, monkeypatch):
    test_db = tmp_path / "users.db"
    monkeypatch.setattr("server.auth.DB_PATH", test_db)
    monkeypatch.setattr("server.auth.DB_DIR", test_db.parent)
    import server.auth as auth

    with auth._get_conn() as conn:
        conn.execute("DROP TABLE IF EXISTS tokens")
        conn.execute("DROP TABLE IF EXISTS users")
        conn.commit()
    auth.init_db()

    exes = tmp_path / "exes"
    exes.mkdir()
    # user_id=1 拥有 owned，另一个用户拥有 other
    for slug, owner, n_msgs in (("owned", 1, 3), ("other", 999, 5)):
        ex_dir = exes / slug
        (ex_dir / "conversations").mkdir(parents=True)
        (ex_dir / "meta.json").write_text(
            json.dumps(
                {
                    "name": slug,
                    "slug": slug,
                    "owner_user_id": owner,
                    "created_at": "2024-01-01",
                }
            ),
            encoding="utf-8",
        )
        (ex_dir / "conversations" / "conversation.jsonl").write_text(
            "".join(
                json.dumps({"role": "user", "content": f"m{i}"}) + "\n"
                for i in range(n_msgs)
            ),
            encoding="utf-8",
        )

    monkeypatch.setattr("config.EXES_DIR", exes)
    monkeypatch.setattr("config.get_ex_dir", lambda s: exes / s)
    monkeypatch.setattr("config.SINGLE_USER_MODE", False)

    # 登录限流器是模块级单例，用例间必须隔离，否则相互挤占配额
    import server.routes as routes

    monkeypatch.setattr(routes, "_login_limiter", None)

    from server.app import create_app

    return TestClient(create_app())


def _login(client, username="u1", password="pass1234"):
    client.post("/api/auth/register", json={"username": username, "password": password})
    r = client.post(
        "/api/auth/login", json={"username": username, "password": password}
    )
    return r.json()["token"]


class TestStatsUsage:
    """/stats/usage 曾因 import 不存在的 check_access 而恒定返回 0。"""

    def test_counts_owned_exes_not_zero(self, client):
        token = _login(client)
        r = client.get("/api/stats/usage", headers={"Authorization": f"Bearer {token}"})
        assert r.status_code == 200
        body = r.json()
        # 首个注册用户 user_id=1，拥有 owned（3 条消息）
        assert body["total_exes"] == 1, "回归：统计不应恒为 0"
        assert body["total_messages"] == 3

    def test_excludes_other_users_exes(self, client):
        token = _login(client)
        body = client.get(
            "/api/stats/usage", headers={"Authorization": f"Bearer {token}"}
        ).json()
        # other 属于 user_id=999，不得计入
        assert body["total_exes"] == 1
        assert body["total_messages"] == 3


class TestTokenExpiryTimezone:
    """expires_at 曾用本地时间写入，却与 SQLite 的 UTC datetime('now') 比较。"""

    def test_expiry_written_in_utc(self, client):
        import server.auth as auth

        _login(client, "tzuser", "pass1234")
        with auth._get_conn() as conn:
            expires = conn.execute(
                "SELECT expires_at FROM tokens ORDER BY rowid DESC LIMIT 1"
            ).fetchone()[0]

        expected = time.strftime(
            "%Y-%m-%d %H:%M:%S",
            time.gmtime(time.time() + auth.TOKEN_EXPIRY_SECONDS),
        )
        # 允许几秒执行误差，但必须是 UTC 而非本地时区
        assert (
            abs(
                time.mktime(time.strptime(expires, "%Y-%m-%d %H:%M:%S"))
                - time.mktime(time.strptime(expected, "%Y-%m-%d %H:%M:%S"))
            )
            < 5
        ), "回归：expires_at 必须以 UTC 写入"

    def test_clean_expired_tokens_agrees_with_validate(self, client):
        """清理与校验必须同口径：清理后仍有效的 token 不应被删。"""
        import server.auth as auth

        token = _login(client, "tzuser2", "pass1234")
        assert auth.validate_token(token) is not None
        auth.clean_expired_tokens()
        assert auth.validate_token(token) is not None, "回归：新 token 被误清理"

    def test_expired_token_rejected_and_cleaned(self, client):
        import server.auth as auth

        token = _login(client, "tzuser3", "pass1234")
        past = time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(time.time() - 3600))
        with auth._get_conn() as conn:
            conn.execute("UPDATE tokens SET expires_at = ?", (past,))
            conn.commit()
        assert auth.validate_token(token) is None


class TestFeedbackValidation:
    """/feedback 曾用查询串接收正文且无长度上限。"""

    def test_rejects_oversized_content(self, client):
        token = _login(client)
        r = client.post(
            "/api/feedback",
            json={"feedback_type": "bug", "content": "x" * 5000},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert r.status_code == 422, "回归：超长正文必须被拒"

    def test_accepts_normal_content(self, client):
        token = _login(client)
        r = client.post(
            "/api/feedback",
            json={"feedback_type": "bug", "content": "登录页样式错位"},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert r.status_code == 200
        assert r.json()["ok"] is True

    def test_requires_auth(self, client):
        r = client.post("/api/feedback", json={"feedback_type": "bug", "content": "x"})
        assert r.status_code == 401


class TestGroupsOwnershipFilter:
    """/exes/groups 曾直接遍历 exes/ 目录，把他人镜像的 slug 与昵称暴露给任何登录用户。"""

    def test_only_lists_own_exes(self, client):
        token = _login(client)
        r = client.get("/api/exes/groups", headers={"Authorization": f"Bearer {token}"})
        assert r.status_code == 200

        listed = {
            item["slug"] for entries in r.json()["groups"].values() for item in entries
        }
        assert "owned" in listed
        assert "other" not in listed, "回归：泄露了他人镜像"

    def test_requires_auth(self, client):
        assert client.get("/api/exes/groups").status_code == 401
