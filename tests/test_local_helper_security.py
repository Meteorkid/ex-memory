from pathlib import Path
from urllib.parse import urlsplit

from fastapi.testclient import TestClient

from local_helper.api import HelperSettings, create_helper_app
from local_helper.security import OneTimeTicketStore, is_loopback_host, validate_origin
from local_helper.wechat_macos.discovery import WeChatAccount, WeChatEnvironment


SITE_ORIGIN = "https://memory.example.com"


def client() -> TestClient:
    app = create_helper_app(
        HelperSettings(
            allowed_origins=frozenset({SITE_ORIGIN}),
            open_browser_on_launch=False,
        )
    )
    return TestClient(app)


def test_loopback_host_validation():
    assert is_loopback_host("127.0.0.1:17653")
    assert is_loopback_host("localhost:17653")
    assert is_loopback_host("[::1]:17653")
    assert not is_loopback_host("evil.example")
    assert not is_loopback_host("127.0.0.1.evil.example")


def test_origin_requires_exact_match():
    allowed = frozenset({SITE_ORIGIN})
    assert validate_origin(SITE_ORIGIN, allowed)
    assert validate_origin(SITE_ORIGIN + "/", allowed)
    assert not validate_origin("https://evil.example", allowed)
    assert not validate_origin(None, allowed)


def test_control_api_rejects_wrong_host_and_origin():
    helper = client()
    assert helper.get(
        "/v1/control/health",
        headers={"host": "evil.example", "origin": SITE_ORIGIN},
    ).status_code == 421
    assert helper.get(
        "/v1/control/health",
        headers={"origin": "https://evil.example"},
    ).status_code == 403


def test_control_api_cors_and_public_status_are_privacy_safe():
    helper = client()
    headers = {"origin": SITE_ORIGIN}
    health = helper.get("/v1/control/health", headers=headers)
    assert health.status_code == 200
    assert set(health.json()) == {"status", "helper_version", "platform", "architecture", "api_version"}
    assert health.headers["access-control-allow-origin"] == SITE_ORIGIN

    launched = helper.post("/v1/control/launch", headers=headers)
    assert launched.status_code == 200
    assert set(launched.json()) == {"task_id", "launched", "local_url"}
    assert launched.json()["local_url"].startswith("http://127.0.0.1:")

    status = helper.get(f"/v1/control/tasks/{launched.json()['task_id']}", headers=headers)
    assert status.status_code == 200
    assert set(status.json()) == {"task_id", "status", "phase", "progress", "error_code"}


def test_control_api_accepts_private_network_preflight_for_allowed_origin():
    helper = client()

    response = helper.options(
        "/v1/control/health",
        headers={
            "origin": SITE_ORIGIN,
            "access-control-request-method": "GET",
            "access-control-request-private-network": "true",
        },
    )

    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == SITE_ORIGIN
    assert response.headers["access-control-allow-private-network"] == "true"


def test_local_ticket_is_one_time():
    app = create_helper_app(
        HelperSettings(allowed_origins=frozenset({SITE_ORIGIN}), open_browser_on_launch=False)
    )
    helper = TestClient(app)
    task = app.state.tasks.create()
    ticket = app.state.tickets.issue()
    path = f"/local/export?ticket={ticket.token}&task={task.task_id}"
    response = helper.get(path)
    assert response.status_code == 200
    assert response.cookies.get("ex_memory_local_session")
    assert helper.get(path).status_code == 403


def test_local_page_can_be_reopened_with_a_fresh_one_time_ticket_for_the_same_task():
    app = create_helper_app(
        HelperSettings(allowed_origins=frozenset({SITE_ORIGIN}), open_browser_on_launch=False)
    )
    helper = TestClient(app)
    headers = {"origin": SITE_ORIGIN}
    launched = helper.post("/v1/control/launch", headers=headers).json()
    first_url = urlsplit(launched["local_url"])
    first_path = f"{first_url.path}?{first_url.query}"

    assert helper.get(first_path).status_code == 200
    assert helper.get(first_path).status_code == 403

    reopened = helper.post(
        f"/v1/control/tasks/{launched['task_id']}/reopen",
        headers=headers,
    )

    assert reopened.status_code == 200
    assert reopened.json()["task_id"] == launched["task_id"]
    assert reopened.json()["local_url"] != launched["local_url"]
    second_url = urlsplit(reopened.json()["local_url"])
    second_path = f"{second_url.path}?{second_url.query}"
    assert helper.get(second_path).status_code == 200
    assert helper.get(second_path).status_code == 403


def test_reopening_the_local_page_invokes_the_browser_with_a_fresh_url(monkeypatch):
    opened_urls = []
    monkeypatch.setattr(
        "local_helper.api.webbrowser.open",
        lambda url, new: opened_urls.append((url, new)),
    )
    app = create_helper_app(HelperSettings(allowed_origins=frozenset({SITE_ORIGIN})))
    helper = TestClient(app)
    headers = {"origin": SITE_ORIGIN}
    launched = helper.post("/v1/control/launch", headers=headers).json()

    reopened = helper.post(
        f"/v1/control/tasks/{launched['task_id']}/reopen",
        headers=headers,
    )

    assert reopened.status_code == 200
    assert opened_urls == [
        (launched["local_url"], 2),
        (reopened.json()["local_url"], 2),
    ]
    assert opened_urls[0][0] != opened_urls[1][0]


def test_local_export_page_includes_request_failure_permission_steps():
    app = create_helper_app(
        HelperSettings(allowed_origins=frozenset({SITE_ORIGIN}), open_browser_on_launch=False)
    )
    helper = TestClient(app)
    task = app.state.tasks.create()
    ticket = app.state.tickets.issue()

    page = helper.get(f"/local/export?ticket={ticket.token}&task={task.task_id}")

    assert page.status_code == 200
    assert "请求失败处理方法" in page.text
    assert "1. 点击 +" in page.text
    assert "2. 选择 /Applications/ex-memory 微信导出助手.app" in page.text
    assert "3. 打开它右侧的权限开关" in page.text
    assert "4. 如果系统要求，输入 Mac 密码" in page.text
    assert 'id="request-permission-steps" class="hidden"' in page.text


def test_local_export_page_routes_api_failures_to_permission_guidance():
    app = create_helper_app(
        HelperSettings(allowed_origins=frozenset({SITE_ORIGIN}), open_browser_on_launch=False)
    )
    helper = TestClient(app)
    task = app.state.tasks.create()
    ticket = app.state.tickets.issue()

    page = helper.get(f"/local/export?ticket={ticket.token}&task={task.task_id}")

    assert "catch(error){renderRequestFailure(error);throw error}" in page.text
    assert "message.includes('完全磁盘访问')" in page.text


def test_local_export_page_disables_start_when_no_account_is_found():
    app = create_helper_app(
        HelperSettings(allowed_origins=frozenset({SITE_ORIGIN}), open_browser_on_launch=False)
    )
    helper = TestClient(app)
    task = app.state.tasks.create()
    ticket = app.state.tickets.issue()

    page = helper.get(f"/local/export?ticket={ticket.token}&task={task.task_id}")

    assert "else{elements.prepare.disabled=true" in page.text
    assert "请先安装并登录微信 4.1.12" in page.text


def test_local_export_page_auto_selects_the_detected_current_wechat_account():
    app = create_helper_app(
        HelperSettings(allowed_origins=frozenset({SITE_ORIGIN}), open_browser_on_launch=False)
    )
    helper = TestClient(app)
    task = app.state.tasks.create()
    ticket = app.state.tickets.issue()

    page = helper.get(f"/local/export?ticket={ticket.token}&task={task.task_id}")

    assert "environment.current_account_id" in page.text
    assert "当前登录" in page.text
    assert "elements.account.value=environment.current_account_id" in page.text


def test_local_export_page_keeps_account_selection_recoverable_after_validation_errors():
    app = create_helper_app(
        HelperSettings(allowed_origins=frozenset({SITE_ORIGIN}), open_browser_on_launch=False)
    )
    helper = TestClient(app)
    task = app.state.tasks.create()
    ticket = app.state.tickets.issue()

    page = helper.get(f"/local/export?ticket={ticket.token}&task={task.task_id}")

    assert "if(!elements.account.value)" in page.text
    assert "请先选择当前登录的微信账号" in page.text
    assert "if(fatal)" in page.text
    assert "renderRequestFailure(new Error('本地助手没有“完全磁盘访问”权限。'),true)" in page.text


def test_local_export_page_stops_before_sip_steps_for_an_unsupported_wechat_version():
    app = create_helper_app(
        HelperSettings(allowed_origins=frozenset({SITE_ORIGIN}), open_browser_on_launch=False)
    )
    helper = TestClient(app)
    task = app.state.tasks.create()
    ticket = app.state.tickets.issue()

    page = helper.get(f"/local/export?ticket={ticket.token}&task={task.task_id}")

    assert "environment.supported_wechat_versions.includes(environment.wechat_version)" in page.text
    assert "尚未支持，为保护本机数据已停止" in page.text
    assert "不要关闭 SIP" in page.text


def test_local_export_page_uses_project_dark_theme():
    app = create_helper_app(
        HelperSettings(allowed_origins=frozenset({SITE_ORIGIN}), open_browser_on_launch=False)
    )
    helper = TestClient(app)
    task = app.state.tasks.create()
    ticket = app.state.tickets.issue()

    page = helper.get(f"/local/export?ticket={ticket.token}&task={task.task_id}")

    assert "--bg-primary:#0a0a12" in page.text
    assert "--accent:#7c6cff" in page.text
    assert "#export{background:linear-gradient(135deg,#07c160,#06a855);" in page.text
    assert "@media (prefers-reduced-motion:reduce)" in page.text


def test_local_export_page_includes_photographable_sip_recovery_steps():
    app = create_helper_app(
        HelperSettings(allowed_origins=frozenset({SITE_ORIGIN}), open_browser_on_launch=False)
    )
    helper = TestClient(app)
    task = app.state.tasks.create()
    ticket = app.state.tickets.issue()

    page = helper.get(f"/local/export?ticket={ticket.token}&task={task.task_id}")

    assert "请先用手机拍照保存本页步骤" in page.text
    assert "Apple 芯片 Mac" in page.text
    assert "长按电源键，直到出现启动选项" in page.text
    assert "Intel Mac" in page.text
    assert "开机后立即长按 ⌘R" in page.text
    assert "csrutil disable" in page.text
    assert "csrutil enable" in page.text


def test_local_export_page_shows_terminal_results_for_both_sip_commands():
    app = create_helper_app(
        HelperSettings(allowed_origins=frozenset({SITE_ORIGIN}), open_browser_on_launch=False)
    )
    helper = TestClient(app)
    task = app.state.tasks.create()
    ticket = app.state.tickets.issue()

    page = helper.get(f"/local/export?ticket={ticket.token}&task={task.task_id}")

    assert page.text.count('<svg class="terminal-shot"') == 4
    assert "csrutil disable 在 Apple 芯片 Mac 上执行后的完整交互示意图" in page.text
    assert "csrutil enable 在 Apple 芯片 Mac 上执行后的恢复模式终端示意图" in page.text
    assert "Successfully disabled System Integrity Protection." in page.text
    assert "Successfully enabled System Integrity Protection." in page.text
    assert "Enter your username: your_username" in page.text
    assert "输入密码时屏幕不会显示任何字符，这是正常现象。" in page.text
    assert "不同 macOS 版本的提示文字和窗口外观可能略有差异" in page.text


def test_local_export_page_shows_sip_verification_and_troubleshooting():
    app = create_helper_app(
        HelperSettings(allowed_origins=frozenset({SITE_ORIGIN}), open_browser_on_launch=False)
    )
    helper = TestClient(app)
    task = app.state.tasks.create()
    ticket = app.state.tickets.issue()

    page = helper.get(f"/local/export?ticket={ticket.token}&task={task.task_id}")

    assert "csrutil status" in page.text
    assert "Failed to create local policy" in page.text
    assert "csrutil clear" in page.text


def test_local_export_page_shows_extracting_progress_indicator():
    app = create_helper_app(
        HelperSettings(allowed_origins=frozenset({SITE_ORIGIN}), open_browser_on_launch=False)
    )
    helper = TestClient(app)
    task = app.state.tasks.create()
    ticket = app.state.tickets.issue()

    page = helper.get(f"/local/export?ticket={ticket.token}&task={task.task_id}")

    assert "正在捕获并验证该账号全部数据库" in page.text
    assert ".status.working::before" in page.text
    assert "@keyframes status-spin" in page.text


def test_local_export_page_explains_per_account_key_rules():
    app = create_helper_app(
        HelperSettings(allowed_origins=frozenset({SITE_ORIGIN}), open_browser_on_launch=False)
    )
    helper = TestClient(app)
    task = app.state.tasks.create()
    ticket = app.state.tickets.issue()

    page = helper.get(f"/local/export?ticket={ticket.token}&task={task.task_id}")

    assert "每个微信账号的数据库密钥不同" in page.text
    assert "切换账号" in page.text
    assert "其他账号需要分别登录并分别提取" in page.text
    assert 'id="key-rules-confirmed"' in page.text


def test_local_export_page_shows_failed_hint_and_troubleshooting_steps():
    app = create_helper_app(
        HelperSettings(allowed_origins=frozenset({SITE_ORIGIN}), open_browser_on_launch=False)
    )
    helper = TestClient(app)
    task = app.state.tasks.create()
    ticket = app.state.tickets.issue()

    page = helper.get(f"/local/export?ticket={ticket.token}&task={task.task_id}")

    assert 'id="failed-hint"' in page.text
    assert "任务失败" in page.text
    assert "当前已验证的 4.1.12" in page.text
    assert "请先重新开启 SIP 并重启" in page.text


def test_local_environment_requires_private_session():
    app = create_helper_app(
        HelperSettings(allowed_origins=frozenset({SITE_ORIGIN}), open_browser_on_launch=False)
    )
    helper = TestClient(app)
    assert helper.get("/local/api/environment").status_code == 401

    task = app.state.tasks.create()
    ticket = app.state.tickets.issue()
    helper.get(f"/local/export?ticket={ticket.token}&task={task.task_id}")
    environment = helper.get("/local/api/environment")

    assert environment.status_code == 200
    assert set(environment.json()) == {
        "platform",
        "sip_status",
        "wechat_version",
        "supported_wechat_versions",
        "current_account_id",
        "accounts",
        "data_accessible",
        "error_code",
    }


def test_local_environment_explains_full_disk_access_requirement():
    app = create_helper_app(
        HelperSettings(allowed_origins=frozenset({SITE_ORIGIN}), open_browser_on_launch=False)
    )
    app.state.environment_provider = lambda: WeChatEnvironment(
        app_version="4.1.12",
        accounts=(),
        data_root_exists=True,
        data_accessible=False,
        error_code="full_disk_access_required",
    )
    helper = TestClient(app)
    task = app.state.tasks.create()
    ticket = app.state.tickets.issue()
    helper.get(f"/local/export?ticket={ticket.token}&task={task.task_id}")

    response = helper.get("/local/api/environment")

    assert response.status_code == 200
    assert response.json()["error_code"] == "full_disk_access_required"


def test_local_mutation_requires_csrf_token():
    app = create_helper_app(
        HelperSettings(allowed_origins=frozenset({SITE_ORIGIN}), open_browser_on_launch=False)
    )
    helper = TestClient(app)
    task = app.state.tasks.create()
    ticket = app.state.tickets.issue()
    page = helper.get(f"/local/export?ticket={ticket.token}&task={task.task_id}")
    csrf = page.headers["x-ex-memory-csrf"]

    assert helper.post("/local/api/confirm", json={}).status_code == 403
    assert helper.post("/local/api/confirm", headers={"x-ex-memory-csrf": csrf}, json={}).status_code == 200


def test_prepare_requires_explicit_per_account_key_confirmation(tmp_path: Path):
    account_root = tmp_path / "account"
    storage = account_root / "db_storage"
    storage.mkdir(parents=True)
    database = storage / "message.db"
    database.write_bytes(bytes(4096))
    account = WeChatAccount("wxid_account", account_root, storage, (database,), "fingerprint")
    app = create_helper_app(
        HelperSettings(
            allowed_origins=frozenset({SITE_ORIGIN}),
            open_browser_on_launch=False,
            workflow_root=tmp_path / "tasks",
        )
    )
    app.state.environment_provider = lambda: WeChatEnvironment("4.1.12", (account,), True)
    helper = TestClient(app)
    task = app.state.tasks.create()
    ticket = app.state.tickets.issue()
    page = helper.get(f"/local/export?ticket={ticket.token}&task={task.task_id}")
    headers = {"x-ex-memory-csrf": page.headers["x-ex-memory-csrf"]}

    rejected = helper.post(
        "/local/api/expert/prepare",
        headers=headers,
        json={"account_id": account.account_id, "key_rules_confirmed": False},
    )

    assert rejected.status_code == 409
    assert "每个微信账号密钥不同" in rejected.json()["detail"]


def test_prepare_rejects_an_empty_account_selection_with_actionable_guidance(tmp_path: Path):
    app = create_helper_app(
        HelperSettings(
            allowed_origins=frozenset({SITE_ORIGIN}),
            open_browser_on_launch=False,
            workflow_root=tmp_path / "tasks",
        )
    )
    helper = TestClient(app)
    task = app.state.tasks.create()
    ticket = app.state.tickets.issue()
    page = helper.get(f"/local/export?ticket={ticket.token}&task={task.task_id}")

    response = helper.post(
        "/local/api/expert/prepare",
        headers={"x-ex-memory-csrf": page.headers["x-ex-memory-csrf"]},
        json={"account_id": "", "key_rules_confirmed": True},
    )

    assert response.status_code == 400
    assert response.json()["detail"] == "请先选择当前登录的微信账号"


def test_prepare_stops_an_unsupported_wechat_version_before_creating_a_workflow(tmp_path: Path):
    account_root = tmp_path / "account"
    storage = account_root / "db_storage"
    storage.mkdir(parents=True)
    database = storage / "message.db"
    database.write_bytes(bytes(4096))
    account = WeChatAccount("wxid_account", account_root, storage, (database,), "fingerprint")
    app = create_helper_app(
        HelperSettings(
            allowed_origins=frozenset({SITE_ORIGIN}),
            open_browser_on_launch=False,
            workflow_root=tmp_path / "tasks",
        )
    )
    app.state.environment_provider = lambda: WeChatEnvironment("4.1.13", (account,), True)
    helper = TestClient(app)
    task = app.state.tasks.create()
    ticket = app.state.tickets.issue()
    page = helper.get(f"/local/export?ticket={ticket.token}&task={task.task_id}")

    response = helper.post(
        "/local/api/expert/prepare",
        headers={"x-ex-memory-csrf": page.headers["x-ex-memory-csrf"]},
        json={"account_id": account.account_id, "key_rules_confirmed": True},
    )

    assert response.status_code == 409
    assert response.json()["detail"] == "当前微信 4.1.13 尚未支持；此助手仅验证 4.1.12"
    assert not (tmp_path / "tasks").exists()


def test_prepare_explains_when_a_previously_listed_account_directory_changed(tmp_path: Path):
    app = create_helper_app(
        HelperSettings(
            allowed_origins=frozenset({SITE_ORIGIN}),
            open_browser_on_launch=False,
            workflow_root=tmp_path / "tasks",
        )
    )
    app.state.environment_provider = lambda: WeChatEnvironment("4.1.12", (), True)
    helper = TestClient(app)
    task = app.state.tasks.create()
    ticket = app.state.tickets.issue()
    page = helper.get(f"/local/export?ticket={ticket.token}&task={task.task_id}")

    response = helper.post(
        "/local/api/expert/prepare",
        headers={"x-ex-memory-csrf": page.headers["x-ex-memory-csrf"]},
        json={"account_id": "previous-account", "key_rules_confirmed": True},
    )

    assert response.status_code == 409
    assert response.json()["detail"] == "所选微信账号目录已变化，请刷新本地页面后重新选择"


def test_local_user_can_delete_safe_task_data(tmp_path: Path):
    account_root = tmp_path / "account"
    storage = account_root / "db_storage"
    storage.mkdir(parents=True)
    database = storage / "message.db"
    database.write_bytes(bytes(4096))
    account = WeChatAccount("wxid_account", account_root, storage, (database,), "fingerprint")
    app = create_helper_app(
        HelperSettings(
            allowed_origins=frozenset({SITE_ORIGIN}),
            open_browser_on_launch=False,
            workflow_root=tmp_path / "tasks",
        )
    )
    app.state.environment_provider = lambda: WeChatEnvironment("4.1.12", (account,), True)
    helper = TestClient(app)
    task = app.state.tasks.create()
    ticket = app.state.tickets.issue()
    page = helper.get(f"/local/export?ticket={ticket.token}&task={task.task_id}")
    headers = {"x-ex-memory-csrf": page.headers["x-ex-memory-csrf"]}
    prepared = helper.post(
        "/local/api/expert/prepare",
        headers=headers,
        json={"account_id": account.account_id, "key_rules_confirmed": True},
    )
    assert prepared.status_code == 200

    deleted = helper.delete("/local/api/task-data", headers=headers)

    assert deleted.status_code == 200
    assert deleted.json() == {"deleted": True}
    assert not (tmp_path / "tasks" / task.task_id).exists()
    public = helper.get(f"/v1/control/tasks/{task.task_id}", headers={"origin": SITE_ORIGIN})
    assert public.json() == {
        "task_id": task.task_id,
        "status": "cancelled",
        "phase": "deleted",
        "progress": 0,
        "error_code": "",
    }


def test_local_export_page_offers_irreversible_task_cleanup():
    app = create_helper_app(
        HelperSettings(allowed_origins=frozenset({SITE_ORIGIN}), open_browser_on_launch=False)
    )
    helper = TestClient(app)
    task = app.state.tasks.create()
    ticket = app.state.tickets.issue()

    page = helper.get(f"/local/export?ticket={ticket.token}&task={task.task_id}")

    assert 'id="delete-task-data"' in page.text
    assert "全部解密数据库快照" in page.text
    assert "此操作不可恢复" in page.text
    assert "'/local/api/task-data',{method:'DELETE'}" in page.text


def test_ticket_store_rejects_replay():
    store = OneTimeTicketStore(ttl_seconds=10)
    ticket = store.issue()
    assert store.consume(ticket.token)
    assert not store.consume(ticket.token)
