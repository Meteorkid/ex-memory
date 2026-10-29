"""真实 MCP SDK 子进程验证：连接上下文必须由同一个 asyncio 任务创建和关闭。"""

import asyncio
import os
from pathlib import Path
import sys

import pytest

from tests.test_astrbot_plugin import make_plugin, plugin_module as plugin_module

pytest.importorskip("mcp")


def write_echo_server(path: Path):
    path.mkdir()
    (path / "mcp_server.py").write_text(
        "import os\nfrom mcp.server.fastmcp import FastMCP\n"
        'server = FastMCP("synthetic-lifecycle")\n'
        "@server.tool()\ndef echo() -> dict:\n"
        '    return {"pid": os.getpid(), "token": os.environ["EXMEMORY_MCP_TOKEN"]}\n'
        'server.run(transport="stdio")\n',
        encoding="utf-8",
    )


def process_exists(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


@pytest.mark.parametrize("changed_setting", ["exmemory_token", "exmemory_dir"])
def test_cross_task_reconnect_preserves_polling_and_reaps_processes(
    plugin_module, tmp_path, changed_setting
):
    first_project = tmp_path / "first"
    second_project = tmp_path / "second"
    write_echo_server(first_project)
    write_echo_server(second_project)

    async def scenario():
        plugin, _ = make_plugin(
            plugin_module,
            exmemory_dir=str(first_project),
            exmemory_python=sys.executable,
            exmemory_token="synthetic-one",
        )
        first_round = asyncio.Event()
        continue_polling = asyncio.Event()
        next_round = asyncio.Event()
        responses = []

        async def proactive_round():
            responses.append(await plugin._call("echo", {}))
            if len(responses) == 1:
                first_round.set()
                await continue_polling.wait()
            else:
                next_round.set()

        plugin._proactive_round = proactive_round
        plugin._interval_seconds = lambda: 0.01
        process_ids = []
        owners = []
        try:
            await plugin.initialize()
            await asyncio.wait_for(first_round.wait(), 10)
            process_ids.append(responses[0]["pid"])
            owners.append(getattr(plugin, "_connection_owner", None))
            plugin.conf[changed_setting] = (
                "synthetic-two"
                if changed_setting == "exmemory_token"
                else str(second_project)
            )
            event_call = asyncio.create_task(plugin._call("echo", {}))
            response = await asyncio.wait_for(event_call, 10)
            process_ids.append(response["pid"])
            owners.append(getattr(plugin, "_connection_owner", None))
            assert not plugin._proactive_task.done(), "配置重连不应取消主动轮询任务"
            assert response["pid"] != responses[0]["pid"]
            if changed_setting == "exmemory_token":
                assert response["token"] == "synthetic-two"
            assert not process_exists(process_ids[0])
            continue_polling.set()
            await asyncio.wait_for(next_round.wait(), 10)
            assert responses[1]["pid"] == response["pid"]
        finally:
            continue_polling.set()
            await plugin.terminate()
        assert all(owner is not None and owner.done() for owner in owners)
        assert all(not process_exists(pid) for pid in process_ids)
        assert plugin._proactive_task is None
        assert plugin._session is None

    asyncio.run(asyncio.wait_for(scenario(), 30))


def test_failed_child_start_releases_owner_and_allows_retry(plugin_module, tmp_path):
    project = tmp_path / "server"
    write_echo_server(project)

    async def scenario():
        plugin, _ = make_plugin(
            plugin_module,
            exmemory_dir=str(project),
            exmemory_python=str(tmp_path / "missing-python"),
        )
        with pytest.raises(FileNotFoundError):
            await plugin._ensure_session()
        assert plugin._connection_owner is None
        assert plugin._session is None
        plugin.conf["exmemory_python"] = sys.executable
        try:
            response = await plugin._call("echo", {})
        finally:
            await plugin.terminate()
        assert not process_exists(response["pid"])

    asyncio.run(asyncio.wait_for(scenario(), 15))


def test_cancelled_startup_closes_child_from_owner_task(plugin_module, tmp_path):
    project = tmp_path / "slow-start"
    write_echo_server(project)
    server_file = project / "mcp_server.py"
    server_file.write_text(
        "import os, pathlib, time\n"
        "pathlib.Path('pid').write_text(str(os.getpid()))\n"
        "time.sleep(0.2)\n" + server_file.read_text(encoding="utf-8"),
        encoding="utf-8",
    )

    async def scenario():
        plugin, _ = make_plugin(
            plugin_module, exmemory_dir=str(project), exmemory_python=sys.executable
        )
        startup = asyncio.create_task(plugin._ensure_session())
        try:
            while not (project / "pid").exists():
                await asyncio.sleep(0.01)
            pid = int((project / "pid").read_text())
            owner = plugin._connection_owner
            startup.cancel()
            with pytest.raises(asyncio.CancelledError):
                await startup
            assert owner.done()
            assert plugin._connection_owner is None
            assert not process_exists(pid)
        finally:
            await plugin.terminate()

    asyncio.run(asyncio.wait_for(scenario(), 15))
