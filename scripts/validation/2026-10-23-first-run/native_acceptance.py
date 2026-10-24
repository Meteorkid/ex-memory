"""在隔离 loopback fixture 上验收原生文本协议；不导入应用或读取真实凭据。"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import sqlite3
import time
from typing import Any
import urllib.error
import urllib.request
import uuid


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # 即使本机服务异常返回 Location，也不让验收流量离开 loopback。
        return None


def identity(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()[:12]


class NativeAcceptance:
    def __init__(self, root: Path, port: int):
        self.root = root
        self.base = f"http://127.0.0.1:{port}"
        self.run = "native_" + uuid.uuid4().hex[:10]
        self.checks: list[dict[str, Any]] = []
        self.headers: dict[str, str] = {}
        self.http = urllib.request.build_opener(
            urllib.request.ProxyHandler({}), NoRedirect()
        )
        self.cases = [
            {
                "name": "custom_anthropic_custom_gemini",
                "llm": {
                    "provider": "custom_anthropic",
                    "base_url": "https://anthropic.models.example.com/v1",
                    "model": "fixture-anthropic-chat",
                },
                "embedding": {
                    "provider": "custom_gemini",
                    "base_url": "https://gemini.models.example.com/v1beta",
                    "model": "fixture-gemini-embedding",
                },
                "protocol": "anthropic",
                "llm_host": "anthropic.models.example.com",
                "embedding_host": "gemini.models.example.com",
            },
            {
                "name": "gemini_gemini",
                "llm": {"provider": "gemini", "model": "fixture-gemini-chat"},
                "embedding": {
                    "provider": "gemini",
                    "model": "fixture-gemini-embedding",
                },
                "protocol": "gemini",
                "llm_host": "generativelanguage.googleapis.com",
                "embedding_host": "generativelanguage.googleapis.com",
            },
        ]
        for index, case in enumerate(self.cases):
            case["user"] = f"{self.run}-account-{index}"
            case["slug"] = f"{self.run}_{index}"
        self.keys = {
            (case["user"], kind, version): f"fixture-{case['user']}-{kind}-v{version}"
            for case in self.cases
            for kind in ("llm", "embedding")
            for version in (1, 2)
        }

    def path(self, relative: str) -> Path:
        path = (self.root / relative).resolve()
        if not path.is_relative_to(self.root):
            raise ValueError("验收文件路径不可通过符号链接离开工作区")
        return path

    def check(self, name: str, condition: bool, evidence: Any = None) -> None:
        self.checks.append(
            {"name": name, "passed": bool(condition), "evidence": evidence}
        )
        if not condition:
            raise AssertionError(f"{name}: {evidence}")

    def request(
        self,
        method: str,
        path: str,
        *,
        user: str,
        data: dict | None = None,
        upload: bool = False,
    ) -> tuple[int, Any]:
        headers = {"X-Sandbox-User": user}
        payload = None
        if upload:
            sample = self.path("samples/synthetic-wechat.json").read_bytes()
            boundary = "----SyntheticNative" + uuid.uuid4().hex
            payload = (
                (
                    f'--{boundary}\r\nContent-Disposition: form-data; name="target_name"'
                    "\r\n\r\n小雨\r\n"
                    f'--{boundary}\r\nContent-Disposition: form-data; name="file"; '
                    'filename="synthetic-wechat.json"\r\n'
                    "Content-Type: application/json\r\n\r\n"
                ).encode()
                + sample
                + f"\r\n--{boundary}--\r\n".encode()
            )
            headers["Content-Type"] = "multipart/form-data; boundary=" + boundary
        elif data is not None:
            payload = json.dumps(data).encode()
            headers["Content-Type"] = "application/json"
        prefix = "" if path.startswith("/__fixture__/") else "/api"
        request = urllib.request.Request(
            self.base + prefix + path, data=payload, headers=headers, method=method
        )
        try:
            response = self.http.open(request, timeout=45)
        except urllib.error.HTTPError as error:
            response = error
        with response:
            self.headers = {
                key.lower(): value for key, value in response.headers.items()
            }
            content = response.read().decode()
            if self.headers.get("content-type", "").startswith("application/json"):
                content = json.loads(content)
            return response.code, content

    def records(self) -> list[dict]:
        path = self.path("model-calls.jsonl")
        return (
            [json.loads(line) for line in path.read_text().splitlines() if line]
            if path.exists()
            else []
        )

    def settings(self, case: dict, *, update: dict | None = None) -> dict:
        status, result = self.request(
            "GET" if update is None else "PUT",
            "/account/model-settings",
            user=case["user"],
            data=update,
        )
        prefix = case["name"] + (
            "_read_settings" if update is None else "_save_settings"
        )
        self.check(prefix + "_status", status == 200, {"status": status})
        self.check(
            prefix + "_no_store", self.headers.get("cache-control") == "no-store"
        )
        self.check(
            prefix + "_no_key_echo",
            not any(key in json.dumps(result) for key in self.keys.values()),
        )
        return result

    def bind_workspace(self, case: dict) -> None:
        status, state = self.request("GET", "/__fixture__/state", user=case["user"])
        self.check(
            case["name"] + "_fixture_endpoint", status == 200, {"status": status}
        )
        # 端口必须对应这份带标记的数据库，不能误测另一台本机服务。
        database = self.path("runtime/data/users.db")
        with sqlite3.connect(database.as_uri() + "?mode=ro", uri=True) as conn:
            row = conn.execute(
                "SELECT user_id FROM external_identities "
                "WHERE provider = ? AND external_user_id = ?",
                ("meteor-store", case["user"]),
            ).fetchone()
        self.check(
            case["name"] + "_workspace_matches_port",
            bool(row) and row[0] == state["user_id"],
        )

    def poll(self, case: dict, task_id: str) -> dict:
        deadline = time.monotonic() + 45
        while time.monotonic() < deadline:
            status, task = self.request("GET", "/tasks/" + task_id, user=case["user"])
            self.check(case["name"] + "_task_owned", status == 200, {"status": status})
            if task["status"] in ("failed", "succeeded"):
                self.check(
                    case["name"] + "_import_completed",
                    task["status"] == "succeeded",
                    task,
                )
                return task
            time.sleep(0.05)
        raise AssertionError(case["name"] + "_task_timeout")

    def stream(self, case: dict, version: int) -> str:
        status, stream = self.request(
            "POST",
            "/chat/stream",
            user=case["user"],
            data={
                "slug": case["slug"],
                "message": "合成测试：小雨喜欢喝什么？",
                "history": [],
            },
        )
        prefix = f"{case['name']}_stream_v{version}"
        self.check(
            prefix + "_http",
            status == 200 and isinstance(stream, str),
            {"status": status},
        )
        lines = stream.splitlines()
        events = [
            json.loads(line[6:])
            for line in lines
            if line.startswith("data: ") and line != "data: [DONE]"
        ]
        reply = "".join(
            event.get("content", "") for event in events if event.get("type") == "text"
        )
        marker = identity(self.keys[(case["user"], "llm", version)])
        self.check(
            prefix + "_done_with_current_key",
            "data: [DONE]" in lines and marker in reply,
            {"identity": marker},
        )
        self.check(
            prefix + "_no_other_key_marker",
            not any(
                identity(key) in reply
                for key in self.keys.values()
                if identity(key) != marker
            ),
        )
        self.check(
            prefix + "_no_key_echo",
            not any(key in stream for key in self.keys.values()),
        )
        return marker

    def ingest(self, case: dict) -> None:
        user, slug, prefix = case["user"], case["slug"], case["name"]
        status, cfg = self.request("GET", "/import/config", user=user)
        self.check(
            prefix + "_both_models_configured",
            status == 200 and cfg["llm_configured"] and cfg["embedding_configured"],
        )
        status, _ = self.request(
            "POST",
            "/consents",
            user=user,
            data={
                "policy_type": "third_party_data",
                "policy_version": cfg["third_party_data_policy_version"],
            },
        )
        self.check(prefix + "_synthetic_explicit_consent", status == 200)
        calls = len(self.records())
        status, _ = self.request(
            "POST",
            "/exes",
            user=user,
            data={
                "slug": slug,
                "name": "原生协议合成验收",
                "answers": [],
                "defer_generation": True,
            },
        )
        self.check(prefix + "_deferred_draft_created", status == 200)
        self.check(prefix + "_draft_does_not_call_model", len(self.records()) == calls)
        status, entries = self.request("GET", "/exes", user=user)
        self.check(
            prefix + "_draft_awaits_import",
            status == 200
            and any(
                entry["slug"] == slug and entry["state"] == "awaiting_import"
                for entry in entries
            ),
        )
        status, uploaded = self.request(
            "POST", f"/exes/{slug}/import", user=user, upload=True
        )
        self.check(
            prefix + "_synthetic_wechat_accepted", status == 200, {"status": status}
        )
        case["task_id"] = uploaded["task_id"]
        task = self.poll(case, case["task_id"])
        result = (
            json.loads(task["result"])
            if isinstance(task["result"], str)
            else task["result"]
        )
        self.check(
            prefix + "_import_statistics",
            result["messages"] == 24
            and result["chunks"] > 0
            and result["state"] == "completed",
            result,
        )
        status, entries = self.request("GET", "/exes", user=user)
        self.check(
            prefix + "_mirror_ready",
            status == 200
            and any(
                entry["slug"] == slug and entry["state"] == "completed"
                for entry in entries
            ),
        )

    def lock_and_rotate(self, case: dict) -> None:
        settings = self.settings(case)
        prefix = case["name"]
        self.check(prefix + "_embedding_locked", settings["embedding"]["locked"])
        for label, change in (
            ("model", {"model": "fixture-different-embedding"}),
            (
                "protocol",
                {
                    "provider": "custom",
                    "base_url": "https://models.example.com/v1",
                    "api_key": self.keys[(case["user"], "embedding", 2)],
                },
            ),
        ):
            calls = len(self.records())
            status, _ = self.request(
                "PUT",
                "/account/model-settings",
                user=case["user"],
                data={
                    "expected_revision": settings["revision"],
                    "embedding": change,
                },
            )
            self.check(
                prefix + "_embedding_" + label + "_switch_rejected",
                status == 409,
                {"status": status},
            )
            self.check(
                prefix + "_locked_change_no_model_call", len(self.records()) == calls
            )
        unchanged = self.settings(case)
        self.check(prefix + "_locked_configuration_unchanged", unchanged == settings)
        first_marker = self.stream(case, 1)
        rotated = self.settings(
            case,
            update={
                "expected_revision": settings["revision"],
                "llm": {"api_key": self.keys[(case["user"], "llm", 2)]},
            },
        )
        self.check(
            prefix + "_rotation_only_changes_llm",
            rotated["embedding"] == settings["embedding"]
            and rotated["revision"] == settings["revision"] + 1,
        )
        self.check(
            prefix + "_rotation_uses_new_key", self.stream(case, 2) != first_marker
        )

    def verify_calls(self) -> None:
        records = self.records()
        self.check(
            "station_sentinel_zero_calls",
            not any(row.get("key_class") == "station_sentinel" for row in records),
        )
        for case in self.cases:
            for kind, version in (("llm", 1), ("llm", 2), ("embedding", 1)):
                marker = identity(self.keys[(case["user"], kind, version)])
                calls = [row for row in records if row.get("identity") == marker]
                protocol = case["protocol"] if kind == "llm" else "gemini"
                evidence = {
                    "identity": marker,
                    "protocol": protocol,
                    "calls": len(calls),
                }
                self.check(
                    f"{case['name']}_{kind}_v{version}_native_calls",
                    bool(calls)
                    and all(
                        row.get("kind") == kind
                        and row.get("protocol") == protocol
                        and row.get("host") == case[kind + "_host"]
                        and row.get("key_class") == "synthetic_account"
                        and row.get("accepted")
                        for row in calls
                    ),
                    evidence,
                )
                if kind == "llm":
                    self.check(
                        f"{case['name']}_v{version}_native_stream",
                        any(row.get("stream") for row in calls),
                        evidence,
                    )
                    if version == 1:
                        self.check(
                            case["name"] + "_native_profile_generation",
                            any(not row.get("stream") for row in calls),
                            evidence,
                        )

    def execute(self) -> None:
        status, _ = self.request("GET", "/account/model-settings", user="anonymous")
        self.check("anonymous_settings_denied", status == 401)
        for case in self.cases:
            self.bind_workspace(case)
            settings = self.settings(case)
            self.check(
                case["name"] + "_fresh_account_has_no_keys",
                settings["revision"] == 0
                and not settings["llm"]["has_key"]
                and not settings["embedding"]["has_key"],
            )
            status, cfg = self.request("GET", "/import/config", user=case["user"])
            self.check(
                case["name"] + "_no_station_fallback",
                status == 200
                and not cfg["llm_configured"]
                and not cfg["embedding_configured"],
            )
            update = {"expected_revision": settings["revision"]}
            for kind in ("llm", "embedding"):
                update[kind] = {
                    **case[kind],
                    "api_key": self.keys[(case["user"], kind, 1)],
                }
            saved = self.settings(case, update=update)
            self.check(
                case["name"] + "_native_providers_saved",
                all(
                    saved[kind]["provider"] == case[kind]["provider"]
                    and saved[kind]["has_key"]
                    for kind in ("llm", "embedding")
                ),
            )
        for case in self.cases:
            self.ingest(case)
            self.lock_and_rotate(case)
        for owner, other in (self.cases, self.cases[::-1]):
            calls = len(self.records())
            status, _ = self.request(
                "GET", "/tasks/" + owner["task_id"], user=other["user"]
            )
            self.check(owner["name"] + "_task_hidden_from_other_account", status == 404)
            status, _ = self.request(
                "POST", f"/exes/{owner['slug']}/import", user=other["user"], upload=True
            )
            self.check(
                owner["name"] + "_cross_account_import_denied", status in (403, 404)
            )
            status, entries = self.request("GET", "/exes", user=other["user"])
            self.check(
                owner["name"] + "_mirror_hidden_from_other_account",
                status == 200
                and not any(entry["slug"] == owner["slug"] for entry in entries),
            )
            self.check(
                owner["name"] + "_denied_access_no_model_call",
                len(self.records()) == calls,
            )
        self.verify_calls()

    def report(self) -> None:
        report = {
            "run": self.run,
            "synthetic_only": True,
            "network": "127.0.0.1",
            "provider_substitution": "final_http_transport_only",
            "account_routes_sdk_and_native_adapter": "real",
            "checks": self.checks,
            "passed": bool(self.checks) and all(c["passed"] for c in self.checks),
        }
        target = self.path("native-acceptance.json")
        temporary = self.path(f".native-acceptance-{self.run}.tmp")
        temporary.write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        temporary.replace(target)
        print(
            json.dumps(
                {
                    "report": str(target),
                    "passed": report["passed"],
                    "checks": len(self.checks),
                },
                ensure_ascii=False,
            )
        )


def main() -> None:
    parser = argparse.ArgumentParser(
        description="离线原生协议 HTTP 验收，不启动或刷新 fixture"
    )
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--port", type=int, default=8769)
    args = parser.parse_args()
    root = args.workspace.resolve()
    repo = Path(__file__).resolve().parents[3]
    if root == repo or repo in root.parents or root in repo.parents:
        parser.error("--workspace 必须与原仓库目录分离")
    if not (root / ".ex-memory-validation-workspace").is_file():
        parser.error("目录不属于隔离验收服务，请先运行 sandbox.py sync")
    if not 1 <= args.port <= 65535:
        parser.error("--port 必须在 1–65535 之间；主机固定为 127.0.0.1")
    acceptance = NativeAcceptance(root, args.port)
    try:
        acceptance.execute()
    except BaseException as error:
        acceptance.checks.append(
            {"name": "run_error", "passed": False, "evidence": str(error)}
        )
        raise
    finally:
        acceptance.report()


if __name__ == "__main__":
    main()
