"""闸门链路延迟守卫（NFR-010 / NFR-060）。

每条消息进 LLM 之前要过四道闸门：危机识别 → 强度保护 → 内容审核 → 配额预扣。
它们叠在首字延迟前面，加起来必须可忽略——否则为了合规把体验拖垮，
用户会用脚投票，最后两头都没落着。

这里量的是**闸门本身**，不含 LLM 调用（那受网络与供应商影响，测不稳）。
"""

import hashlib
import os
import time
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

os.environ["EX_MEMORY_TEST"] = "1"

# 闸门总预算。PRD 给危机检查单项定的是 P95 < 200ms，
# 四道加起来留 300ms 已经很宽松。
GATE_BUDGET_MS = 300


@pytest.fixture
def client(tmp_path, monkeypatch):
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
    monkeypatch.setattr(routes_mod, "_check_exe_access", lambda slug, uid: slug)

    from server.app import create_app

    return TestClient(create_app())


@pytest.fixture
def auth_headers(client, request):
    name = "lat_" + hashlib.md5(request.node.name.encode()).hexdigest()[:12]
    client.post("/api/auth/register", json={"username": name, "password": "test123456"})
    token = client.post(
        "/api/auth/login", json={"username": name, "password": "test123456"}
    ).json()["token"]
    return {"Authorization": f"Bearer {token}"}


def _p95(values):
    ordered = sorted(values)
    return ordered[max(0, int(len(ordered) * 0.95) - 1)]


class TestGateOverhead:
    def test_full_gate_chain_is_fast(self, client, auth_headers):
        """引擎打桩为立即返回，量到的就是闸门自身开销。"""
        engine = MagicMock()
        engine.chat.return_value = ("好", [], None)
        engine.last_provider = "stub"
        engine.model = "stub"
        engine.style_profile = None

        durations = []
        with patch("server.routes._get_engine", return_value=engine):
            with patch("server.routes._run_session_archive"):
                # 预热，排除首次导入与建表开销
                client.post(
                    "/api/chat",
                    json={"slug": "d", "message": "预热"},
                    headers=auth_headers,
                )
                for _ in range(30):
                    start = time.perf_counter()
                    client.post(
                        "/api/chat",
                        json={"slug": "d", "message": "在吗"},
                        headers=auth_headers,
                    )
                    durations.append((time.perf_counter() - start) * 1000)

        p95 = _p95(durations)
        assert p95 < GATE_BUDGET_MS, (
            f"闸门链路 P95 {p95:.0f}ms 超出预算 {GATE_BUDGET_MS}ms"
        )

    def test_crisis_detection_alone_is_fast(self):
        """危机检查在每条消息的必经路径上，PRD 给的是 P95 < 200ms。"""
        from core.safety.crisis import detect_crisis

        samples = [
            "在吗",
            "今天好累",
            "我不想活了",
            "哈哈哈哈",
            "有点撑不下去了",
            "明天见",
            "困死了",
        ]
        durations = []
        for _ in range(200):
            for text in samples:
                start = time.perf_counter()
                detect_crisis(text)
                durations.append((time.perf_counter() - start) * 1000)
        assert _p95(durations) < 200

    def test_moderation_is_fast(self):
        from core.safety.moderation import moderate_input

        durations = []
        for _ in range(300):
            start = time.perf_counter()
            moderate_input("今天天气真好我们出去走走吧")
            durations.append((time.perf_counter() - start) * 1000)
        assert _p95(durations) < 50

    def test_blocked_path_returns_even_faster(self, client, auth_headers):
        """被拦下的请求根本不该走到引擎，只会更快。"""
        with patch("server.routes._get_engine") as get_engine:
            start = time.perf_counter()
            client.post(
                "/api/chat",
                json={"slug": "d", "message": "我不想活了"},
                headers=auth_headers,
            )
            elapsed = (time.perf_counter() - start) * 1000
        get_engine.assert_not_called()
        assert elapsed < GATE_BUDGET_MS
