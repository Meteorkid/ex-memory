"""关系时间线与跨会话状态（FR-065 / FR-066）。

现有三层记忆都是空间维度的：ta 说过什么、ta 是什么样的人。
缺的是时间维度与连续性。
"""

import hashlib
import os
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from core.relationship import (
    add_event,
    clear_for_mirror,
    delete_event,
    exe_key,
    get_state,
    list_events,
    set_state,
    state_prompt,
    timeline_prompt,
)

os.environ["EX_MEMORY_TEST"] = "1"


@pytest.fixture
def env(tmp_path, monkeypatch):
    import server.auth as auth
    import server.routes as routes_mod

    db = tmp_path / "users.db"
    monkeypatch.setattr(auth, "DB_PATH", db)
    monkeypatch.setattr(auth, "DB_DIR", db.parent)
    auth.init_db()
    monkeypatch.setattr("config.EXES_DIR", tmp_path / "exes")
    noop = MagicMock()
    noop.check = MagicMock()
    monkeypatch.setattr(routes_mod, "_login_limiter", noop)
    return tmp_path


@pytest.fixture
def client(env):
    from server.app import create_app

    return TestClient(create_app())


def _account(client, request):
    name = "rel_" + hashlib.md5(request.node.name.encode()).hexdigest()[:12]
    client.post("/api/auth/register", json={"username": name, "password": "test123456"})
    token = client.post(
        "/api/auth/login", json={"username": name, "password": "test123456"}
    ).json()["token"]
    from server.auth import validate_token

    return {"Authorization": f"Bearer {token}"}, validate_token(token)


class TestExeKey:
    def test_includes_owner(self):
        assert exe_key("xiaoyu", 12) == "12/xiaoyu"

    def test_flat_without_owner(self):
        assert exe_key("xiaoyu") == "xiaoyu"


class TestTimeline:
    def test_add_and_list(self, env):
        add_event("s", "第一次一起看海", owner=1, happened_at="2024-07", emotion="开心")
        events = list_events("s", 1)
        assert len(events) == 1
        assert events[0]["event"] == "第一次一起看海"
        assert events[0]["emotion"] == "开心"

    def test_ordered_by_time(self, env):
        add_event("s", "后来", owner=1, happened_at="2025-01")
        add_event("s", "更早", owner=1, happened_at="2024-01")
        assert [e["event"] for e in list_events("s", 1)] == ["更早", "后来"]

    def test_mirrors_are_isolated(self, env):
        add_event("a", "A 的事", owner=1)
        add_event("b", "B 的事", owner=1)
        assert len(list_events("a", 1)) == 1

    def test_owners_are_isolated(self, env):
        add_event("same", "用户1的事", owner=1)
        assert list_events("same", 2) == []

    def test_empty_event_rejected(self, env):
        with pytest.raises(ValueError):
            add_event("s", "   ", owner=1)

    def test_delete_checks_ownership(self, env):
        event_id = add_event("s", "事件", owner=1)
        # 换个 owner 删不掉，避免删到别的镜像
        assert delete_event(event_id, "s", 2) is False
        assert delete_event(event_id, "s", 1) is True


class TestTimelinePrompt:
    def test_empty_timeline_costs_no_tokens(self, env):
        assert timeline_prompt("s", 1) == ""

    def test_events_appear_with_time_and_emotion(self, env):
        add_event("s", "一起看海", owner=1, happened_at="2024-07", emotion="开心")
        prompt = timeline_prompt("s", 1)
        assert "2024-07" in prompt and "一起看海" in prompt and "开心" in prompt

    def test_prompt_discourages_mechanical_listing(self, env):
        add_event("s", "某事", owner=1)
        assert "不要刻意罗列" in timeline_prompt("s", 1)

    def test_prompt_is_capped(self, env):
        from core.relationship import MAX_TIMELINE_IN_PROMPT

        for i in range(MAX_TIMELINE_IN_PROMPT + 10):
            add_event("s", f"事件{i}", owner=1, happened_at=f"2024-{i % 12 + 1:02d}")
        assert timeline_prompt("s", 1).count("- ") <= MAX_TIMELINE_IN_PROMPT


class TestCrossSessionState:
    def test_absent_state_returns_none(self, env):
        assert get_state("s", 1) is None

    def test_set_and_get(self, env):
        set_state("s", owner=1, mood="有点低落", recent_context="最近在赶项目")
        state = get_state("s", 1)
        assert state["mood"] == "有点低落"
        assert state["recent_context"] == "最近在赶项目"

    def test_partial_update_keeps_the_other_field(self, env):
        """一次更新不该把另一半抹掉。"""
        set_state("s", owner=1, mood="开心", recent_context="在旅行")
        set_state("s", owner=1, mood="累")
        state = get_state("s", 1)
        assert state["mood"] == "累"
        assert state["recent_context"] == "在旅行"

    def test_state_prompt_empty_when_unset(self, env):
        assert state_prompt("s", 1) == ""

    def test_state_prompt_carries_mood(self, env):
        set_state("s", owner=1, mood="有点低落")
        prompt = state_prompt("s", 1)
        assert "有点低落" in prompt
        assert "不必主动汇报" in prompt


class TestCleanup:
    def test_mirror_deletion_clears_db_rows(self, env):
        """时间线与状态在库里，不会随目录一起消失。"""
        add_event("s", "事件", owner=1)
        set_state("s", owner=1, mood="开心")
        clear_for_mirror("s", 1)
        assert list_events("s", 1) == []
        assert get_state("s", 1) is None

    def test_delete_endpoint_clears_relationship_data(self, client, request, env):
        headers, user_id = _account(client, request)
        ex_dir = env / "exes" / str(user_id) / "gone"
        ex_dir.mkdir(parents=True)
        (ex_dir / "meta.json").write_text(
            '{"owner_user_id": %d}' % user_id, encoding="utf-8"
        )
        add_event("gone", "事件", owner=user_id)

        resp = client.request(
            "DELETE", "/api/exes/gone", json={"confirm": True}, headers=headers
        )
        assert resp.status_code == 200
        assert list_events("gone", user_id) == []


class TestApi:
    def test_timeline_crud(self, client, request, env):
        headers, user_id = _account(client, request)
        with patch("server.routes._check_exe_access", lambda slug, uid: slug):
            add = client.post(
                "/api/exes/demo/timeline",
                json={"event": "一起看海", "happened_at": "2024-07", "emotion": "开心"},
                headers=headers,
            )
            assert add.status_code == 200

            events = client.get("/api/exes/demo/timeline", headers=headers).json()[
                "events"
            ]
            assert len(events) == 1

            removed = client.delete(
                f"/api/exes/demo/timeline/{events[0]['id']}", headers=headers
            )
            assert removed.status_code == 200
            assert (
                client.get("/api/exes/demo/timeline", headers=headers).json()["events"]
                == []
            )

    def test_state_roundtrip(self, client, request, env):
        headers, _user_id = _account(client, request)
        with patch("server.routes._check_exe_access", lambda slug, uid: slug):
            client.put(
                "/api/exes/demo/state",
                json={"mood": "想你", "recent_context": "在加班"},
                headers=headers,
            )
            body = client.get("/api/exes/demo/state", headers=headers).json()
        assert body["mood"] == "想你"

    def test_timeline_requires_auth(self, client):
        assert client.get("/api/exes/demo/timeline").status_code == 401
