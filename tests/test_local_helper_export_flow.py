import hashlib
import sqlite3
import time
from pathlib import Path

from fastapi.testclient import TestClient

from local_helper.api import HelperSettings, create_helper_app
from local_helper.wechat_macos.discovery import WeChatAccount, WeChatEnvironment
from local_helper.wechat_macos.sip import SIPStatus


SITE_ORIGIN = "https://memory.example.com"


def _sqlite(path: Path, statements: tuple[str, ...]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as connection:
        for statement in statements:
            connection.execute(statement)
    return path


def test_private_flow_lists_session_and_exports_html(tmp_path: Path):
    account_root = tmp_path / "account"
    storage = account_root / "db_storage"
    source = _sqlite(storage / "source.db", ("CREATE TABLE source(id INTEGER)",))
    account = WeChatAccount("wxid_owner", account_root, storage, (source,), "fingerprint")
    environment = WeChatEnvironment("4.1.12", (account,), True)
    status = [SIPStatus.ENABLED]
    app = create_helper_app(
        HelperSettings(
            allowed_origins=frozenset({SITE_ORIGIN}),
            open_browser_on_launch=False,
            workflow_root=tmp_path / "tasks",
            export_root=tmp_path / "exports",
        )
    )
    app.state.environment_provider = lambda: environment
    app.state.sip_status_provider = lambda: status[-1]
    client = TestClient(app)
    launch = client.post("/v1/control/launch", headers={"origin": SITE_ORIGIN}).json()
    ticket = app.state.tickets.issue()
    page = client.get(f"/local/export?ticket={ticket.token}&task={launch['task_id']}")
    csrf = page.headers["x-ex-memory-csrf"]
    headers = {"x-ex-memory-csrf": csrf}

    prepared = client.post(
        "/local/api/expert/prepare",
        headers=headers,
        json={"account_id": account.account_id, "key_rules_confirmed": True},
    )
    assert prepared.status_code == 200

    status.append(SIPStatus.DISABLED)
    workflow = app.state.workflow
    session_wxid = "wxid_friend"
    table = "Msg_" + hashlib.md5(session_wxid.encode(), usedforsecurity=False).hexdigest()

    def make_plain_databases(_keys, task_dir):
        contact = _sqlite(
            task_dir / "plain" / "contact.db",
            (
                "CREATE TABLE contact(username TEXT, nick_name TEXT, remark TEXT, local_type INTEGER)",
                "INSERT INTO contact VALUES ('wxid_friend', '昵称', '殆红尘一万次以上散尽NEP', 1)",
            ),
        )
        session = _sqlite(
            task_dir / "plain" / "session.db",
            (
                "CREATE TABLE SessionTable(username TEXT, last_timestamp INTEGER, nMsgCount INTEGER)",
                "INSERT INTO SessionTable VALUES ('wxid_friend', 123, 1)",
            ),
        )
        message = _sqlite(
            task_dir / "plain" / "message.db",
            (
                f"CREATE TABLE {table}(local_id INTEGER, local_type INTEGER, create_time INTEGER, message_content TEXT)",
                f"INSERT INTO {table} VALUES (1, 1, 123, '完整聊天内容')",
            ),
        )
        return contact, session, message

    workflow.decrypt_while_sip_disabled(
        task_id=launch["task_id"],
        extract_keys=lambda: ("memory-only",),
        wait_for_wechat_exit=lambda: True,
        snapshot_and_decrypt=make_plain_databases,
    )
    status.append(SIPStatus.ENABLED)
    assert client.post("/local/api/expert/authorize-export", headers=headers, json={}).status_code == 200

    sessions = client.get("/local/api/sessions").json()["sessions"]
    assert sessions[0]["display_name"] == "殆红尘一万次以上散尽NEP"
    started = client.post(
        "/local/api/export",
        headers=headers,
        json={"session_wxid": session_wxid},
    )
    assert started.status_code == 200

    task = None
    for _ in range(50):
        task = client.get("/local/api/task").json()
        if task["phase"] in {"complete", "partial", "failed"}:
            break
        time.sleep(0.02)

    assert task["phase"] == "complete"
    output = Path(task["output_dir"])
    html = next(output.glob("*.html")).read_text(encoding="utf-8")
    assert "完整聊天内容" in html
    assert (output / "export-manifest.json").is_file()
