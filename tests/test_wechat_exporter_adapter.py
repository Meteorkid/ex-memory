"""WechatExporter 外部适配器测试。"""

from pathlib import Path

import pytest


def test_run_wechat_exporter_builds_command(tmp_path, monkeypatch):
    from core.exporters.wechat_adapter import WechatExportOptions, run_wechat_exporter

    binary = tmp_path / "WechatExporter"
    binary.write_text("#!/bin/sh\n", encoding="utf-8")
    binary.chmod(0o755)
    backup = tmp_path / "backup"
    backup.mkdir()
    output = tmp_path / "output"
    calls = {}

    def fake_run(cmd, check, capture_output, text):
        calls["cmd"] = cmd
        calls["check"] = check
        calls["capture_output"] = capture_output
        calls["text"] = text

        class Result:
            stdout = "ok"
            stderr = ""

        return Result()

    monkeypatch.setattr("subprocess.run", fake_run)

    run_wechat_exporter(
        WechatExportOptions(
            backup_dir=backup,
            output_dir=output,
            account="wxid",
            sessions=("张三",),
            binary_path=Path(binary),
            enable_filter=True,
        )
    )

    assert calls["cmd"] == [
        str(binary),
        f"--backup={backup}",
        f"--output={output}",
        "--account=wxid",
        "--asyncloading=onscroll",
        "--filter=yes",
        "--session=张三",
    ]
    assert calls["check"] is True
    assert output.exists()


def test_run_wechat_exporter_requires_config(tmp_path, monkeypatch):
    from core.exporters.wechat_adapter import (
        WechatExporterNotConfigured,
        WechatExportOptions,
        run_wechat_exporter,
    )

    monkeypatch.delenv("WECHAT_EXPORTER_BIN", raising=False)

    with pytest.raises(
        WechatExporterNotConfigured,
        match="git submodule update --init --recursive",
    ):
        run_wechat_exporter(
            WechatExportOptions(
                backup_dir=tmp_path / "backup",
                output_dir=tmp_path / "output",
                account="wxid",
            )
        )


def test_run_wechat_exporter_rejects_missing_binary(tmp_path):
    from core.exporters.wechat_adapter import (
        WechatExporterNotConfigured,
        WechatExportOptions,
        run_wechat_exporter,
    )

    with pytest.raises(WechatExporterNotConfigured, match="二进制不存在"):
        run_wechat_exporter(
            WechatExportOptions(
                backup_dir=tmp_path / "backup",
                output_dir=tmp_path / "output",
                account="wxid",
                binary_path=tmp_path / "missing",
            )
        )


def test_run_wechat_exporter_rejects_non_executable_binary(tmp_path):
    from core.exporters.wechat_adapter import (
        WechatExporterNotConfigured,
        WechatExportOptions,
        run_wechat_exporter,
    )

    binary = tmp_path / "WechatExporter"
    binary.write_text("#!/bin/sh\n", encoding="utf-8")
    binary.chmod(0o644)

    with pytest.raises(WechatExporterNotConfigured, match="不可执行"):
        run_wechat_exporter(
            WechatExportOptions(
                backup_dir=tmp_path / "backup",
                output_dir=tmp_path / "output",
                account="wxid",
                binary_path=binary,
            )
        )


def test_run_wechat_exporter_rejects_unknown_async_loading(tmp_path):
    from core.exporters.wechat_adapter import WechatExportOptions, run_wechat_exporter

    binary = tmp_path / "WechatExporter"
    binary.write_text("#!/bin/sh\n", encoding="utf-8")
    binary.chmod(0o755)
    backup = tmp_path / "backup"
    backup.mkdir()

    with pytest.raises(ValueError, match="asyncloading"):
        run_wechat_exporter(
            WechatExportOptions(
                backup_dir=backup,
                output_dir=tmp_path / "output",
                account="wxid",
                binary_path=binary,
                async_loading="unexpected",
            )
        )
