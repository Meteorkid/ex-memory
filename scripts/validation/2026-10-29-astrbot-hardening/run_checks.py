"""在新的源码副本中验证 AstrBot/MCP；禁止读取个人运行数据或访问外网。"""

import argparse
import importlib.util
import os
from pathlib import Path
import runpy
import shutil
import sys

REPO = Path(__file__).resolve().parents[3]


def load_sandbox(repo):
    path = repo / "scripts/validation/2026-10-23-first-run/sandbox.py"
    spec = importlib.util.spec_from_file_location("offline_sandbox", path)
    sandbox = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(sandbox)
    return sandbox


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("mode", choices=["pytest", "script", "sync"])
    args, remainder = parser.parse_known_args()
    workspace = args.workspace.resolve()
    if workspace == REPO or REPO in workspace.parents or workspace in REPO.parents:
        parser.error("工作目录必须在仓库外")
    if workspace.exists():
        parser.error("请使用新的工作目录，保留前次失败证据")

    sandbox = load_sandbox(REPO)
    sandbox.ROOT = workspace
    sandbox.SOURCE = workspace / "source"
    sandbox.RUNTIME = workspace / "runtime"
    sandbox.DIRS = (*sandbox.DIRS, "mcp_server", "integrations", "scripts", "docs")
    sandbox.FILES = (*sandbox.FILES, "requirements-mcp.txt", "requirements.txt")
    sandbox.refresh()
    shutil.copy2(REPO / "web/app.py", sandbox.SOURCE / "web/app.py")
    # 旧 sandbox 从 HEAD 取评测源码；验证当前工作区时覆盖其中已跟踪的源码。
    for target in (sandbox.SOURCE / "evals").rglob("*.py"):
        source = REPO / target.relative_to(sandbox.SOURCE)
        if source.is_file():
            shutil.copy2(source, target)
    print(f"验证副本：{workspace}", flush=True)
    if args.mode == "sync":
        return

    sys.dont_write_bytecode = True
    sandbox.bootstrap()
    # stdio 子进程也必须继承隔离，不可重新读取 Keychain 或个人目录。
    guard_dir = workspace / "guard"
    guard_dir.mkdir()
    guard = """import ipaddress, os, pathlib, socket, sys
original = pathlib.Path(ORIGINAL)
def audit(event, args):
    if event == "open" and args and isinstance(args[0], (str, bytes, os.PathLike)):
        p = pathlib.Path(os.fsdecode(args[0])).resolve()
        dependency = original / ".venv" / "lib"
        mode = args[1] if len(args) > 1 and isinstance(args[1], str) else ""
        dependency_read = dependency in p.parents and not any(c in mode for c in "wax+")
        if (p == original or original in p.parents) and not dependency_read:
            raise RuntimeError("Original repository access blocked in child")
    if event == "subprocess.Popen" and args and "security" in str(args[0]):
        raise RuntimeError("Keychain blocked in child")
    if event in ("socket.connect", "socket.getaddrinfo"):
        address = args[1] if event == "socket.connect" else args[0]
        host = address[0] if isinstance(address, tuple) else address
        if host in (None, "localhost", "testserver") or (event == "socket.connect" and not isinstance(address, tuple)):
            return
        try:
            local = ipaddress.ip_address(host).is_loopback
        except ValueError:
            local = False
        if not local:
            raise RuntimeError("External network blocked in child")
sys.addaudithook(audit)
sys.dont_write_bytecode = True
try:
    import dotenv
    dotenv.load_dotenv = lambda *a, **k: False
except ImportError:
    pass
try:
    import core.keychain
    core.keychain.get_key = lambda *a, **k: None
except ImportError:
    pass
""".replace("ORIGINAL", repr(str(REPO)))
    (guard_dir / "sitecustomize.py").write_text(guard, encoding="utf-8")
    os.environ["PYTHONPATH"] = os.pathsep.join([str(guard_dir), str(sandbox.SOURCE)])
    os.environ["PYTHONDONTWRITEBYTECODE"] = "1"
    if args.mode == "pytest":
        import pytest

        raise SystemExit(pytest.main(["-p", "no:cacheprovider", *remainder]))
    if not remainder:
        parser.error("script 模式需要仓库相对路径")
    script = (sandbox.SOURCE / remainder[0]).resolve()
    if sandbox.SOURCE not in script.parents:
        parser.error("脚本必须位于验证副本内")
    sys.argv = [str(script), *remainder[1:]]
    runpy.run_path(str(script), run_name="__main__")


if __name__ == "__main__":
    main()
