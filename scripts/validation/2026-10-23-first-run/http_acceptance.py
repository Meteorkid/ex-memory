"""HTTP acceptance against the synthetic loopback app, not production."""

from __future__ import annotations
import argparse
import json
import hashlib
import sqlite3
from pathlib import Path
import time
import urllib.request
import urllib.error
import uuid

parser = argparse.ArgumentParser(
    description="对仅含合成数据的本机验收服务运行 HTTP 流程"
)
parser.add_argument("--workspace", type=Path, required=True)
parser.add_argument("--port", type=int, default=8769)
args = parser.parse_args()
ROOT = args.workspace.resolve()
if not (ROOT / ".ex-memory-validation-workspace").is_file():
    parser.error("目录不属于隔离验收服务，请先运行 sandbox.py sync")
BASE = f"http://127.0.0.1:{args.port}/api"
RUN = "acceptance_" + uuid.uuid4().hex[:10]
USER = RUN + "-owner"
OTHER = RUN + "-other"
checks = []
response_headers = {}
KEYS = {
    (user, kind, version): f"fixture-{user}-{kind}-v{version}"
    for user in (USER, OTHER)
    for kind in ("llm", "embedding")
    for version in (1, 2, 3)
}


def identity(key):
    return hashlib.sha256(key.encode()).hexdigest()[:12]


def request(method, path, data=None, *, user=USER, file=None, target="小雨"):
    global response_headers
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
        (BASE.removesuffix("/api") if path.startswith("/__fixture__/") else BASE) + path,
        data=payload, headers=headers, method=method
    )
    try:
        with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(
            req, timeout=45
        ) as response:
            response_headers = dict(response.headers.items())
            content = response.read().decode()
            return response.status, json.loads(content) if response.headers.get(
                "Content-Type", ""
            ).startswith("application/json") else content
    except urllib.error.HTTPError as exc:
        response_headers = dict(exc.headers.items())
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


def poll(task_id, *, user=USER):
    for _ in range(200):
        status, task = request("GET", "/tasks/" + task_id, user=user)
        if status != 200:
            check("task_read_owned", False, status)
        if task["status"] in ("failed", "succeeded"):
            check("task_read_owned", True, status)
            return task
        time.sleep(0.05)
    raise AssertionError("task timeout")


def model_count():
    return len(model_records())


def model_records():
    path = ROOT / "model-calls.jsonl"
    identities = {identity(key) for key in KEYS.values()}
    return [row for row in (
        json.loads(line) for line in path.read_text().splitlines()
    ) if row.get("identity") in identities or row.get("key_class") == "station_sentinel"] if path.exists() else []


def private_settings_response(name, status, data):
    check(name + "_status", status == 200, {"status": status})
    check(name + "_no_store", any(k.lower() == "cache-control" and v == "no-store" for k, v in response_headers.items()))
    check(name + "_no_plaintext_key", not any(key in json.dumps(data) for key in KEYS.values()))


def save_settings(user, revision, version, *, initial=False):
    payload = {"expected_revision": revision}
    for kind in ("llm", "embedding"):
        payload[kind] = {"api_key": KEYS[(user, kind, version)]}
        if initial:
            payload[kind].update(
                provider="custom", base_url="https://models.example.com/v1",
                model="fixture-chat-model" if kind == "llm" else "fixture-embedding-model",
            )
    status, data = request("PUT", "/account/model-settings", payload, user=user)
    private_settings_response(f"save_{'owner' if user == USER else 'other'}_v{version}", status, data)
    check("saved_key_flags", data["llm"]["has_key"] and data["embedding"]["has_key"])
    return data


def stream_chat(user, version):
    status, stream = request("POST", "/chat/stream", {
        "slug": RUN, "message": "合成测试：你喜欢喝什么？", "history": [],
    }, user=user)
    check("chat_stream_http", status == 200 and isinstance(stream, str), {"status": status})
    events = [json.loads(line[6:]) for line in stream.splitlines()
              if line.startswith("data: ") and line != "data: [DONE]"]
    reply = "".join(event.get("content", "") for event in events if event.get("type") == "text")
    marker = identity(KEYS[(user, "llm", version)])
    check("chat_uses_current_account_key", marker in reply and "[DONE]" in stream, {
        "user": "owner" if user == USER else "other", "version": version, "identity": marker,
    })
    check("chat_no_key_echo", not any(key in reply for key in KEYS.values()))
    return marker


def main():
    status, data = request("GET", "/account/model-settings", user="anonymous")
    check("anonymous_settings_denied", status == 401, {"status": status})
    status, cfg = request("GET", "/import/config")
    check(
        "new_account_does_not_inherit_station_keys",
        status == 200 and not cfg["llm_configured"] and not cfg["embedding_configured"],
        cfg,
    )
    status, settings = request("GET", "/account/model-settings")
    private_settings_response("initial_settings", status, settings)
    check("new_account_unconfigured", settings["revision"] == 0 and not settings["llm"]["has_key"] and not settings["embedding"]["has_key"])
    check("fixture_encryption_available", settings["storage_available"])
    settings = save_settings(USER, settings["revision"], 1, initial=True)
    status, cfg = request("GET", "/import/config")
    check("import_configuration", status == 200 and cfg["llm_configured"] and cfg["embedding_configured"], cfg)
    status, other_settings = request("GET", "/account/model-settings", user=OTHER)
    check("other_account_has_no_inherited_keys", status == 200 and not other_settings["llm"]["has_key"] and not other_settings["embedding"]["has_key"])
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
    status, settings = request("GET", "/account/model-settings")
    check("embedding_model_locked_after_ingest", status == 200 and settings["embedding"]["locked"])
    status, blocked_change = request("PUT", "/account/model-settings", {
        "expected_revision": settings["revision"], "embedding": {"model": "different-vector-model"},
    })
    check("embedding_model_switch_rejected", status == 409, blocked_change)
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
    status, data = request("GET", "/tasks/not_a_real_task", user=OTHER)
    check("unknown_task_is_404", status == 404, {"status": status})
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
    owner_marker = stream_chat(USER, 1)
    status, state = request("GET", "/__fixture__/state")
    check("account_caches_populated", status == 200 and state["engine_keys"] and state["client_revisions"] and state["router_revisions"], state)
    with sqlite3.connect(ROOT / "runtime/data/users.db") as conn:
        ciphertexts = conn.execute("SELECT llm_key_ciphertext, embedding_key_ciphertext FROM user_model_settings WHERE user_id = ?", (state["user_id"],)).fetchone()
    check("keys_encrypted_at_rest", all(value.startswith("v1.") for value in ciphertexts) and not any(key in "".join(ciphertexts) for key in KEYS.values()))
    check("keys_absent_from_sqlite_plaintext", not any(key.encode() in (ROOT / "runtime/data/users.db").read_bytes() for key in KEYS.values()))
    settings = save_settings(USER, settings["revision"], 2)
    status, state = request("GET", "/__fixture__/state")
    check("key_rotation_invalidates_all_account_caches", status == 200 and not state["engine_keys"] and not state["client_revisions"] and not state["router_revisions"], state)
    owner_rotated_marker = stream_chat(USER, 2)
    check("rotation_uses_new_key", owner_rotated_marker != owner_marker)
    calls = model_count()
    status, settings = request("PUT", "/account/model-settings", {
        "expected_revision": settings["revision"], "llm": {"delete_key": True}, "embedding": {"delete_key": True},
    })
    private_settings_response("delete_keys", status, settings)
    status, state = request("GET", "/__fixture__/state")
    check("key_delete_invalidates_all_account_caches", status == 200 and not state["engine_keys"] and not state["client_revisions"] and not state["router_revisions"], state)
    status, data = request("POST", "/chat", {"slug": RUN, "message": "删除 Key 后不应调用模型"})
    check("deleted_keys_block_existing_mirror", status == 409 and model_count() == calls, {"status": status, "new_calls": model_count() - calls})
    status, other_settings = request("GET", "/account/model-settings", user=OTHER)
    check("other_account_remains_unconfigured", status == 200 and other_settings["revision"] == 0)
    save_settings(OTHER, other_settings["revision"], 1, initial=True)
    status, data = request("POST", "/consents", {"policy_type": "third_party_data", "policy_version": cfg["third_party_data_policy_version"]}, user=OTHER)
    check("other_account_explicit_consent", status == 200)
    status, data = request("POST", f"/exes/{RUN}/import", file=ROOT / "samples/synthetic-wechat.json", user=OTHER)
    check("other_account_import_accepted", status == 200, data)
    task = poll(data["task_id"], user=OTHER)
    check("other_account_import_completed", task["status"] == "succeeded", task)
    other_marker = stream_chat(OTHER, 1)
    check("two_accounts_use_distinct_keys", other_marker not in (owner_marker, owner_rotated_marker))
    settings = save_settings(USER, settings["revision"], 3)
    stream_chat(USER, 3)
    records = model_records()
    check("account_model_calls_use_real_getter_and_sdk_transport", {row["kind"] for row in records} == {"llm", "embedding"}, {"calls": len(records)})
    check("station_sentinel_never_called", all(row["key_class"] == "synthetic_account" and row["accepted"] for row in records))


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
        "provider_substitution": "final_http_transport_only",
        "account_getters_router_sdk": "real",
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
