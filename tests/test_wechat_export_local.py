"""本机微信导出向导测试。"""

import plistlib
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

    monkeypatch.setattr("config.LOCAL_WECHAT_EXPORT_ENABLED", True)
    monkeypatch.setattr("config.WECHAT_EXPORT_BACKUP_ROOT", tmp_path / "MobileSync" / "Backup")
    monkeypatch.setattr("config.WECHAT_EXPORT_OUTPUT_DIR", tmp_path / "wechat_exports")

    binary = tmp_path / "WechatExporter"
    binary.write_text("#!/bin/sh\n", encoding="utf-8")
    binary.chmod(0o755)
    monkeypatch.setenv("WECHAT_EXPORTER_BIN", str(binary))

    import server.routes as routes
    monkeypatch.setattr(routes, "_login_limiter", None)

    from server.app import create_app
    return TestClient(create_app())


def _register_and_login(client):
    client.post("/api/auth/register", json={"username": "u1", "password": "pass1234"})
    r = client.post("/api/auth/login", json={"username": "u1", "password": "pass1234"})
    return r.json()["token"]


def _make_backup(root, backup_id="backup-1"):
    backup = root / backup_id
    backup.mkdir(parents=True)
    with open(backup / "Info.plist", "wb") as f:
        plistlib.dump({
            "Device Name": "Meteor iPhone",
            "Product Type": "iPhone16,2",
            "Product Version": "17.5",
        }, f)
    return backup


def test_wechat_export_status_requires_auth(client):
    assert client.get("/api/wechat-export/status").status_code == 401


def test_wechat_export_backups_respects_local_switch(client, monkeypatch):
    token = _register_and_login(client)
    headers = {"Authorization": f"Bearer {token}"}
    monkeypatch.setattr("config.LOCAL_WECHAT_EXPORT_ENABLED", False)

    r = client.get("/api/wechat-export/backups", headers=headers)

    assert r.status_code == 403


def test_wechat_export_lists_backups(client):
    import config

    token = _register_and_login(client)
    headers = {"Authorization": f"Bearer {token}"}
    _make_backup(config.WECHAT_EXPORT_BACKUP_ROOT)

    r = client.get("/api/wechat-export/backups", headers=headers)

    assert r.status_code == 200
    backups = r.json()["backups"]
    assert backups[0]["id"] == "backup-1"
    assert backups[0]["device_name"] == "Meteor iPhone"


def test_wechat_export_task_runs_and_downloads_output(client, monkeypatch):
    import config
    from core.exporters.wechat_adapter import WechatExportOptions

    token = _register_and_login(client)
    headers = {"Authorization": f"Bearer {token}"}
    _make_backup(config.WECHAT_EXPORT_BACKUP_ROOT)

    def fake_run(options: WechatExportOptions):
        (options.output_dir / "index.html").write_text("<html>ok</html>", encoding="utf-8")

        class Result:
            stdout = "ok"
            stderr = ""

        return Result()

    monkeypatch.setattr("core.exporters.wechat_adapter.run_wechat_exporter", fake_run)

    r = client.post(
        "/api/wechat-export/tasks",
        headers=headers,
        json={
            "backup_id": "backup-1",
            "account": "wxid_xxx",
            "sessions": ["张三"],
            "async_loading": "onscroll",
            "enable_filter": True,
        },
    )
    assert r.status_code == 200
    task_id = r.json()["task_id"]

    task = None
    for _ in range(20):
        task = client.get(f"/api/wechat-export/tasks/{task_id}", headers=headers).json()
        if task["status"] == "success":
            break
        time.sleep(0.05)

    assert task["status"] == "success"
    assert task["output_files"][0]["path"] == "index.html"

    download = client.get(
        f"/api/wechat-export/tasks/{task_id}/files/index.html",
        headers=headers,
    )
    assert download.status_code == 200
    assert "ok" in download.text

    escaped = client.get(
        f"/api/wechat-export/tasks/{task_id}/files/../task.json",
        headers=headers,
    )
    assert escaped.status_code == 404
