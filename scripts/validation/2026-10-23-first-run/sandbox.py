"""Task-owned offline Ex-Memory sandbox; never loads personal runtime files."""

from __future__ import annotations
import argparse
import ipaddress
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys

ORIGINAL = Path(__file__).resolve().parents[3]
ROOT: Path
SOURCE: Path
RUNTIME: Path
DIRS = (
    "core",
    "server",
    "pipeline",
    "memory",
    "parsers",
    "tests",
    "prompts",
    "migrations",
    "resources",
    "templates",
    "commands",
    "local_helper",
    "packaging",
)
FILES = (
    "config.py",
    "run.py",
    "pyproject.toml",
    "pytest.ini",
    "setup.cfg",
    "Makefile",
    ".eslintrc.json",
    "package.json",
    "README.md",
    "AGENTS.md",
)


def refresh():
    marker = ROOT / ".ex-memory-validation-workspace"
    if ROOT.exists() and any(ROOT.iterdir()) and not marker.is_file():
        raise ValueError("临时目录非空且不属于本验证脚本，请使用新的空目录")
    ROOT.mkdir(parents=True, exist_ok=True)
    marker.write_text(str(ORIGINAL), encoding="utf-8")
    SOURCE.mkdir(exist_ok=True)
    RUNTIME.mkdir(exist_ok=True)
    for name in DIRS:
        src = ORIGINAL / name
        if src.exists():
            shutil.copytree(
                src,
                SOURCE / name,
                dirs_exist_ok=True,
                ignore=shutil.ignore_patterns("__pycache__", ".DS_Store", "*.pyc"),
            )
    for name in ("crisis_resources.zh-CN.json", "moderation_wordlist.zh-CN.json"):
        src = ORIGINAL / "content" / name
        if src.is_file():
            (SOURCE / "content").mkdir(exist_ok=True)
            shutil.copy2(src, SOURCE / "content" / name)
    for name in FILES:
        if (ORIGINAL / name).is_file():
            shutil.copy2(ORIGINAL / name, SOURCE / name)
    shutil.copytree(
        ORIGINAL / "web" / "static",
        SOURCE / "web" / "static",
        dirs_exist_ok=True,
        ignore=shutil.ignore_patterns("custom", ".DS_Store", "__pycache__"),
    )
    paths = subprocess.check_output(
        ["git", "-C", str(ORIGINAL), "ls-tree", "-r", "--name-only", "HEAD", "evals"],
        text=True,
    ).splitlines()
    for name in paths:
        if not (
            name.endswith(".py")
            or name in ("evals/data/corpus.jsonl", "evals/data/golden.jsonl")
        ):
            continue
        target = SOURCE / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(
            subprocess.check_output(
                ["git", "-C", str(ORIGINAL), "show", "HEAD:" + name]
            )
        )


def bootstrap():
    safe = {
        k: v
        for k, v in os.environ.items()
        if k in {"PATH", "LANG", "LC_ALL", "TERM", "TMPDIR"}
    }
    os.environ.clear()
    os.environ.update(safe)
    os.environ.update(
        {
            "EX_MEMORY_TEST": "1",
            "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
            "DATABASE_URL": "",
            "REDIS_URL": "",
            "OTLP_ENDPOINT": "",
            "BLOB_BUCKET": "",
            "SINGLE_USER_MODE": "false",
            "LLM_PROVIDERS": "",
            "ANONYMIZED_TELEMETRY": "False",
            "POSTHOG_DISABLED": "1",
            "REQUIRE_PHONE_VERIFICATION": "false",
            "REQUIRE_AGE_CONFIRMATION": "false",
            "LLM_API_KEY": "",
            "EMBEDDING_API_KEY": "",
            "LLM_BASE_URL": "http://127.0.0.1:9/v1",
            "EMBEDDING_BASE_URL": "http://127.0.0.1:9/v1",
        }
    )
    forbidden = [
        ORIGINAL / part
        for part in (".env", "data", "exes", "logs", "web/static/stickers/custom")
    ]

    def protect_private_files(event, args):
        if event == "open" and args and isinstance(args[0], (str, bytes, os.PathLike)):
            name = Path(os.fsdecode(args[0])).absolute()
            if any(name == root or root in name.parents for root in forbidden):
                raise RuntimeError("Private source path blocked by task sandbox")
            if (
                len(args) > 1
                and isinstance(args[1], str)
                and any(flag in args[1] for flag in "wax+")
                and (name == ORIGINAL or ORIGINAL in name.parents)
            ):
                raise RuntimeError("Original repository writes blocked by task sandbox")
        if event == "subprocess.Popen" and args and "security" in str(args[0]):
            raise RuntimeError("Keychain process blocked by task sandbox")

    sys.addaudithook(protect_private_files)
    os.chdir(SOURCE)
    sys.path.insert(0, str(SOURCE))
    import dotenv

    dotenv.load_dotenv = lambda *args, **kwargs: False
    import core.keychain

    core.keychain.get_key = lambda *args, **kwargs: None

    def no_secret_mutations(*args, **kwargs):
        raise RuntimeError("Keychain unavailable in offline sandbox")

    core.keychain.set_key = no_secret_mutations
    core.keychain.delete_key = no_secret_mutations
    real_connect = socket.socket.connect
    real_connect_ex = socket.socket.connect_ex
    real_getaddrinfo = socket.getaddrinfo

    def local_host(host):
        if host in ("localhost", "testserver", None):
            return True
        try:
            return ipaddress.ip_address(host).is_loopback
        except ValueError:
            return False

    def connect(sock, address):
        if isinstance(address, tuple) and not local_host(address[0]):
            raise RuntimeError("External network blocked by task sandbox")
        return real_connect(sock, address)

    def connect_ex(sock, address):
        if isinstance(address, tuple) and not local_host(address[0]):
            raise RuntimeError("External network blocked by task sandbox")
        return real_connect_ex(sock, address)

    def getaddrinfo(host, *args, **kwargs):
        if not local_host(host):
            raise RuntimeError("External DNS blocked by task sandbox")
        return real_getaddrinfo(host, *args, **kwargs)

    socket.socket.connect = connect
    socket.socket.connect_ex = connect_ex
    socket.getaddrinfo = getaddrinfo
    import config

    config.PROJECT_DIR = SOURCE
    config.EXES_DIR = RUNTIME / "exes"
    config.LOG_DIR = RUNTIME / "logs"
    config.DATABASE_URL = ""
    config.REDIS_URL = ""
    config.OTLP_ENDPOINT = ""
    config.BLOB_BUCKET = ""
    import server.auth

    server.auth.DB_DIR = RUNTIME / "data"
    server.auth.DB_PATH = server.auth.DB_DIR / "users.db"
    server.auth.DB_DIR.mkdir(parents=True, exist_ok=True)
    # 直接测试引擎时不会经过 FastAPI 启动；空沙箱也需具备相同的基础表。
    server.auth.init_db()
    import core.sticker_manager

    core.sticker_manager.CUSTOM_BASE = RUNTIME / "stickers"
    return config


def main():
    global ORIGINAL, ROOT, SOURCE, RUNTIME
    parser = argparse.ArgumentParser(description="隔离源码、模型和数据的本机首用验证")
    parser.add_argument(
        "--repo", type=Path, default=ORIGINAL, help="ex-memory 仓库根目录"
    )
    parser.add_argument(
        "--workspace", type=Path, required=True, help="仓库外的任务专用临时目录"
    )
    parser.add_argument("mode", choices=["sync", "pytest", "serve"])
    parser.add_argument("--port", type=int, default=8765)
    args, remainder = parser.parse_known_args()
    ORIGINAL = args.repo.resolve()
    ROOT = args.workspace.resolve()
    if ROOT == ORIGINAL or ORIGINAL in ROOT.parents or ROOT in ORIGINAL.parents:
        parser.error("--workspace 必须与仓库目录分离，不能是仓库本身、子目录或父目录")
    if not (ORIGINAL / "config.py").is_file():
        parser.error("--repo 必须指向 ex-memory 仓库")
    SOURCE, RUNTIME = ROOT / "source", ROOT / "runtime"
    if args.mode != "sync" and not (ROOT / ".ex-memory-validation-workspace").is_file():
        parser.error("请先对这个 --workspace 运行 sync")
    if args.mode == "sync":
        refresh()
        print(
            json.dumps(
                {
                    "source": str(SOURCE),
                    "runtime": str(RUNTIME),
                    "private_inputs_copied": False,
                }
            )
        )
        return
    config = bootstrap()
    if args.mode == "pytest":
        import pytest

        raise SystemExit(pytest.main(remainder))
    from fixture_server import run

    run(config, args.port, ROOT)


if __name__ == "__main__":
    main()
