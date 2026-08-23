import plistlib
from pathlib import Path

from local_helper.wechat_macos.discovery import detect_environment, discover_accounts


def test_discover_accounts_only_includes_real_db_storage(tmp_path: Path):
    data_root = tmp_path / "xwechat_files"
    db_storage = data_root / "wxid_alpha_abcd" / "db_storage" / "message"
    db_storage.mkdir(parents=True)
    (db_storage / "message_0.db").write_bytes(b"0123456789abcdefpayload")
    (data_root / "not-an-account").mkdir()

    accounts = discover_accounts(data_root)

    assert len(accounts) == 1
    assert accounts[0].account_id == "wxid_alpha_abcd"
    assert accounts[0].databases[0].name == "message_0.db"
    assert len(accounts[0].schema_fingerprint) == 64


def test_discovery_ignores_symlinked_account(tmp_path: Path):
    data_root = tmp_path / "xwechat_files"
    external = tmp_path / "external" / "db_storage"
    external.mkdir(parents=True)
    (external / "message.db").write_bytes(b"0123456789abcdef")
    data_root.mkdir()
    (data_root / "linked-account").symlink_to(external.parent, target_is_directory=True)

    assert discover_accounts(data_root) == ()


def test_detect_environment_reads_app_version(tmp_path: Path):
    app_path = tmp_path / "WeChat.app"
    plist = app_path / "Contents" / "Info.plist"
    plist.parent.mkdir(parents=True)
    with plist.open("wb") as stream:
        plistlib.dump({"CFBundleShortVersionString": "4.1.12"}, stream)

    environment = detect_environment(app_path=app_path, data_root=tmp_path / "missing")

    assert environment.app_version == "4.1.12"
    assert not environment.data_root_exists
    assert environment.accounts == ()


def test_detect_environment_reports_full_disk_access_denied(tmp_path: Path, monkeypatch):
    data_root = tmp_path / "xwechat_files"
    data_root.mkdir()
    original_iterdir = Path.iterdir

    def denied_iterdir(path):
        if path == data_root:
            raise PermissionError("Operation not permitted")
        return original_iterdir(path)

    monkeypatch.setattr(Path, "iterdir", denied_iterdir)

    environment = detect_environment(app_path=tmp_path / "WeChat.app", data_root=data_root)

    assert environment.accounts == ()
    assert environment.data_accessible is False
    assert environment.error_code == "full_disk_access_required"
