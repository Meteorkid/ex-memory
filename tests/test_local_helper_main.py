from pathlib import Path

import pytest

from local_helper.main import configured_origins, parse_args, runtime_resource


def test_cli_origin_is_optional_for_packaged_release(monkeypatch, tmp_path: Path):
    monkeypatch.delenv("EX_MEMORY_ALLOWED_ORIGINS", raising=False)
    monkeypatch.setattr("local_helper.main.runtime_resource", lambda _name: tmp_path / "missing")

    assert parse_args([]).allowed_origin is None
    with pytest.raises(SystemExit, match="Origin"):
        configured_origins(None)


def test_dev_capture_component_paths_are_accepted():
    args = parse_args(
        [
            "--capture-launcher",
            "/tmp/lldb_capture_launcher.sh",
            "--capture-module",
            "/tmp/lldb_key_capture.py",
            "--sqlcipher-binary",
            "/opt/homebrew/bin/sqlcipher",
        ]
    )

    assert args.capture_launcher == Path("/tmp/lldb_capture_launcher.sh")
    assert args.capture_module == Path("/tmp/lldb_key_capture.py")
    assert args.sqlcipher_binary == Path("/opt/homebrew/bin/sqlcipher")


def test_release_origin_and_environment_are_normalized(monkeypatch, tmp_path: Path):
    release = tmp_path / "release-origin.txt"
    release.write_text("https://memory.example.com/\n", encoding="utf-8")
    monkeypatch.setenv("EX_MEMORY_ALLOWED_ORIGINS", "https://beta.example.com/")
    monkeypatch.setattr("local_helper.main.runtime_resource", lambda _name: release)

    assert configured_origins(None) == frozenset(
        {"https://memory.example.com", "https://beta.example.com"}
    )


def test_runtime_resource_finds_macos_bundle_frameworks(monkeypatch, tmp_path: Path):
    app_contents = tmp_path / "Helper.app" / "Contents"
    executable = app_contents / "MacOS" / "helper"
    release = app_contents / "Frameworks" / "release-origin.txt"
    executable.parent.mkdir(parents=True)
    release.parent.mkdir(parents=True)
    executable.touch()
    release.write_text("http://127.0.0.1:8000\n", encoding="utf-8")
    monkeypatch.setattr("local_helper.main.sys.executable", str(executable))
    monkeypatch.setattr("local_helper.main.sys._MEIPASS", str(tmp_path / "missing"), raising=False)

    assert runtime_resource("release-origin.txt") == release


def test_runtime_resource_resolves_pyinstaller_bundle_symlink(monkeypatch, tmp_path: Path):
    app_contents = tmp_path / "Helper.app" / "Contents"
    executable = app_contents / "MacOS" / "helper"
    resources = app_contents / "Resources"
    frameworks = app_contents / "Frameworks"
    executable.parent.mkdir(parents=True)
    resources.mkdir(parents=True)
    frameworks.mkdir(parents=True)
    executable.touch()
    release = resources / "release-origin.txt"
    release.write_text("http://127.0.0.1:8000\n", encoding="utf-8")
    (frameworks / "release-origin.txt").symlink_to("../Resources/release-origin.txt")
    monkeypatch.setattr("local_helper.main.sys.executable", str(executable))
    monkeypatch.setattr("local_helper.main.sys._MEIPASS", str(frameworks), raising=False)

    assert runtime_resource("release-origin.txt") == release
