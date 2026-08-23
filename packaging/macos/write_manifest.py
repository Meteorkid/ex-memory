"""生成可随安装包发布的构建清单。"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--artifact", type=Path, required=True)
    parser.add_argument("--version", required=True)
    parser.add_argument("--architecture", required=True)
    parser.add_argument("--site-origin", action="append", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    artifact = args.artifact.resolve(strict=True)
    payload = {
        "format": "ex-memory-local-helper-build-v1",
        "version": args.version,
        "api_version": 1,
        "platform": "macos",
        "architecture": args.architecture,
        "supported_wechat_versions": ["4.1.12"],
        "site_origin": args.site_origin[0],
        "site_origins": args.site_origin,
        "artifact": artifact.name,
        "size": artifact.stat().st_size,
        "sha256": _sha256(artifact),
        "release_channel": "unsigned-open-source-beta",
        "developer_id_signed": False,
        "notarized": False,
        "built_at": datetime.now(timezone.utc).isoformat(),
    }
    temporary = args.output.with_suffix(args.output.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temporary, args.output)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


if __name__ == "__main__":
    main()
