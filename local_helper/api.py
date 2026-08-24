"""macOS 本地助手的有限 localhost API。"""

from __future__ import annotations

from html import escape
import platform
import webbrowser
import threading
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlencode

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel, Field

from local_helper import __version__
from local_helper.export.html_exporter import export_conversation
from local_helper.security import LocalSession, LocalSessionStore, OneTimeTicketStore, is_loopback_host, validate_origin
from local_helper.task_store import PublicTaskStore
from local_helper.wechat_macos.discovery import SUPPORTED_WECHAT_VERSIONS, detect_environment
from local_helper.wechat_macos.catalog import discover_decrypted_catalog, load_session_catalog
from local_helper.wechat_macos.pipeline import run_expert_decryption
from local_helper.wechat_macos.reader import iter_messages
from local_helper.wechat_macos.sip import get_sip_status
from local_helper.workflow import ExpertWorkflow, WorkflowError, WorkflowPhase, WorkflowState


LOCAL_SESSION_COOKIE = "ex_memory_local_session"


@dataclass(frozen=True)
class HelperSettings:
    allowed_origins: frozenset[str]
    local_base_url: str = "http://127.0.0.1:17653"
    open_browser_on_launch: bool = True
    workflow_root: Path = field(
        default_factory=lambda: Path.home() / "Library" / "Application Support" / "ex-memory-helper" / "tasks"
    )
    capture_launcher: Path = field(
        default_factory=lambda: Path(__file__).parent / "wechat_macos" / "lldb_capture_launcher.sh"
    )
    capture_module: Path = field(
        default_factory=lambda: Path(__file__).parent / "wechat_macos" / "lldb_key_capture.py"
    )
    sqlcipher_binary: Path = field(default_factory=lambda: Path(__file__).parent / "bin" / "sqlcipher")
    export_root: Path = field(default_factory=lambda: Path.home() / "Downloads" / "ex-memory-wechat-exports")


class PrepareExpertRequest(BaseModel):
    account_id: str
    key_rules_confirmed: bool = False


class ExportConversationRequest(BaseModel):
    session_wxid: str = Field(min_length=1, max_length=255)


class ResumeWorkflowRequest(BaseModel):
    task_id: str = Field(pattern=r"^[0-9a-f]{32}$")


def create_helper_app(settings: HelperSettings) -> FastAPI:
    if not settings.allowed_origins:
        raise ValueError("至少需要一个网站 Origin 白名单")

    app = FastAPI(
        title="ex-memory Local Helper",
        version=__version__,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    tickets = OneTimeTicketStore()
    sessions = LocalSessionStore()
    tasks = PublicTaskStore()
    app.state.settings = settings
    app.state.tickets = tickets
    app.state.sessions = sessions
    app.state.tasks = tasks
    app.state.environment_provider = detect_environment
    app.state.sip_status_provider = get_sip_status
    app.state.workflow = None
    app.state.public_task_aliases = {}

    def issue_local_url(task_id: str) -> str:
        ticket = tickets.issue()
        query = urlencode({"ticket": ticket.token, "task": task_id})
        local_url = f"{settings.local_base_url}/local/export?{query}"
        if settings.open_browser_on_launch:
            webbrowser.open(local_url, new=2)
        return local_url

    @app.middleware("http")
    async def enforce_loopback_and_origin(request: Request, call_next):
        if not is_loopback_host(request.headers.get("host", "")):
            return JSONResponse(status_code=421, content={"error": "invalid_host"})

        if request.url.path.startswith("/v1/control/"):
            origin = request.headers.get("origin")
            if not validate_origin(origin, settings.allowed_origins):
                return JSONResponse(status_code=403, content={"error": "origin_not_allowed"})
            if request.method == "OPTIONS":
                response = JSONResponse(content={"ok": True})
            else:
                response = await call_next(request)
            response.headers["Access-Control-Allow-Origin"] = origin
            response.headers["Vary"] = "Origin"
            response.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
            response.headers["Access-Control-Allow-Headers"] = "Content-Type"
            if request.headers.get("access-control-request-private-network") == "true":
                response.headers["Access-Control-Allow-Private-Network"] = "true"
            return response

        if request.url.path.startswith("/local/api/"):
            origin = request.headers.get("origin")
            if origin and origin.rstrip("/") != settings.local_base_url.rstrip("/"):
                return JSONResponse(status_code=403, content={"error": "local_origin_required"})

        return await call_next(request)

    @app.get("/v1/control/health")
    def health():
        return {
            "status": "ok",
            "helper_version": __version__,
            "platform": "macos",
            "architecture": platform.machine(),
            "api_version": 1,
        }

    @app.post("/v1/control/launch")
    def launch():
        task = tasks.create()
        local_url = issue_local_url(task.task_id)
        return {"task_id": task.task_id, "launched": True, "local_url": local_url}

    @app.post("/v1/control/tasks/{task_id}/reopen")
    def reopen_local_page(task_id: str):
        try:
            task = tasks.get(task_id)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="任务不存在") from exc
        return {"task_id": task.task_id, "reopened": True, "local_url": issue_local_url(task.task_id)}

    @app.get("/v1/control/tasks/{task_id}")
    def task_status(task_id: str):
        try:
            return tasks.get(task_id).public_dict()
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="任务不存在") from exc

    @app.get("/local/export", response_class=HTMLResponse)
    def local_export(ticket: str, task: str):
        try:
            tasks.get(task)
        except (ValueError, KeyError) as exc:
            raise HTTPException(status_code=404, detail="任务不存在") from exc
        if not tickets.consume(ticket):
            raise HTTPException(status_code=403, detail="启动票据无效或已使用")
        session = sessions.issue(task)
        app.state.public_task_aliases[task] = task
        response = HTMLResponse(_local_export_page(session.csrf_token, settings.export_root))
        response.set_cookie(
            LOCAL_SESSION_COOKIE,
            session.token,
            httponly=True,
            samesite="strict",
            secure=False,
            max_age=8 * 60 * 60,
            path="/local",
        )
        response.headers["X-Ex-Memory-CSRF"] = session.csrf_token
        response.headers["Cache-Control"] = "no-store"
        return response

    def require_local_session(request: Request) -> LocalSession:
        session = sessions.get(request.cookies.get(LOCAL_SESSION_COOKIE))
        if session is None:
            raise HTTPException(status_code=401, detail="本地会话无效或已过期")
        return session

    def require_csrf(request: Request, session: LocalSession) -> None:
        if request.headers.get("x-ex-memory-csrf") != session.csrf_token:
            raise HTTPException(status_code=403, detail="CSRF 校验失败")

    def update_public_state(state: WorkflowState) -> None:
        status, progress = _public_workflow_status(state.phase)
        public_task_id = app.state.public_task_aliases.get(state.task_id, state.task_id)
        try:
            tasks.update(
                public_task_id,
                status=status,
                phase=state.phase.value,
                progress=progress,
                error_code=state.error_code,
            )
        except KeyError:
            # 重启后公共控制面任务不会恢复，但本地私有任务仍可继续。
            pass

    def get_workflow() -> ExpertWorkflow:
        if app.state.workflow is None:
            app.state.workflow = ExpertWorkflow(
                settings.workflow_root,
                sip_status=app.state.sip_status_provider,
                on_state_change=update_public_state,
            )
        return app.state.workflow

    def account_for_state(state: WorkflowState):
        environment = app.state.environment_provider()
        account = next(
            (
                item
                for item in environment.accounts
                if item.account_id == state.account_id and str(item.root) == state.account_root
            ),
            None,
        )
        if account is None:
            raise HTTPException(status_code=409, detail="微信账号目录已经变化，请重新开始")
        return environment, account

    @app.get("/local/api/environment")
    def local_environment(request: Request):
        require_local_session(request)
        environment = app.state.environment_provider()
        return {
            "platform": "macos",
            "sip_status": app.state.sip_status_provider().value,
            "wechat_version": environment.app_version,
            "supported_wechat_versions": SUPPORTED_WECHAT_VERSIONS,
            "current_account_id": environment.current_account_id,
            "data_accessible": environment.data_accessible,
            "error_code": environment.error_code,
            "accounts": [
                {
                    "account_id": account.account_id,
                    "database_count": len(account.databases),
                    "schema_fingerprint": account.schema_fingerprint,
                }
                for account in environment.accounts
            ],
        }

    @app.post("/local/api/confirm")
    def local_confirm(request: Request):
        session = require_local_session(request)
        require_csrf(request, session)
        return {"ok": True, "task_id": session.task_id}

    @app.post("/local/api/expert/prepare")
    def prepare_expert(payload: PrepareExpertRequest, request: Request):
        session = require_local_session(request)
        require_csrf(request, session)
        if not payload.account_id.strip():
            raise HTTPException(status_code=400, detail="请先选择当前登录的微信账号")
        environment = app.state.environment_provider()
        if environment.app_version not in SUPPORTED_WECHAT_VERSIONS:
            current_version = environment.app_version or "未检测到"
            supported_versions = "、".join(SUPPORTED_WECHAT_VERSIONS)
            raise HTTPException(
                status_code=409,
                detail=f"当前微信 {current_version} 尚未支持；此助手仅验证 {supported_versions}",
            )
        account = next((item for item in environment.accounts if item.account_id == payload.account_id), None)
        if account is None:
            raise HTTPException(status_code=409, detail="所选微信账号目录已变化，请刷新本地页面后重新选择")
        if not payload.key_rules_confirmed:
            raise HTTPException(status_code=409, detail="请先确认：每个微信账号密钥不同，切换账号后必须重新提取")
        try:
            state = get_workflow().prepare(
                task_id=session.task_id,
                account_id=account.account_id,
                account_root=account.root,
            )
        except (ValueError, WorkflowError) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return _private_state(state, app.state.sip_status_provider().value)

    @app.post("/local/api/expert/decrypt")
    def start_expert_decryption(request: Request):
        session = require_local_session(request)
        require_csrf(request, session)
        workflow = get_workflow()
        try:
            state = workflow.load(session.workflow_task_id)
        except WorkflowError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        environment, account = account_for_state(state)
        if environment.app_version not in SUPPORTED_WECHAT_VERSIONS:
            supported_versions = "、".join(SUPPORTED_WECHAT_VERSIONS)
            raise HTTPException(status_code=409, detail=f"当前 LLDB 提取器仅验证支持微信 {supported_versions}")
        sip_status = app.state.sip_status_provider()
        if sip_status.value != "disabled":
            raise HTTPException(status_code=409, detail="请先在恢复模式中手动关闭 SIP 并重启")

        def worker() -> None:
            try:
                run_expert_decryption(
                    workflow=workflow,
                    task_id=session.workflow_task_id,
                    account=account,
                    capture_launcher=settings.capture_launcher,
                    capture_module=settings.capture_module,
                    sqlcipher_binary=settings.sqlcipher_binary,
                    sip_status=sip_status,
                )
            except Exception as exc:
                try:
                    version = app.state.environment_provider().app_version
                except Exception:
                    version = ""
                try:
                    workflow.fail(
                        session.workflow_task_id,
                        error_code="decryption_failed",
                        error_detail=_decryption_error_detail(exc, version),
                    )
                except (ValueError, WorkflowError):
                    pass

        threading.Thread(target=worker, name=f"wechat-decrypt-{session.task_id[:8]}", daemon=True).start()
        return {"accepted": True, "task_id": session.workflow_task_id}

    @app.post("/local/api/expert/authorize-export")
    def authorize_export(request: Request):
        session = require_local_session(request)
        require_csrf(request, session)
        try:
            state = get_workflow().authorize_export(session.workflow_task_id)
        except WorkflowError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return _private_state(state, app.state.sip_status_provider().value)

    @app.get("/local/api/task")
    def private_task(request: Request):
        session = require_local_session(request)
        try:
            state = get_workflow().load(session.workflow_task_id)
        except WorkflowError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return _private_state(state, app.state.sip_status_provider().value)

    @app.delete("/local/api/task-data")
    def delete_task_data(request: Request):
        session = require_local_session(request)
        require_csrf(request, session)
        workflow = get_workflow()
        try:
            workflow.delete_task(session.workflow_task_id)
        except WorkflowError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        public_task_id = app.state.public_task_aliases.get(session.workflow_task_id, session.task_id)
        try:
            tasks.update(public_task_id, status="cancelled", phase="deleted", progress=0)
        except KeyError:
            pass
        return {"deleted": True}

    @app.get("/local/api/recovery-tasks")
    def recovery_tasks(request: Request):
        require_local_session(request)
        recoverable = {
            WorkflowPhase.AWAITING_SIP_DISABLED,
            WorkflowPhase.AWAITING_SIP_ENABLED,
            WorkflowPhase.READY_TO_EXPORT,
            WorkflowPhase.COMPLETE,
            WorkflowPhase.PARTIAL,
        }
        return {
            "tasks": [
                {
                    "task_id": state.task_id,
                    "phase": state.phase.value,
                    "account_id": state.account_id,
                    "output_dir": state.output_dir,
                }
                for state in get_workflow().list_states()
                if state.phase in recoverable
            ]
        }

    @app.post("/local/api/resume")
    def resume_workflow(payload: ResumeWorkflowRequest, request: Request):
        session = require_local_session(request)
        require_csrf(request, session)
        try:
            state = get_workflow().load(payload.task_id)
        except WorkflowError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        sessions.bind_workflow(session.token, state.task_id)
        app.state.public_task_aliases[state.task_id] = session.task_id
        update_public_state(state)
        return _private_state(state, app.state.sip_status_provider().value)

    @app.get("/local/api/sessions")
    def local_sessions(request: Request):
        session = require_local_session(request)
        workflow = get_workflow()
        try:
            state = workflow.load(session.workflow_task_id)
        except WorkflowError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        if state.phase not in {
            WorkflowPhase.READY_TO_EXPORT,
            WorkflowPhase.COMPLETE,
            WorkflowPhase.PARTIAL,
        }:
            raise HTTPException(status_code=409, detail="必须先完成解密并重新开启 SIP")
        if app.state.sip_status_provider().value != "enabled":
            raise HTTPException(status_code=409, detail="SIP 未开启，拒绝读取会话")
        catalog = discover_decrypted_catalog(workflow.decrypted_paths(state))
        _contacts, found_sessions = load_session_catalog(catalog)
        return {
            "sessions": [
                {
                    "wxid": item.wxid,
                    "display_name": item.display_name,
                    "is_group": item.is_group,
                    "last_timestamp": item.last_timestamp,
                    "message_count": item.message_count,
                }
                for item in found_sessions
            ]
        }

    @app.post("/local/api/export")
    def start_conversation_export(payload: ExportConversationRequest, request: Request):
        local_session = require_local_session(request)
        require_csrf(request, local_session)
        workflow = get_workflow()
        try:
            state = workflow.load(local_session.workflow_task_id)
        except WorkflowError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        environment, account = account_for_state(state)
        catalog = discover_decrypted_catalog(workflow.decrypted_paths(state))
        _contacts, found_sessions = load_session_catalog(catalog)
        selected = next((item for item in found_sessions if item.wxid == payload.session_wxid), None)
        if selected is None:
            raise HTTPException(status_code=404, detail="所选会话不存在")
        if not catalog.message_databases:
            raise HTTPException(status_code=409, detail="未识别到消息数据库")
        try:
            workflow.start_export(local_session.workflow_task_id)
        except WorkflowError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

        def worker() -> None:
            try:
                result = export_conversation(
                    messages=iter_messages(
                        catalog.message_databases,
                        session_wxid=selected.wxid,
                        owner_wxid=account.owner_wxid,
                    ),
                    output_root=settings.export_root,
                    account_root=account.root,
                    session_wxid=selected.wxid,
                    display_name=selected.display_name,
                    owner_wxid=account.owner_wxid,
                    wechat_version=environment.app_version,
                    schema_fingerprint=account.schema_fingerprint,
                    exporter_version=__version__,
                    media_databases=catalog.media_databases,
                )
                workflow.finish_export(
                    local_session.workflow_task_id,
                    output_dir=result.output_dir,
                    partial=result.status == "partial",
                )
            except Exception as exc:
                workflow.fail(local_session.workflow_task_id, error_code="export_failed", error_detail=str(exc))

        threading.Thread(target=worker, name=f"wechat-export-{local_session.task_id[:8]}", daemon=True).start()
        return {"accepted": True, "task_id": local_session.workflow_task_id}

    return app


def _local_export_page(csrf_token: str, export_root: Path) -> str:
    csrf = escape(csrf_token, quote=True)
    output_path = escape(str(export_root.expanduser()), quote=True)
    page = """<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="ex-memory-csrf" content="__CSRF__"><title>ex-memory 本地微信导出</title>
<style>
:root{color-scheme:dark;--bg-primary:#0a0a12;--bg-secondary:#12121e;--bg-tertiary:#1a1a28;--accent:#7c6cff;--accent-hover:#9b8aff;--wechat-green:#07c160;--text-primary:rgba(255,255,255,.92);--text-secondary:rgba(255,255,255,.68);--text-tertiary:rgba(255,255,255,.46);--border:rgba(124,108,255,.16)}
*{box-sizing:border-box}
body{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;max-width:780px;margin:0 auto;padding:44px 20px 64px;color:var(--text-primary);background:radial-gradient(circle at 50% -10%,rgba(124,108,255,.16),transparent 34%),var(--bg-primary);min-height:100vh;line-height:1.55}
h1{margin:0 0 8px;font-size:clamp(28px,5vw,40px);letter-spacing:-.04em}h2{margin:0 0 14px;font-size:18px}h3{margin:0 0 10px;font-size:15px;color:#c4bcff}body>p{margin:0 0 28px;color:var(--text-secondary)}
.card{border:1px solid var(--border);border-radius:16px;padding:22px;margin:16px 0;background:linear-gradient(145deg,rgba(26,26,40,.92),rgba(18,18,30,.94));box-shadow:0 18px 48px rgba(0,0,0,.22),inset 0 1px rgba(255,255,255,.025)}
.warning{background:linear-gradient(145deg,rgba(73,45,20,.5),rgba(35,25,23,.86));border-color:rgba(245,158,11,.28)}.danger{background:linear-gradient(145deg,rgba(76,29,42,.55),rgba(35,20,30,.9));border-color:rgba(244,63,94,.3)}
button{background:linear-gradient(135deg,var(--accent),#5a4cd4);color:#fff;border:1px solid rgba(255,255,255,.08);border-radius:10px;padding:10px 16px;margin:4px 6px 4px 0;font:inherit;font-weight:600;cursor:pointer;box-shadow:0 8px 20px rgba(90,76,212,.22);transition:transform .18s ease,filter .18s ease,border-color .18s ease}
button:hover:not(:disabled){filter:brightness(1.12);transform:translateY(-1px)}button:focus-visible,select:focus-visible,input:focus-visible{outline:2px solid var(--accent-hover);outline-offset:2px}button:disabled{opacity:.42;cursor:not-allowed;box-shadow:none;transform:none}
#export{background:linear-gradient(135deg,#07c160,#06a855);box-shadow:0 8px 20px rgba(7,193,96,.2)}
select,input{width:100%;padding:11px 12px;margin:8px 0 12px;border:1px solid var(--border);border-radius:10px;font:inherit;color:var(--text-primary);background:rgba(255,255,255,.045);transition:border-color .18s ease,box-shadow .18s ease}select:focus,input:focus{border-color:var(--accent);box-shadow:0 0 0 3px rgba(124,108,255,.12);outline:none}input::placeholder{color:var(--text-tertiary)}option{background:var(--bg-tertiary);color:var(--text-primary)}
code{background:rgba(124,108,255,.12);color:#c4bcff;padding:2px 6px;border:1px solid rgba(124,108,255,.12);border-radius:5px}.status{padding:13px 14px;border-radius:10px;background:rgba(124,108,255,.08);border:1px solid rgba(124,108,255,.12);color:var(--text-secondary);line-height:1.5;overflow-wrap:anywhere}.status.working{color:var(--text-primary)}.status.working::before{content:'';display:inline-block;width:13px;height:13px;margin-right:9px;border:2px solid rgba(124,108,255,.25);border-top-color:#7c6cff;border-radius:50%;animation:status-spin .8s linear infinite;vertical-align:-2px}@keyframes status-spin{to{transform:rotate(360deg)}}.status.danger{background:rgba(244,63,94,.1);border-color:rgba(244,63,94,.32);color:#fda4af}.failed-hint{margin-top:12px;padding:14px;border:1px solid rgba(244,63,94,.3);border-radius:11px;background:rgba(76,29,42,.35)}.failed-hint h3{margin:0 0 8px;color:#fda4af}.failed-detail{margin:0 0 10px;color:var(--text-secondary);overflow-wrap:anywhere}.failed-steps{margin:0 0 10px;padding-left:20px;color:var(--text-secondary)}.failed-steps li+li{margin-top:4px}
.shutdown-guide{margin:14px 0 18px;padding:16px;border:1px solid rgba(245,158,11,.24);border-radius:13px;background:rgba(245,158,11,.055)}.photo-reminder{margin:0 0 14px;padding:11px 12px;border-radius:9px;background:rgba(245,158,11,.13);color:#fcd69a;font-weight:700}.chip-grid{display:grid;grid-template-columns:1fr 1fr;gap:12px}.chip-card{padding:14px;border:1px solid var(--border);border-radius:11px;background:rgba(10,10,18,.42)}.chip-card ol{margin:0;padding-left:20px;color:var(--text-secondary)}.chip-card li+li{margin-top:6px}.command-box{display:flex;align-items:center;justify-content:space-between;gap:12px;margin:14px 0 10px;padding:12px 14px;border:1px solid rgba(124,108,255,.24);border-radius:10px;background:rgba(124,108,255,.09)}.command-box code{font-size:15px;font-weight:700;user-select:all}.safety-note{color:#fda4af;font-weight:650}
.terminal-example{max-width:760px;margin:12px auto 16px}.terminal-shot{display:block;width:100%;height:auto;border:1px solid rgba(124,108,255,.24);border-radius:12px;background:#08090e;box-shadow:0 16px 38px rgba(0,0,0,.32)}.terminal-example figcaption{margin-top:7px;color:var(--text-tertiary);font-size:11px;text-align:center}
.task{display:flex;justify-content:space-between;gap:10px;align-items:center;padding:10px 0;border-top:1px solid var(--border)}.hidden{display:none}.sessions{max-height:360px;overflow:auto;padding-right:3px}.session{display:block;width:100%;text-align:left;background:rgba(255,255,255,.035);color:var(--text-primary);box-shadow:none;border-color:rgba(255,255,255,.055)}.session:hover:not(:disabled){background:rgba(124,108,255,.1)}.session.selected{outline:2px solid var(--accent);background:rgba(124,108,255,.14)}.muted{color:var(--text-tertiary);font-size:13px;overflow-wrap:anywhere}
::-webkit-scrollbar{width:8px}::-webkit-scrollbar-track{background:transparent}::-webkit-scrollbar-thumb{background:rgba(124,108,255,.24);border-radius:999px}
@media (max-width:600px){body{padding:28px 14px 44px}.card{padding:17px;border-radius:14px}.chip-grid{grid-template-columns:1fr}.command-box{align-items:flex-start;flex-direction:column}button{width:100%;margin:5px 0}.task{align-items:stretch;flex-direction:column}}
@media (prefers-reduced-motion:reduce){*,*::before,*::after{scroll-behavior:auto!important;transition:none!important}button:hover:not(:disabled){transform:none}.status.working::before{animation:none}}
</style></head>
<body><h1>本地微信聊天导出</h1><p>联系人、聊天正文、数据库密钥和媒体始终只在这台 Mac 上处理。</p>
<section class="card"><h2>1. 环境与当前账号</h2><div id="environment">正在检查……</div><select id="account"><option value="">选择当前登录的微信账号</option></select><div class="warning" style="padding:14px;border-radius:10px;margin:10px 0"><strong>每个微信账号的数据库密钥不同</strong><p>首次导入、切换账号、助手重启、微信升级或密钥验证失败时，都必须重新提取。一次成功提取只覆盖当前账号的全部数据库，其他账号需要分别登录并分别提取。</p><label><input id="key-rules-confirmed" type="checkbox" style="width:auto;margin-right:8px">我已确认所选账号就是微信当前登录账号，并理解切换账号后必须重新提取</label></div><button id="prepare">开始专家流程</button></section>
<section id="request-failure" class="card danger hidden"><h2>请求失败处理方法</h2><p id="request-error"></p><div id="request-permission-steps" class="hidden"><p>请打开“系统设置 → 隐私与安全性 → 完全磁盘访问”，然后：</p><p>1. 点击 +</p><p>2. 选择 /Applications/ex-memory 微信导出助手.app</p><p>3. 打开它右侧的权限开关</p><p>4. 如果系统要求，输入 Mac 密码</p><p class="muted">授权后重新启动助手并刷新当前页面。</p></div></section>
<section id="recovery" class="card hidden"><h2>发现可恢复任务</h2><div id="recovery-list"></div></section>
<section class="card warning"><h2>专家实验模式</h2><p>微信 4.1.12 的本地数据库受保护。提取前需要完全退出微信；开始提取后再启动并登录刚才确认的账号。取得密钥并生成解密快照后，必须重新开启 SIP 并重启，才能选择联系人和生成 HTML。</p><p><strong>不要在 SIP 关闭期间浏览网页、安装软件或继续导出。</strong></p></section>
<section class="card"><h2>2. 当前任务</h2><div id="status" class="status">尚未开始</div><div id="failed-hint" class="hidden"><h3>任务失败</h3><p id="failed-detail" class="failed-detail"></p><p>请检查：</p><ol class="failed-steps"><li>微信版本是否为当前已验证的 4.1.12。</li><li>弹出管理员授权框时是否已输入 Mac 密码。</li><li>SIP 是否确实已关闭（终端执行 <code>csrutil status</code> 显示 disabled）。</li><li>微信当前登录账号是否与所选账号一致；不同账号必须重新提取。</li></ol><p class="muted">修正后请先重新开启 SIP 并重启，再从头开始专家流程。</p></div>
<div id="disable-actions" class="hidden"><div class="shutdown-guide"><p class="photo-reminder">📱 接下来需要关机操作，请先用手机拍照保存本页步骤。</p><div class="chip-grid"><section class="chip-card"><h3>Apple 芯片 Mac</h3><ol><li>保存工作，将 Mac 完全关机。</li><li>长按电源键，直到出现启动选项。</li><li>选择“选项”，点击“继续”；如有要求，选择管理员并输入登录密码。</li><li>从屏幕顶部选择“实用工具 → 终端”。</li><li>执行 <code>csrutil disable</code> 并按回车。</li><li>出现确认提示后输入 <code>y</code> 并按回车。</li><li>按提示输入 Mac 管理员用户名并按回车。</li><li>输入该管理员的登录密码并按回车；终端不会显示输入的字符，这是正常现象。</li><li>看到成功提示后，从 Apple 菜单选择“重新启动”。</li></ol></section><section class="chip-card"><h3>Intel Mac</h3><ol><li>保存工作，将 Mac 完全关机。</li><li>按下电源键，开机后立即长按 ⌘R，直到出现 Apple 标志或旋转地球。</li><li>如有要求，选择管理员并输入登录密码。</li><li>从屏幕顶部选择“实用工具 → 终端”。</li><li>执行 <code>csrutil disable</code> 并按回车。</li><li>如终端要求确认或认证，按屏幕提示完成；输入密码时不会显示字符。</li><li>看到成功提示后，从 Apple 菜单选择“重新启动”。</li></ol></section></div><div class="command-box"><strong>在恢复模式终端执行</strong><code>csrutil disable</code></div>__DISABLE_TERMINAL__<p>命令完成后，从 Apple 菜单选择“重新启动”。回到系统后可先在“终端”执行 <code>csrutil status</code> 核对：显示 <code>disabled</code> 即已关闭。然后启动并登录微信，再点击下方按钮。</p><p>特殊情况：若提示 <code>Failed to create local policy</code>，先执行 <code>csrutil clear</code>，从 Apple 菜单重启后再次进入恢复模式重试。</p><p class="safety-note">SIP 关闭期间不要浏览网页、安装软件或运行不可信程序。</p></div><button id="decrypt">我已关闭 SIP，开始提取和解密</button><p class="muted">助手会再次检测 SIP。出现“请完全退出微信”后，请用微信菜单退出，不要强制结束本地助手。</p></div>
<div id="enable-actions" class="hidden danger"><div class="shutdown-guide"><p class="photo-reminder">📱 解密快照已生成。请先用手机拍照保存本页步骤，然后立即重新开启 SIP。</p><div class="chip-grid"><section class="chip-card"><h3>Apple 芯片 Mac</h3><ol><li>将 Mac 完全关机。</li><li>长按电源键，直到出现启动选项。</li><li>选择“选项”，点击“继续”；如有要求，选择管理员并输入登录密码。</li><li>从屏幕顶部选择“实用工具 → 终端”。</li><li>执行 <code>csrutil enable</code> 并按回车。</li><li>如终端提示认证，输入管理员用户名和登录密码并按回车；密码不会显示字符。</li><li>看到成功提示后，从 Apple 菜单选择“重新启动”。</li></ol></section><section class="chip-card"><h3>Intel Mac</h3><ol><li>将 Mac 完全关机。</li><li>按下电源键，开机后立即长按 ⌘R，直到出现 Apple 标志或旋转地球。</li><li>如有要求，选择管理员并输入登录密码。</li><li>从屏幕顶部选择“实用工具 → 终端”。</li><li>执行 <code>csrutil enable</code> 并按回车。</li><li>如终端要求确认或认证，按屏幕提示完成；输入密码时不会显示字符。</li><li>看到成功提示后，从 Apple 菜单选择“重新启动”。</li></ol></section></div><div class="command-box"><strong>在恢复模式终端执行</strong><code>csrutil enable</code></div>__ENABLE_TERMINAL__<p>命令完成后，从 Apple 菜单选择“重新启动”。回到系统后可先在“终端”执行 <code>csrutil status</code> 核对：显示 <code>enabled</code> 即已开启。然后点击下方按钮。</p><p>特殊情况：若提示 <code>Failed to create local policy</code>，先执行 <code>csrutil clear</code>，从 Apple 菜单重启后再次进入恢复模式重试。</p><p class="safety-note">重新开启 SIP 不能撤销关闭期间已经发生的系统修改或数据泄露。</p></div><button id="authorize">我已重新开启 SIP，继续导出</button><p class="muted">助手会再次检测 SIP；确认已开启后才允许读取聊天和生成 HTML。</p></div></section>
<section id="session-card" class="card hidden"><h2>3. 选择联系人并导出</h2><input id="session-search" placeholder="搜索备注、昵称或 wxid"><div id="sessions" class="sessions"></div><button id="export" disabled>导出全部聊天为 HTML</button><p class="muted">输出位置：<code>__EXPORT_ROOT__</code>；视频、音频和其他资源按类别单独存放。</p></section>
<section id="task-data-actions" class="card hidden"><h2>4. 清理本地任务数据</h2><p>删除当前任务的状态和解密数据库快照。已生成到输出目录的 HTML 不受影响。</p><button id="delete-task-data">删除本地任务数据</button><p class="muted">此操作不可恢复；正在提取、解密或导出时不能删除。</p></section>
<script>
const csrf=document.querySelector('meta[name="ex-memory-csrf"]').content;
const elements=Object.fromEntries(['environment','account','key-rules-confirmed','prepare','request-failure','request-error','request-permission-steps','recovery','recovery-list','status','disable-actions','decrypt','enable-actions','authorize','session-card','session-search','sessions','export','failed-hint','failed-detail','task-data-actions','delete-task-data'].map(id=>[id,document.getElementById(id)]));
let allSessions=[];let selected='';let pollTimer;
async function api(path,options={}){try{const headers={...(options.headers||{})};if(options.method&&options.method!=='GET')headers['X-Ex-Memory-CSRF']=csrf;if(options.body)headers['Content-Type']='application/json';const response=await fetch(path,{...options,headers});const data=await response.json().catch(()=>({}));if(!response.ok)throw new Error(data.detail||'请求失败');return data}catch(error){renderRequestFailure(error);throw error}}
function renderRequestFailure(error,fatal=false){const message=error?.message||'请求失败';elements['request-error'].textContent=message;elements['request-failure'].classList.remove('hidden');elements['request-permission-steps'].classList.toggle('hidden',!message.includes('完全磁盘访问'));if(fatal)for(const button of [elements.prepare,elements.decrypt,elements.authorize,elements.export])button.disabled=true}
async function initialize(){try{const [environment,recovery]=await Promise.all([api('/local/api/environment'),api('/local/api/recovery-tasks')]);if(!environment.data_accessible){renderRequestFailure(new Error('本地助手没有“完全磁盘访问”权限。'),true)}else{elements.environment.textContent=`微信 ${environment.wechat_version||'未检测到'} · SIP ${environment.sip_status} · 发现 ${environment.accounts.length} 个账号目录`;environment.accounts.forEach((item,index)=>{const option=document.createElement('option');const isCurrent=item.account_id===environment.current_account_id;option.value=item.account_id;option.textContent=`账号 ${index+1}${isCurrent?'（当前登录）':''} · ${item.database_count} 个数据库`;elements.account.append(option)});if(!environment.supported_wechat_versions.includes(environment.wechat_version)){elements.prepare.disabled=true;elements.status.textContent=`当前微信 ${environment.wechat_version||'未检测到'} 尚未支持，为保护本机数据已停止。此助手仅验证 ${environment.supported_wechat_versions.join('、')}；请等待兼容版本，不要关闭 SIP。`}else if(environment.current_account_id&&environment.accounts.some(item=>item.account_id===environment.current_account_id)){elements.account.value=environment.current_account_id;elements.status.textContent='已自动选择当前登录的微信账号，请核对后继续。'}else if(environment.accounts.length){elements.status.textContent='已发现微信账号目录，但无法唯一判断当前登录账号，请在下拉列表手动选择。'}else{elements.prepare.disabled=true;elements.status.textContent='未发现微信账号目录。请先安装并登录微信 4.1.12，然后重新启动助手。'}}renderRecovery(recovery.tasks)}catch(error){renderRequestFailure(error,true)}}
function renderRecovery(tasks){if(!tasks.length)return;elements.recovery.classList.remove('hidden');for(const item of tasks){const row=document.createElement('div');row.className='task';const label=document.createElement('span');label.textContent=`${item.account_id} · ${phaseText(item.phase)}`;const button=document.createElement('button');button.textContent='恢复';button.onclick=async()=>{await api('/local/api/resume',{method:'POST',body:JSON.stringify({task_id:item.task_id})});startPolling()};row.append(label,button);elements['recovery-list'].append(row)}}
elements.prepare.onclick=async()=>{if(!elements.account.value){renderRequestFailure(new Error('请先选择当前登录的微信账号'));return}if(!elements['key-rules-confirmed'].checked){renderRequestFailure(new Error('请先确认每个微信账号密钥不同，并确认当前登录账号'));return}try{elements['request-failure'].classList.add('hidden');await api('/local/api/expert/prepare',{method:'POST',body:JSON.stringify({account_id:elements.account.value,key_rules_confirmed:true})});startPolling()}catch(error){renderRequestFailure(error)}};
elements.decrypt.onclick=async()=>{try{await api('/local/api/expert/decrypt',{method:'POST',body:'{}'});startPolling()}catch(error){renderRequestFailure(error)}};
elements.authorize.onclick=async()=>{try{await api('/local/api/expert/authorize-export',{method:'POST',body:'{}'});startPolling()}catch(error){renderRequestFailure(error)}};
elements.export.onclick=async()=>{if(!selected)return;try{await api('/local/api/export',{method:'POST',body:JSON.stringify({session_wxid:selected})});elements.export.disabled=true;startPolling()}catch(error){renderRequestFailure(error)}};
elements['delete-task-data'].onclick=async()=>{if(!window.confirm('确定删除当前任务状态和全部解密数据库快照吗？此操作不可恢复。'))return;try{await api('/local/api/task-data',{method:'DELETE'});clearTimeout(pollTimer);elements.status.textContent='本地任务数据已删除；已导出的 HTML 不受影响';elements['task-data-actions'].classList.add('hidden');elements['session-card'].classList.add('hidden')}catch(error){renderRequestFailure(error)}};
elements['session-search'].oninput=renderSessions;
function startPolling(){clearTimeout(pollTimer);poll()}
async function poll(){try{const task=await api('/local/api/task');renderTask(task);if(!['complete','partial','failed'].includes(task.phase))pollTimer=setTimeout(poll,1500)}catch(error){renderRequestFailure(error)} }
function renderTask(task){elements.status.textContent=`${phaseText(task.phase)} · SIP ${task.sip_status}${task.error_detail?' · '+task.error_detail:''}${task.output_dir?' · '+task.output_dir:''}`;elements.status.classList.toggle('working',['extracting_keys','decrypting','exporting'].includes(task.phase));elements.status.classList.toggle('danger',task.phase==='failed');elements['disable-actions'].classList.toggle('hidden',task.phase!=='awaiting_sip_disabled');elements['enable-actions'].classList.toggle('hidden',task.phase!=='awaiting_sip_enabled');elements['failed-hint'].classList.toggle('hidden',task.phase!=='failed');elements['task-data-actions'].classList.toggle('hidden',!['awaiting_sip_disabled','awaiting_sip_enabled','ready_to_export','complete','partial','failed'].includes(task.phase));if(task.phase==='failed')elements['failed-detail'].textContent=task.error_detail||'未知错误';if(task.phase==='ready_to_export')loadSessions();if(['complete','partial'].includes(task.phase))elements['session-card'].classList.remove('hidden')}
function phaseText(phase){return({awaiting_sip_disabled:'等待手动关闭 SIP；提取前请完全退出微信',extracting_keys:'等待启动微信并登录已确认的当前账号；正在捕获并验证该账号全部数据库',awaiting_wechat_exit:'当前账号全库密钥已验证，请现在完全退出微信',decrypting:'微信已退出，正在逐库解密并校验 sqlite_master',awaiting_sip_enabled:'当前账号解密完成；其他账号需分别登录并重新提取。必须立即重新开启 SIP',ready_to_export:'SIP 已开启，可以选择当前账号联系人',exporting:'正在生成完整 HTML 和分类资源，请稍候',complete:'当前账号导出完成；其他账号必须分别提取',partial:'当前账号导出完成，部分资源缺失；其他账号必须分别提取',failed:'任务失败'})[phase]||phase}
async function loadSessions(){elements['session-card'].classList.remove('hidden');if(allSessions.length)return;try{allSessions=(await api('/local/api/sessions')).sessions;renderSessions()}catch(error){renderRequestFailure(error)}}
function renderSessions(){const query=elements['session-search'].value.trim().toLowerCase();elements.sessions.replaceChildren();for(const item of allSessions.filter(item=>`${item.display_name} ${item.wxid}`.toLowerCase().includes(query)).slice(0,500)){const button=document.createElement('button');button.className='session'+(selected===item.wxid?' selected':'');button.textContent=`${item.display_name} · ${item.message_count||'未知'} 条 · ${item.wxid}`;button.onclick=()=>{selected=item.wxid;elements.export.disabled=false;renderSessions()};elements.sessions.append(button)}}
initialize();
</script></body></html>"""
    return (
        page.replace("__CSRF__", csrf)
        .replace("__EXPORT_ROOT__", output_path)
        .replace("__DISABLE_TERMINAL__", _terminal_result_figure("disable"))
        .replace("__ENABLE_TERMINAL__", _terminal_result_figure("enable"))
        .replace("然后启动并登录微信，再点击下方按钮。", "点击下方按钮前，请先从微信菜单完全退出微信。")
        .replace("我已关闭 SIP，开始提取和解密", "微信已完全退出，开始等待提取")
        .replace(
            "助手会再次检测 SIP。出现“请完全退出微信”后，请用微信菜单退出，不要强制结束本地助手。",
            "点击后再启动微信并登录已确认的当前账号。全库密钥验证完成后，助手会再次提示退出微信。",
        )
    )


def _terminal_result_figure(action: str) -> str:
    command = f"csrutil {action}"
    action_word = "disabled" if action == "disable" else "enabled"

    if action == "disable":
        apple_rows = [
            ("cmd", command),
            ("txt", "This will disable System Integrity Protection. Are you sure you want to continue? [y/N]: y"),
            ("txt", "Enter your username: your_username"),
            ("pwd", "Enter your password:"),
            ("ok", f"Successfully {action_word} System Integrity Protection."),
            ("txt", "Please restart the machine for the changes to take effect."),
            ("ps1", ""),
        ]
        apple_note = "输入密码时屏幕不会显示任何字符，这是正常现象。"
        apple_aria = "csrutil disable 在 Apple 芯片 Mac 上执行后的完整交互示意图"
    else:
        apple_rows = [
            ("cmd", command),
            ("ok", f"Successfully {action_word} System Integrity Protection."),
            ("txt", "Please restart the machine for the changes to take effect."),
            ("ps1", ""),
        ]
        apple_note = "如提示认证，输入管理员用户名和密码；密码不会显示字符。"
        apple_aria = "csrutil enable 在 Apple 芯片 Mac 上执行后的恢复模式终端示意图"

    intel_rows = [
        ("cmd", command),
        ("ok", f"Successfully {action_word} System Integrity Protection."),
        ("txt", "Please restart the machine for the changes to take effect."),
        ("ps1", ""),
    ]
    intel_note = "如出现确认或认证提示，按屏幕提示完成；密码不会显示字符。"

    return _terminal_svg("Terminal — Recovery · Apple 芯片", apple_rows, apple_note, apple_aria) + _terminal_svg(
        "Terminal — Recovery · Intel Mac",
        intel_rows,
        intel_note,
        f"{command} 在 Intel Mac 上执行后的恢复模式终端示意图",
    )


def _terminal_svg(title: str, rows: list[tuple[str, str]], note: str, aria_label: str) -> str:
    top = 82
    line_h = 27
    height = top + len(rows) * line_h + 30
    parts = []
    y = top
    for kind, text in rows:
        if kind == "cmd":
            parts.append(f'<text x="24" y="{y}" fill="#78dba9">bash-3.2#</text><text x="124" y="{y}" fill="#f3f0fa">{text}</text>')
        elif kind == "ok":
            parts.append(f'<text x="24" y="{y}" fill="#78dba9">{text}</text>')
        elif kind == "pwd":
            parts.append(
                f'<text x="24" y="{y}" fill="#c9c5d2">{text}</text>'
                f'<rect x="210" y="{y - 13}" width="9" height="17" rx="1" fill="#c4bcff" opacity=".9"/>'
                f'<text x="228" y="{y}" fill="#6f6a7c">（不显示字符）</text>'
            )
        elif kind == "ps1":
            parts.append(f'<text x="24" y="{y}" fill="#78dba9">bash-3.2#</text><rect x="124" y="{y - 13}" width="9" height="17" rx="1" fill="#c4bcff" opacity=".9"/>')
        else:
            parts.append(f'<text x="24" y="{y}" fill="#c9c5d2">{text}</text>')
        y += line_h
    note_y = height - 12
    body = "".join(parts)
    return f"""<figure class="terminal-example">
<svg class="terminal-shot" viewBox="0 0 760 {height}" role="img" aria-label="{aria_label}" xmlns="http://www.w3.org/2000/svg">
<rect width="760" height="{height}" rx="12" fill="#08090e"/><path d="M12 0h736a12 12 0 0 1 12 12v34H0V12A12 12 0 0 1 12 0Z" fill="#181a24"/>
<circle cx="24" cy="23" r="6" fill="#ff5f57"/><circle cx="44" cy="23" r="6" fill="#febc2e"/><circle cx="64" cy="23" r="6" fill="#28c840"/>
<text x="380" y="28" text-anchor="middle" fill="#9b96aa" font-family="-apple-system,BlinkMacSystemFont,sans-serif" font-size="13">{title}</text>
<g font-family="SFMono-Regular,Menlo,Monaco,monospace" font-size="15">{body}</g>
<text x="24" y="{note_y}" fill="#6f6a7c" font-family="-apple-system,BlinkMacSystemFont,sans-serif" font-size="12">{note}</text>
</svg>
<figcaption>终端示意图；不同 macOS 版本的提示文字和窗口外观可能略有差异。</figcaption>
</figure>"""


def _public_workflow_status(phase: WorkflowPhase) -> tuple[str, int]:
    mapping = {
        WorkflowPhase.AWAITING_SIP_DISABLED: ("running", 10),
        WorkflowPhase.EXTRACTING_KEYS: ("running", 20),
        WorkflowPhase.AWAITING_WECHAT_EXIT: ("running", 30),
        WorkflowPhase.DECRYPTING: ("running", 50),
        WorkflowPhase.AWAITING_SIP_ENABLED: ("running", 65),
        WorkflowPhase.READY_TO_EXPORT: ("running", 75),
        WorkflowPhase.EXPORTING: ("running", 85),
        WorkflowPhase.COMPLETE: ("success", 100),
        WorkflowPhase.PARTIAL: ("partial", 100),
        WorkflowPhase.FAILED: ("failed", 0),
    }
    return mapping[phase]


def _private_state(state: WorkflowState, sip_status: str) -> dict:
    return {
        "task_id": state.task_id,
        "phase": state.phase.value,
        "sip_status": sip_status,
        "error_code": state.error_code,
        "error_detail": state.error_detail,
        "output_dir": state.output_dir,
    }


def _decryption_error_detail(exc: Exception, app_version: str) -> str:
    message = str(exc) or exc.__class__.__name__
    if "密钥" in message or "key" in message.lower():
        return (
            f"{message}。微信 {app_version or '当前版本'} 的密钥驻留方式可能已变化，"
            "当前内置提取器暂不支持该版本；请改用受支持的微信版本后重试。"
        )
    suffix = f"（微信 {app_version}）" if app_version else ""
    return f"{message}{suffix}"
