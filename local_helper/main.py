"""本地助手运行入口。"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

import uvicorn

from local_helper.api import HelperSettings, create_helper_app


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="ex-memory macOS 本地微信导出助手")
    parser.add_argument("--port", type=int, default=17653)
    parser.add_argument("--allowed-origin", action="append")
    parser.add_argument("--capture-launcher", type=Path)
    parser.add_argument("--capture-module", type=Path)
    parser.add_argument("--sqlcipher-binary", type=Path)
    parser.add_argument("--workflow-root", type=Path)
    parser.add_argument("--export-root", type=Path)
    return parser.parse_args(argv)


def configured_origins(cli_origins: list[str] | None) -> frozenset[str]:
    values = list(cli_origins or ())
    values.extend(item for item in os.getenv("EX_MEMORY_ALLOWED_ORIGINS", "").split(",") if item.strip())
    release_file = runtime_resource("release-origin.txt")
    if release_file.is_file() and not release_file.is_symlink():
        values.extend(release_file.read_text(encoding="utf-8").splitlines())
    origins = frozenset(value.strip().rstrip("/") for value in values if value.strip())
    if not origins:
        raise SystemExit("未配置网站 Origin；请使用 --allowed-origin 或重新构建安装包")
    return origins


def runtime_resource(relative: str) -> Path:
    bundle_root = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent.parent))
    primary = bundle_root / relative
    if primary.exists():
        if primary.is_symlink():
            resolved = primary.resolve()
            bundle_contents = Path(sys.executable).resolve().parent.parent
            try:
                resolved.relative_to(bundle_contents)
            except ValueError:
                return primary
            return resolved
        return primary
    bundle_frameworks = Path(sys.executable).resolve().parent.parent / "Frameworks" / relative
    if bundle_frameworks.exists():
        return bundle_frameworks
    return primary


def main() -> None:
    args = parse_args()
    settings_kwargs = {
        "allowed_origins": configured_origins(args.allowed_origin),
        "local_base_url": f"http://127.0.0.1:{args.port}",
        "capture_launcher": args.capture_launcher
        or runtime_resource("local_helper/wechat_macos/lldb_capture_launcher.sh"),
        "capture_module": args.capture_module
        or runtime_resource("local_helper/wechat_macos/lldb_key_capture.py"),
        "sqlcipher_binary": args.sqlcipher_binary or runtime_resource("local_helper/bin/sqlcipher"),
    }
    if args.workflow_root:
        settings_kwargs["workflow_root"] = args.workflow_root
    if args.export_root:
        settings_kwargs["export_root"] = args.export_root
    settings = HelperSettings(
        **settings_kwargs,
    )
    app = create_helper_app(settings)
    uvicorn.run(app, host="127.0.0.1", port=args.port, access_log=False)


if __name__ == "__main__":
    main()
