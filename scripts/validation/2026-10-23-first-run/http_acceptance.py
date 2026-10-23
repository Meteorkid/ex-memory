"""HTTP acceptance against the synthetic loopback app, not production."""

from __future__ import annotations
import argparse
import json
from pathlib import Path
import time
import urllib.request
import urllib.error
import uuid

parser = argparse.ArgumentParser(
    description="对仅含合成数据的本机验收服务运行 HTTP 流程"
)
parser.add_argument("--workspace", type=Path, required=True)
parser.add_argument("--port", type=int, default=8765)
args = parser.parse_args()
ROOT = args.workspace.resolve()
if not (ROOT / ".ex-memory-validation-workspace").is_file():
    parser.error("目录不属于隔离验收服务，请先运行 sandbox.py sync")
BASE = f"http://127.0.0.1:{args.port}/api"
RUN = "acceptance_" + uuid.uuid4().hex[:10]
USER = RUN + "-owner"
OTHER = RUN + "-other"
checks = []


def request(method, path, data=None, *, user=USER, file=None, target="小雨"):
    headers = {"X-Sandbox-User": user}
    payload = None
    if file is not None:
        boundary = "----SyntheticBoundary" + uuid.uuid4().hex
        payload = (
            f'--{boundary}\r\nContent-Disposition: form-data; name="target_name"\r\n\r\n{target}\r\n--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="{file.name}"\r\nContent-Type: application/octet-stream\r\n\r\n'.encode()
            + file.read_bytes()
            + f"\r\n--{boundary}--\r\n".encode()
        )
        headers["Content-Type"] = "multipart/form-data; boundary=" + boundary
    elif data is not None:
        payload = json.dumps(data).encode()
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(
        BASE + path, data=payload, headers=headers, method=method
    )
    try:
        with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(
            req, timeout=45
        ) as response:
            content = response.read().decode()
            return response.status, json.loads(content) if response.headers.get(
                "Content-Type", ""
            ).startswith("application/json") else content
    except urllib.error.HTTPError as exc:
        content = exc.read().decode()
        try:
            content = json.loads(content)
        except json.JSONDecodeError:
            pass
        return exc.code, content


def check(name, condition, evidence=None):
    checks.append({"name": name, "passed": bool(condition), "evidence": evidence})
    if not condition:
        raise AssertionError(f"{name}: {evidence}")


def poll(task_id):
    for _ in range(200):
        status, task = request("GET", "/tasks/" + task_id)
        if status != 200:
            check("task_read_owned", False, status)
        if task["status"] in ("failed", "succeeded"):
            check("task_read_owned", True, status)
            return task
        time.sleep(0.05)
    raise AssertionError("task timeout")


def model_count():
    path = ROOT / "model-calls.jsonl"
    return len(path.read_text().splitlines()) if path.exists() else 0


def main():
    status, cfg = request("GET", "/import/config")
    check(
        "import_configuration",
        status == 200 and cfg["llm_configured"] and cfg["embedding_configured"],
        cfg,
    )
    status, data = request(
        "POST",
        "/exes",
        {"slug": RUN, "name": "验收代号", "answers": [], "defer_generation": True},
    )
    check("draft_creation", status == 200, data)
    status, entries = request("GET", "/exes")
    draft = next(e for e in entries if e["slug"] == RUN)
    check("draft_waiting_for_import", draft["state"] == "awaiting_import", draft)
    status, data = request(
        "POST", f"/exes/{RUN}/import", file=ROOT / "samples/synthetic-wechat.json"
    )
    check("missing_consent_rejected", status == 403, data)
    status, data = request(
        "POST",
        "/consents",
        {
            "policy_type": "third_party_data",
            "policy_version": cfg["third_party_data_policy_version"],
        },
    )
    check("synthetic_explicit_consent", status == 200, data)
    for filename in ("empty.json", "invalid.json"):
        calls = model_count()
        status, data = request(
            "POST", f"/exes/{RUN}/import", file=ROOT / "samples" / filename
        )
        task = poll(data["task_id"]) if status == 200 else None
        check(
            filename + "_rejected",
            (task and task["status"] == "failed") or status == 400,
            task or data,
        )
        check(
            filename + "_no_model_calls", model_count() == calls, model_count() - calls
        )
    status, data = request(
        "POST",
        f"/exes/{RUN}/import",
        file=ROOT / "samples/synthetic-qq.txt",
        target="不存在的人",
    )
    task = poll(data["task_id"]) if status == 200 else None
    check(
        "speaker_mismatch_rejected",
        (task and task["status"] == "failed") or status == 400,
        task or data,
    )
    if task:
        status, retry = request(
            "POST",
            f"/tasks/{task['task_id'] if 'task_id' in task else task['id']}/retry",
        )
        check("retry_requests_reupload", status == 400, retry)
    status, data = request(
        "POST", f"/exes/{RUN}/import", file=ROOT / "samples/synthetic-wechat.json"
    )
    check("valid_upload_accepted", status == 200, data)
    success_task_id = data["task_id"]
    task = poll(success_task_id)
    check("valid_upload_completed", task["status"] == "succeeded", task)
    result = task["result"]
    if isinstance(result, str):
        result = json.loads(result)
    check(
        "positive_import_statistics",
        result["messages"] == 24
        and result["chunks"] > 0
        and result["state"] == "completed",
        result,
    )
    status, entries = request("GET", "/exes")
    check(
        "ready_mirror",
        any(e["slug"] == RUN and e["state"] == "completed" for e in entries),
        entries,
    )
    mirror = next(
        p.parent
        for p in (ROOT / "runtime/exes").rglob("meta.json")
        if json.loads(p.read_text()).get("slug") == RUN
    )
    corpus = [
        json.loads(line)
        for line in (mirror / "corpus.jsonl").read_text().splitlines()
        if line.strip()
    ]
    check(
        "corpus_has_correct_speakers",
        len(corpus) == 24 and sum(bool(m.get("is_target")) for m in corpus) == 12,
        {
            "messages": len(corpus),
            "target": sum(bool(m.get("is_target")) for m in corpus),
        },
    )
    check(
        "generated_files_exist",
        all(
            (mirror / name).is_file()
            for name in ("SKILL.md", "memory.md", "persona.md")
        ),
        str(mirror),
    )
    check(
        "generated_memory_uses_import",
        "冰美式" in (mirror / "memory.md").read_text(),
        "synthetic_fact_in_memory",
    )
    status, stage = request("GET", f"/exes/{RUN}/stage")
    check("import_starts_with_source_stage", status == 200 and stage["stage"] == "source", stage)
    skill = (mirror / "SKILL.md").read_text()
    check(
        "import_identifies_ai_simulation",
        "AI 模拟" in skill and "不是 AI 助手" not in skill and "分手是已经发生" not in skill,
        "source_grounded_skill_rules",
    )
    status, repeated = request(
        "POST", f"/exes/{RUN}/import", file=ROOT / "samples/synthetic-wechat.json"
    )
    repeated_task = poll(repeated["task_id"])
    check(
        "same_file_retry_is_idempotent",
        repeated_task["status"] == "succeeded"
        and len((mirror / "corpus.jsonl").read_text().splitlines()) == 24,
        repeated_task,
    )
    status, data = request("GET", "/tasks/" + success_task_id, user=OTHER)
    check("cross_user_task_hidden", status == 404, data)
    status, data = request(
        "POST",
        f"/exes/{RUN}/import",
        file=ROOT / "samples/synthetic-wechat.json",
        user=OTHER,
    )
    check("cross_user_import_denied", status in (403, 404), data)
    status, data = request(
        "POST",
        "/exes",
        {
            "slug": RUN,
            "name": "另一账号独立代号",
            "answers": [],
            "defer_generation": True,
        },
        user=OTHER,
    )
    check("same_slug_separate_users", status == 200, data)
    status, data = request("GET", "/exes", user="anonymous")
    check("anonymous_denied", status == 401, data)
    status, stream = request(
        "POST",
        "/chat/stream",
        {"slug": RUN, "message": "合成测试：你喜欢喝什么？", "history": []},
    )
    events = [
        json.loads(line[6:])
        for line in stream.splitlines()
        if line.startswith("data: ") and line != "data: [DONE]"
    ]
    reply = "".join(
        event.get("content", "") for event in events if event.get("type") == "text"
    )
    check(
        "first_chat_stream",
        status == 200 and "离线验收回复" in reply and "[DONE]" in stream,
        {"status": status, "reply": reply},
    )


try:
    main()
except BaseException as error:
    checks.append({"name": "run_error", "passed": False, "evidence": str(error)})
    raise
finally:
    report = {
        "run": RUN,
        "synthetic_only": True,
        "network": "loopback",
        "checks": checks,
        "passed": all(c["passed"] for c in checks),
    }
    (ROOT / "http-acceptance.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2)
    )
    print(
        json.dumps(
            {
                "report": str(ROOT / "http-acceptance.json"),
                "passed": report["passed"],
                "checks": len(checks),
            },
            ensure_ascii=False,
        )
    )
