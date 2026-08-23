import hashlib
import json
import sys
from pathlib import Path
from subprocess import run


def test_build_manifest_contains_artifact_integrity(tmp_path: Path):
    artifact = tmp_path / "helper.dmg"
    artifact.write_bytes(b"dmg-bytes")
    output = tmp_path / "manifest.json"
    script = Path(__file__).parents[1] / "packaging" / "macos" / "write_manifest.py"

    result = run(
        [
            sys.executable,
            str(script),
            "--artifact",
            str(artifact),
            "--version",
            "0.1.0-beta.1",
            "--architecture",
            "arm64",
            "--site-origin",
            "https://memory.example.com",
            "--output",
            str(output),
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0
    manifest = json.loads(output.read_text(encoding="utf-8"))
    assert manifest["sha256"] == hashlib.sha256(b"dmg-bytes").hexdigest()
    assert manifest["api_version"] == 1
    assert manifest["supported_wechat_versions"] == ["4.1.12"]
    assert manifest["developer_id_signed"] is False
    assert manifest["notarized"] is False


def test_build_script_requires_arm64_and_system_lldb():
    script = (Path(__file__).parents[1] / "packaging" / "macos" / "build_helper.sh").read_text(
        encoding="utf-8"
    )

    assert '"$(uname -m)" != "arm64"' in script
    assert "/usr/bin/xcrun --find lldb" in script
    assert 'PYTHON_BIN="${PYTHON_BIN:-python3}"' in script
    assert "打包 Python 必须为 3.10 或更高版本" in script
