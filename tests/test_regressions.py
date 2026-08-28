"""针对已修复缺陷的回归测试。

每个用例都对应一个真实存在过的 bug，防止再次退化。
"""

import json
import pathlib
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

    # 只 patch EXES_DIR：get_ex_dir 在调用时才读它，因此天然跟随。
    # 若改为 patch get_ex_dir 本身，会被 routes 的 from-import 在首次导入时
    # 永久捕获，导致后续用例的重定向全部失效（与执行顺序耦合）。
    monkeypatch.setattr("config.EXES_DIR", exes)
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


class TestMetaPathHonorsConfig:
    """_load_meta / _save_meta 曾硬编码 PROJECT_DIR / "exes"。

    get_ex_dir 在调用时读取 config.EXES_DIR，因此跟随配置；而 PROJECT_DIR
    是常量，写死后这两个函数会绕开配置直接读写仓库里的真实 exes/ 目录，
    与同文件其余 4 处调用及全仓其它模块的口径分裂。
    """

    def test_load_meta_reads_configured_dir(self, client):
        """读路径：走 _load_meta 的端点必须能读到配置指向的镜像。"""
        token = _login(client)
        r = client.get(
            "/api/exes/owned/stage", headers={"Authorization": f"Bearer {token}"}
        )
        assert r.status_code == 200, "回归：_load_meta 未跟随 EXES_DIR 配置"
        assert r.json()["stage"] == "dating"

    def test_save_meta_writes_to_configured_dir(self, client, tmp_path):
        """写路径：写入必须落在配置目录，且真实 exes/ 不被触碰。"""
        import config

        token = _login(client)
        r = client.put(
            "/api/exes/owned/stage?stage=healing",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert r.status_code == 200

        written = json.loads(
            (tmp_path / "exes" / "owned" / "meta.json").read_text(encoding="utf-8")
        )
        assert written["stage"] == "healing", "回归：_save_meta 未写入配置目录"

        real = pathlib.Path(config.PROJECT_DIR) / "exes" / "owned"
        assert not real.exists(), "回归：写到了仓库真实 exes/ 目录"
