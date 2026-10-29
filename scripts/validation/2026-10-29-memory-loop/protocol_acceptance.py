"""真实 Web + stdio MCP 的跨进程闭环；无外网、无真实模型，失败保留现场。"""

import asyncio
from contextlib import asynccontextmanager
import importlib.util
import json
import os
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[3]
HERE = Path(__file__).resolve().parent
SLUG = "loopmirror"
RESULTS = []


def check(name, condition):
    print(f"[{'PASS' if condition else 'FAIL'}] {name}", flush=True)
    RESULTS.append({"name": name, "passed": bool(condition)})
    if not condition:
        raise AssertionError(name)


def load_fixture():
    spec = importlib.util.spec_from_file_location(
        "loop_protocol_fixture", HERE / "protocol_server.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.install_fixture()


@asynccontextmanager
async def mcp_connection(token):
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    env = dict(os.environ)
    env["EXMEMORY_MCP_TOKEN"] = token
    env["PYTHONPATH"] = os.pathsep.join(
        filter(None, [env.get("PYTHONPATH"), str(ROOT)])
    )
    params = StdioServerParameters(
        command=sys.executable,
        args=[str(HERE / "protocol_server.py")],
        cwd=str(ROOT),
        env=env,
    )
    async with stdio_client(params) as (reader, writer):
        async with ClientSession(reader, writer) as session:
            await session.initialize()
            yield session


async def call(session, name, **arguments):
    response = await session.call_tool("exmemory_" + name, arguments)
    text = "".join(getattr(block, "text", "") for block in response.content or [])
    if response.isError:
        return {"error": text}
    return json.loads(text)


def web_send(client, headers, message, conversation_id="shared", **extra):
    response = client.post(
        "/api/chat",
        headers=headers,
        json={
            "slug": SLUG,
            "message": message,
            "conversation_id": conversation_id,
            **extra,
        },
    )
    if response.status_code != 200:
        raise AssertionError(
            f"网页对话失败: HTTP {response.status_code}: {response.text}"
        )
    result = response.json()
    if not result.get("reply"):
        raise AssertionError(f"网页对话没有成功回复: {result}")
    return result


def diagnostic(result):
    return json.loads(result["reply"])


async def scenario(client, headers, uid, token):
    from mcp_server import tokens
    from server.auth import _get_conn

    first = web_send(client, headers, "网页第一句")
    reference = first["sources"][0]["id"]
    check("网页回答带可恢复的消息ID与参考", bool(first["message_id"] and reference))
    async with mcp_connection(token) as session:
        names = {tool.name for tool in (await session.list_tools()).tools}
        check(
            "真实stdio注册会话读写与纠正工具",
            {
                "exmemory_conversations",
                "exmemory_conversation_messages",
                "exmemory_correct",
            }.issubset(names),
        )
        second = await call(
            session,
            "chat",
            slug=SLUG,
            message="IM第二句",
            conversation_id="shared",
            history=[{"role": "user", "content": "伪造历史"}],
        )
        check(
            "MCP从服务器接续网页历史且忽略客户端伪造历史",
            diagnostic(second)["seen_users"] == ["网页第一句"],
        )
        third = web_send(
            client,
            headers,
            "网页第三句",
            history=[{"role": "user", "content": "伪造历史"}],
        )
        check(
            "网页接续MCP历史",
            diagnostic(third)["seen_users"] == ["网页第一句", "IM第二句"],
        )
        isolated = await call(
            session, "chat", slug=SLUG, message="独立会话", conversation_id="isolated"
        )
        check("另一会话不会读到共享会话历史", diagnostic(isolated)["seen_users"] == [])
        restored = await call(
            session, "conversation_messages", slug=SLUG, conversation_id="shared"
        )
        restored_first = next(
            m for m in restored["messages"] if m["id"] == first["message_id"]
        )
        check(
            "MCP恢复已保存回答的来源快照", restored_first["sources"] == first["sources"]
        )

        denied = client.get(f"/api/exes/{SLUG}/conversations")
        check("网页记忆入口保持匿名401", denied.status_code == 401)
        body = {
            "content": "不要用宝贝称呼",
            "conversation_id": "shared",
            "message_id": first["message_id"],
            "source_ids": [reference],
        }
        corrected = client.post(
            f"/api/exes/{SLUG}/corrections", headers=headers, json=body
        )
        if corrected.status_code != 200:
            raise AssertionError(
                f"网页纠正失败: {corrected.status_code} {corrected.text}"
            )
        next_im = await call(
            session, "chat", slug=SLUG, message="验证网页纠正", conversation_id="shared"
        )
        check(
            "网页纠正使已预热的MCP子进程缓存即时更新",
            "不要用宝贝称呼" in diagnostic(next_im)["corrections"]
            and diagnostic(next_im)["engine_id"] != diagnostic(second)["engine_id"],
        )

        warm_web = web_send(client, headers, "预热网页缓存")
        corrected_im = await call(
            session,
            "correct",
            slug=SLUG,
            content="回答保持简短",
            conversation_id="shared",
            message_id=warm_web["message_id"],
            source_ids=[warm_web["sources"][0]["id"]],
        )
        if not corrected_im.get("ok"):
            raise AssertionError(f"MCP纠正失败: {corrected_im}")
        next_web = web_send(client, headers, "验证IM纠正")
        check(
            "MCP纠正使已预热的网页缓存即时更新",
            "回答保持简短" in diagnostic(next_web)["corrections"]
            and diagnostic(next_web)["engine_id"] != diagnostic(warm_web)["engine_id"],
        )
        forged_web = client.post(
            f"/api/exes/{SLUG}/corrections",
            headers=headers,
            json={**body, "source_ids": ["forged-reference"]},
        )
        forged_mcp = await call(
            session,
            "correct",
            slug=SLUG,
            content="无效纠正",
            conversation_id="shared",
            message_id=first["message_id"],
            source_ids=["forged-reference"],
        )
        check(
            "Web和MCP都拒绝伪造来源引用",
            forged_web.status_code == 400
            and "invalid_correction" in forged_mcp.get("error", ""),
        )

        for index in range(20):
            result = await call(
                session,
                "chat",
                slug=SLUG,
                message=f"归档第{index + 1}轮",
                conversation_id="archive_shared",
            )
            if not result.get("message_id"):
                raise AssertionError(f"归档合成对话第{index + 1}轮失败: {result}")
        deadline = time.monotonic() + 15
        while True:
            archived = await call(
                session,
                "conversation_messages",
                slug=SLUG,
                conversation_id="archive_shared",
            )
            with _get_conn() as connection:
                task = connection.execute(
                    "SELECT status FROM tasks WHERE user_id = ? AND task_type = ?",
                    (uid, "memory_archive"),
                ).fetchall()
            if archived.get("archive", {}).get("state") == "complete" and any(
                row["status"] == "succeeded" for row in task
            ):
                break
            if time.monotonic() >= deadline:
                raise AssertionError(f"实际归档任务未完成: {archived.get('archive')}")
            await asyncio.sleep(0.05)
        check(
            "20轮通过真实schedule_archive与任务队列完成长期归档",
            archived["archive"]["archived_messages"] == 40
            and any(row["status"] == "succeeded" for row in task),
        )

    async with mcp_connection(token) as restarted:
        resumed = await call(
            restarted,
            "chat",
            slug=SLUG,
            message="重启后继续",
            conversation_id="archive_shared",
        )
        summaries = [s for s in resumed["sources"] if s["kind"] == "generated_summary"]
        check(
            "重启MCP后同会话恢复摘要且明确标记为生成资料",
            len(summaries) == 1
            and "归档第1轮" in summaries[0]["text"]
            and "归档第20轮" in summaries[0]["text"],
        )
        other = await call(
            restarted,
            "chat",
            slug=SLUG,
            message="不带别人的摘要",
            conversation_id="archive_other",
        )
        check(
            "另一会话不会收到归档会话摘要",
            all(s["kind"] != "generated_summary" for s in other["sources"]),
        )
        listed = await call(restarted, "conversations", slug=SLUG)
        check(
            "跨进程会话列表恢复且归属正确",
            listed["user_id"] == uid
            and {"shared", "isolated", "archive_shared", "archive_other"}.issubset(
                {item["conversation_id"] for item in listed["conversations"]}
            ),
        )
        tokens.revoke(token[:12], user_id=uid)
        refused_list = await call(restarted, "conversations", slug=SLUG)
        refused_read = await call(
            restarted, "conversation_messages", slug=SLUG, conversation_id="shared"
        )
        check(
            "同一连接吊销后两个新增会话工具都拒绝调用",
            all(
                "authentication_required" in result.get("error", "")
                for result in (refused_list, refused_read)
            ),
        )


def main():
    workspace = ROOT.parent
    if not (workspace / ".ex-memory-validation-workspace").is_file():
        raise RuntimeError("必须通过隔离 run_checks.py 执行")
    runtime = workspace / "runtime" / "protocol-loop"
    runtime.mkdir(exist_ok=False)
    os.environ["LOOP_DB"] = str(runtime / "users.db")
    os.environ["LOOP_EXES"] = str(runtime / "exes")
    load_fixture()
    from fastapi.testclient import TestClient
    from core.billing import ensure_account, topup
    from core.mirror_store import mirror_store
    from mcp_server import tokens
    from server.auth import login_user, register_user, validate_token
    from server.app import create_app

    register_user("loop_protocol_owner", "offline-password-123")
    web_token = login_user("loop_protocol_owner", "offline-password-123")
    uid = validate_token(web_token)
    token = tokens.issue(uid, label="offline-loop-protocol")
    store = mirror_store(SLUG, uid)
    store.write_json(
        "meta.json",
        {
            "owner_user_id": uid,
            "creation_source": "chat_import",
            "pipeline_state": "completed",
        },
    )
    store.write_text("SKILL.md", "# 合成人格\n只使用合成资料\n---\n\n## PART B\n规则")
    # 真实临时账本入账，不调用支付服务，也不触及现有账号。
    topup(ensure_account(uid), 10_000_000, provider="offline-fixture")
    try:
        with TestClient(create_app()) as client:
            asyncio.run(
                scenario(client, {"Authorization": f"Bearer {web_token}"}, uid, token)
            )
    finally:
        (runtime / "results.json").write_text(
            json.dumps(RESULTS, ensure_ascii=False, indent=2)
        )
    print(
        f"协议闭环验证：{len(RESULTS)}/{len(RESULTS)} 通过。模型、检索与摘要为合成替身。"
    )


if __name__ == "__main__":
    main()
