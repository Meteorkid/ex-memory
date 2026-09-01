"""同意留痕、数据主体请求、账户级导出与级联删除（FR-015 ~ FR-019）。

其中删除完整性回查是 NFR-034 的看门人：数据平面迁到 Postgres / 对象存储 /
pgvector 之后，如果删除路径没同步改造，这里会立刻变红。
"""

import hashlib
import json
import os
import zipfile
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

os.environ["EX_MEMORY_TEST"] = "1"


@pytest.fixture
def env(tmp_path, monkeypatch):
    """隔离数据库、镜像目录、贴纸目录与反馈文件。"""
    import server.account_lifecycle as lifecycle
    import server.auth as auth
    import server.routes as routes_mod
    import core.sticker_manager as stickers

    db = tmp_path / "users.db"
    monkeypatch.setattr(auth, "DB_PATH", db)
    monkeypatch.setattr(auth, "DB_DIR", db.parent)
    auth.init_db()

    exes = tmp_path / "exes"
    exes.mkdir()
    monkeypatch.setattr("config.EXES_DIR", exes)
    monkeypatch.setattr(stickers, "CUSTOM_BASE", tmp_path / "stickers")
    feedback = tmp_path / "feedback.jsonl"
    monkeypatch.setattr(lifecycle, "_feedback_path", lambda: feedback)

    noop = MagicMock()
    noop.check = MagicMock()
    monkeypatch.setattr(routes_mod, "_login_limiter", noop)

    routes_mod._engine_cache.clear()
    routes_mod._session_counters.clear()
    yield {"exes": exes, "stickers": tmp_path / "stickers", "feedback": feedback}
    routes_mod._engine_cache.clear()
    routes_mod._session_counters.clear()


@pytest.fixture
def client(env):
    from server.app import create_app

    return TestClient(create_app())


def _register(client, request, suffix=""):
    uname = (
        "acct_" + hashlib.md5((request.node.name + suffix).encode()).hexdigest()[:12]
    )
    client.post(
        "/api/auth/register", json={"username": uname, "password": "test123456"}
    )
    resp = client.post(
        "/api/auth/login", json={"username": uname, "password": "test123456"}
    )
    token = resp.json()["token"]
    from server.auth import validate_token

    return {"Authorization": f"Bearer {token}"}, validate_token(token)


class TestConsent:
    def test_record_and_list(self, client, request):
        headers, user_id = _register(client, request)
        resp = client.post(
            "/api/consents",
            json={"policy_type": "third_party_data", "policy_version": "v1"},
            headers=headers,
        )
        assert resp.status_code == 200

        history = client.get("/api/consents", headers=headers).json()["consents"]
        assert len(history) == 1
        assert history[0]["policy_type"] == "third_party_data"
        assert history[0]["policy_version"] == "v1"
        assert history[0]["granted_at"]

    def test_version_mismatch_counts_as_not_consented(self, client, request):
        """协议改版后旧同意自动失效——只记「同意过」等于没有留痕。"""
        headers, user_id = _register(client, request)
        client.post(
            "/api/consents",
            json={"policy_type": "third_party_data", "policy_version": "v1"},
            headers=headers,
        )
        from server.consent_store import has_consented

        assert has_consented(user_id, "third_party_data", "v1") is True
        assert has_consented(user_id, "third_party_data", "v2") is False

    def test_unknown_policy_rejected(self, client, request):
        headers, _ = _register(client, request)
        resp = client.post(
            "/api/consents",
            json={"policy_type": "不存在的协议", "policy_version": "v1"},
            headers=headers,
        )
        assert resp.status_code == 400

    def test_consent_records_ip(self, client, request):
        headers, user_id = _register(client, request)
        client.post(
            "/api/consents",
            json={"policy_type": "privacy", "policy_version": "v1"},
            headers=headers,
        )
        from server.consent_store import list_consents

        assert list_consents(user_id)[0]["ip"]


class TestSubjectRequest:
    def test_accepts_submission_without_login(self, client):
        """🔴 被模拟者从来不是本站用户，这个入口必须免登录。"""
        resp = client.post(
            "/api/safety/report",
            json={
                "claim_type": "subject_complaint",
                "contact": "someone@example.com",
                "target_hint": "昵称小雨，2024 年的记录",
                "detail": "未经我同意用我的聊天记录建了镜像",
            },
        )
        assert resp.status_code == 200
        assert "受理编号" in resp.json()["message"]

    def test_deceased_kin_claim_is_supported(self, client):
        resp = client.post(
            "/api/safety/report",
            json={
                "claim_type": "deceased_kin",
                "contact": "13800000000",
                "identity_evidence": "户口本关系页，已另行提交",
            },
        )
        assert resp.status_code == 200

    def test_unknown_claim_type_rejected(self, client):
        resp = client.post(
            "/api/safety/report",
            json={"claim_type": "随便写的", "contact": "a@b.com"},
        )
        assert resp.status_code == 400

    def test_request_enters_queue(self, client):
        client.post(
            "/api/safety/report",
            json={"claim_type": "subject_complaint", "contact": "x@y.com"},
        )
        from server.consent_store import list_subject_requests

        pending = list_subject_requests()
        assert len(pending) == 1
        assert pending[0]["claim_type"] == "subject_complaint"


class TestCrisisResourcesEndpoint:
    def test_available_without_login(self, client):
        """需要求助资源的人未必登录得进来。"""
        resp = client.get("/api/safety/crisis-resources")
        assert resp.status_code == 200
        assert resp.json()["message"].strip()

    def test_unreviewed_exposes_no_numbers(self, client):
        assert client.get("/api/safety/crisis-resources").json()["hotlines"] == []


def _populate_user_data(env, client, headers, user_id):
    """给账号铺满五处存储的数据，供导出与删除测试使用。"""
    import server.routes as routes_mod
    from server.safety_store import record_safety_event

    ex_dir = env["exes"] / str(user_id) / "mine"
    ex_dir.mkdir(parents=True)
    (ex_dir / "meta.json").write_text(
        json.dumps({"name": "mine", "owner_user_id": user_id}), encoding="utf-8"
    )
    (ex_dir / "SKILL.md").write_text("ta 的人格", encoding="utf-8")

    sticker_dir = env["stickers"] / f"u{user_id}"
    sticker_dir.mkdir(parents=True)
    (sticker_dir / "custom.json").write_text("[]", encoding="utf-8")

    env["feedback"].write_text(
        json.dumps({"user_id": user_id, "content": "我的反馈"}, ensure_ascii=False)
        + "\n"
        + json.dumps({"user_id": 999999, "content": "别人的反馈"}, ensure_ascii=False)
        + "\n",
        encoding="utf-8",
    )

    client.post(
        "/api/consents",
        json={"policy_type": "privacy", "policy_version": "v1"},
        headers=headers,
    )
    record_safety_event(
        user_id=user_id,
        event_type="crisis",
        severity="high",
        action_taken="interrupted",
        slug="mine",
        raw_text="我不想活了",
    )
    routes_mod._engine_cache[(user_id, "mine")] = object()
    routes_mod._session_counters[(user_id, "mine")] = object()


class TestExport:
    def test_export_contains_account_and_mirror_files(self, env, client, request):
        headers, user_id = _register(client, request)
        _populate_user_data(env, client, headers, user_id)

        from server.account_lifecycle import export_account

        zip_path = export_account(user_id)
        try:
            with zipfile.ZipFile(zip_path) as zf:
                names = zf.namelist()
                manifest = json.loads(zf.read("account.json"))
            assert "exes/mine/SKILL.md" in names
            assert "feedback.jsonl" in names
            assert manifest["account"]["id"] == user_id
            assert manifest["consents"]
        finally:
            zip_path.unlink(missing_ok=True)

    def test_export_excludes_credentials(self, env, client, request):
        """导出的是个人信息，不是数据库备份。"""
        headers, user_id = _register(client, request)
        _populate_user_data(env, client, headers, user_id)

        from server.account_lifecycle import export_account

        zip_path = export_account(user_id)
        try:
            with zipfile.ZipFile(zip_path) as zf:
                manifest = json.loads(zf.read("account.json"))
            assert "password_hash" not in manifest["account"]
            assert "salt" not in manifest["account"]
        finally:
            zip_path.unlink(missing_ok=True)


class TestDeletionCompleteness:
    def test_deletion_leaves_no_residue(self, env, client, request):
        """🔴 NFR-034 看门人：五处存储回查必须全空。"""
        headers, user_id = _register(client, request)
        _populate_user_data(env, client, headers, user_id)

        from server.account_lifecycle import delete_account, verify_deletion

        assert verify_deletion(user_id), "铺数据后回查竟然是干净的，说明回查失效"
        delete_account(user_id)
        assert verify_deletion(user_id) == []

    def test_other_users_data_is_untouched(self, env, client, request):
        headers_a, user_a = _register(client, request, "a")
        headers_b, user_b = _register(client, request, "b")
        _populate_user_data(env, client, headers_a, user_a)
        _populate_user_data(env, client, headers_b, user_b)

        from server.account_lifecycle import delete_account, verify_deletion

        delete_account(user_a)
        assert verify_deletion(user_a) == []
        assert verify_deletion(user_b), "误删了另一个账号的数据"

    def test_verify_catches_residue_when_a_store_is_missed(self, env, client, request):
        """回查本身要有效：漏删任意一处都必须被抓到。"""
        headers, user_id = _register(client, request)
        _populate_user_data(env, client, headers, user_id)

        from server.account_lifecycle import delete_account, verify_deletion

        delete_account(user_id)
        # 手工制造一处残留，模拟未来某次改造漏删
        (env["exes"] / str(user_id) / "mine").mkdir(parents=True)
        residues = verify_deletion(user_id)
        assert residues and "镜像目录残留" in residues[0]

    def test_safety_events_are_anonymized_not_deleted(self, env, client, request):
        """危机事件保留聚合统计价值，但去掉关联后不再是个人信息。

        ⚠️ 这一条属于法务判断。若法务要求连同删除，改 _anonymize_safety_events。
        """
        headers, user_id = _register(client, request)
        _populate_user_data(env, client, headers, user_id)

        from server.account_lifecycle import delete_account
        from server.auth import _get_conn

        delete_account(user_id)
        with _get_conn() as conn:
            rows = conn.execute(
                "SELECT user_id, excerpt, input_hash, event_type FROM safety_events"
            ).fetchall()
        assert len(rows) == 1
        assert rows[0]["user_id"] is None
        assert rows[0]["excerpt"] is None
        assert rows[0]["input_hash"] is None
        assert rows[0]["event_type"] == "crisis"

    def test_delete_mode_removes_rows_entirely(self, env, client, request, monkeypatch):
        """法务若裁定连同删除，切配置即可，不必改代码。"""
        monkeypatch.setattr("config.SAFETY_EVENT_DELETION_MODE", "delete")
        headers, user_id = _register(client, request)
        _populate_user_data(env, client, headers, user_id)

        from server.account_lifecycle import delete_account, verify_deletion
        from server.auth import _get_conn

        receipt = delete_account(user_id)
        assert receipt["safety_events"] == "deleted"
        with _get_conn() as conn:
            assert conn.execute("SELECT COUNT(*) FROM safety_events").fetchone()[0] == 0
        assert verify_deletion(user_id) == []

    def test_anonymize_mode_is_reported_in_receipt(self, env, client, request):
        headers, user_id = _register(client, request)
        _populate_user_data(env, client, headers, user_id)

        from server.account_lifecycle import delete_account

        assert delete_account(user_id)["safety_events"] == "anonymized"

    def test_delete_endpoint_requires_confirmation(self, client, request):
        headers, _ = _register(client, request)
        resp = client.request(
            "DELETE", "/api/account", json={"confirm": False}, headers=headers
        )
        assert resp.status_code == 400

    def test_delete_endpoint_wipes_and_verifies(self, env, client, request):
        headers, user_id = _register(client, request)
        _populate_user_data(env, client, headers, user_id)

        resp = client.request(
            "DELETE", "/api/account", json={"confirm": True}, headers=headers
        )
        assert resp.status_code == 200

        from server.account_lifecycle import verify_deletion

        assert verify_deletion(user_id) == []
