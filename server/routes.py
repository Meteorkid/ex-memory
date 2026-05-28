"""REST API 路由。"""

import json
import logging
import threading
from pathlib import Path
from fastapi import APIRouter, HTTPException, UploadFile, File, Form, Depends, Request, Query
from fastapi.responses import StreamingResponse
from starlette.concurrency import run_in_threadpool

from config import EXES_DIR, get_ex_dir, get_collection_name, DISABLE_REGISTRATION
from core.validation import validate_slug, validate_user_input, sanitize_chat_history
from core.exe_access import assert_exe_access, set_owner_user_id, iter_accessible_exes
from core.path_safety import safe_filename
from core.file_utils import atomic_write, atomic_write_json
from core.token_counter import TokenCounter
from core.logging import get_audit_logger
from server.middleware import require_auth, _get_client_ip, security
from fastapi.security import HTTPAuthorizationCredentials
from server.models import (
    CreateRequest, ResumeRequest, ChatRequest, UpdateRequest,
    BackupRequest, RollbackRequest, DeleteRequest,
    ExeInfo, ChatResponse, StatusResponse, ErrorResponse, AuthRequest, LogoutRequest,
    TransferRequest, TransferConfirmRequest,
)

logger = logging.getLogger("ex-memory")
router = APIRouter(prefix="/api")
_INTERNAL_ERROR = "服务器内部错误，请稍后重试"

# 服务端 session 级 token 累计计数器（内存存储，重启后清零）
_session_counters: dict[tuple[int, str], TokenCounter] = {}
_counter_lock = threading.Lock()

# Engine 缓存（避免每次请求重建 SKILL.md / ChromaDB 连接）
_engine_cache: dict[str, object] = {}
_engine_cache_lock = threading.Lock()

# 登录限流 + 审计日志
_login_limiter = None
_audit_logger = None


def _get_login_limiter():
    global _login_limiter
    if _login_limiter is None:
        from server.middleware import LoginRateLimiter
        _login_limiter = LoginRateLimiter()
    return _login_limiter


def _get_audit():
    global _audit_logger
    if _audit_logger is None:
        _audit_logger = get_audit_logger()
    return _audit_logger


def _audit(event: str, username: str = "", client_ip: str = "", detail: str = ""):
    """记录审计事件（不会因审计日志写入失败影响主流程）。"""
    try:
        _get_audit().info(json.dumps({
            "event": event,
            "username": username,
            "ip": client_ip,
            "detail": detail,
        }, ensure_ascii=False))
    except Exception:
        pass


def _get_engine(slug: str):
    """获取或创建 ChatEngine（带缓存，锁内 double-check）。"""
    with _engine_cache_lock:
        engine = _engine_cache.get(slug)
        if engine is not None:
            return engine
    from core.factory import create_engine_and_store
    engine, _, _ = create_engine_and_store(slug)
    with _engine_cache_lock:
        if slug not in _engine_cache:
            _engine_cache[slug] = engine
        return _engine_cache[slug]


def _check_exe_access(slug: str, user_id: int) -> str:
    try:
        slug = validate_slug(slug)
        assert_exe_access(slug, user_id)
        return slug
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    except PermissionError as e:
        raise HTTPException(status_code=403, detail=str(e)) from e


def _copy_upload_limited(src, dest, max_bytes: int) -> int:
    total = 0
    while True:
        chunk = src.read(1024 * 1024)
        if not chunk:
            break
        total += len(chunk)
        if total > max_bytes:
            raise ValueError(f"文件过大，最大支持 {max_bytes // (1024 * 1024)}MB")
        dest.write(chunk)
    return total


def _invalidate_engine(slug: str):
    """使缓存的 engine 失效（纠正/更新 SKILL.md 后调用）。"""
    with _engine_cache_lock:
        _engine_cache.pop(slug, None)


# --- 用户认证 ---

@router.post("/auth/register", response_model=StatusResponse)
def register(req: AuthRequest, request: Request = None):
    """注册新用户。"""
    if DISABLE_REGISTRATION:
        raise HTTPException(status_code=403, detail="注册已关闭")
    client_ip = _get_client_ip(request) if request else "unknown"
    _get_login_limiter().check(req.username, client_ip)
    from server.auth import register_user
    error = register_user(req.username, req.password)
    if error:
        _audit("register_failed", username=req.username, client_ip=client_ip, detail=error)
        raise HTTPException(status_code=400, detail=error)
    _audit("register_success", username=req.username, client_ip=client_ip)
    return StatusResponse(message="注册成功，请登录")


@router.post("/auth/login")
def login(req: AuthRequest, request: Request = None):
    """登录获取 token。"""
    client_ip = _get_client_ip(request) if request else "unknown"
    _get_login_limiter().check(req.username, client_ip)

    from server.auth import login_user
    token = login_user(req.username, req.password)
    if token is None:
        _audit("login_failed", username=req.username, client_ip=client_ip)
        raise HTTPException(status_code=401, detail="用户名或密码错误")
    _audit("login_success", username=req.username, client_ip=client_ip)
    return {"token": token, "token_type": "bearer"}


@router.post("/auth/logout", response_model=StatusResponse)
def logout(
    credentials: HTTPAuthorizationCredentials = Depends(security),
    user_id: int = Depends(require_auth),
):
    """注销当前 Bearer token。"""
    from server.auth import revoke_token
    if credentials:
        revoke_token(credentials.credentials)
    return StatusResponse(message="已注销")


# --- 镜像管理 ---

@router.get("/exes", response_model=list[ExeInfo])
def list_exes(user_id: int = Depends(require_auth)):
    """列出当前用户可访问的镜像。"""
    exes = []
    for d in iter_accessible_exes(user_id):
        meta_path = d / "meta.json"
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            exes.append(ExeInfo(
                slug=d.name,
                name=meta.get("name", d.name),
                state=meta.get("pipeline_state", "unknown"),
                created_at=meta.get("created_at", ""),
                updated_at=meta.get("updated_at"),
            ))
        except Exception:
            pass
    return sorted(exes, key=lambda e: e.created_at, reverse=True)


@router.post("/exes", response_model=StatusResponse)
def create_exe(req: CreateRequest, user_id: int = Depends(require_auth)):
    """创建新镜像。"""
    try:
        slug = validate_slug(req.slug)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    ex_dir = get_ex_dir(slug)
    if ex_dir.exists():
        raise HTTPException(status_code=409, detail=f"镜像 [{slug}] 已存在")

    from pipeline.orchestrator import run_create_flow_api
    result = run_create_flow_api(
        slug=slug, name=req.name, answers=req.answers, owner_user_id=user_id,
    )
    if result.get("error"):
        raise HTTPException(status_code=500, detail=result["error"])
    try:
        set_owner_user_id(slug, user_id)
    except Exception:
        pass
    return StatusResponse(message=f"镜像 [{slug}] 创建成功")


@router.post("/exes/{slug}/resume", response_model=StatusResponse)
def resume_exe(slug: str, req: ResumeRequest, user_id: int = Depends(require_auth)):
    """从上次失败步骤恢复创建。"""
    slug = _check_exe_access(slug, user_id)

    from pipeline.orchestrator import run_create_flow_api
    result = run_create_flow_api(slug=slug, name=req.name, answers=[], resume=True)
    if result.get("error"):
        raise HTTPException(status_code=500, detail=result["error"])
    return StatusResponse(message=f"镜像 [{slug}] 恢复创建成功")


@router.delete("/exes/{slug}", response_model=StatusResponse)
def delete_exe(slug: str, req: DeleteRequest, user_id: int = Depends(require_auth)):
    """删除镜像。"""
    if not req.confirm:
        raise HTTPException(status_code=400, detail="需要确认删除")
    slug = _check_exe_access(slug, user_id)
    ex_dir = get_ex_dir(slug)
    import shutil
    shutil.rmtree(ex_dir)
    _audit("exe_deleted", username=f"user_id={user_id}", detail=f"slug={slug}")
    return StatusResponse(message=f"镜像 [{slug}] 已删除")


# --- 数据导入 ---

MAX_UPLOAD_SIZE = 100 * 1024 * 1024  # 100MB


@router.post("/exes/{slug}/import", response_model=StatusResponse)
async def import_data(slug: str, file: UploadFile = File(...), target_name: str = Form(""), user_id: int = Depends(require_auth)):
    """导入聊天记录数据源（自动检测微信/QQ 格式）。"""
    slug = _check_exe_access(slug, user_id)
    ex_dir = get_ex_dir(slug)

    if file.size is not None and file.size > MAX_UPLOAD_SIZE:
        raise HTTPException(status_code=413, detail="文件过大，最大支持 100MB")

    import tempfile
    import shutil

    try:
        safe_name = safe_filename(file.filename or "upload.dat")
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e

    tmp_dir = Path(tempfile.mkdtemp())
    tmp_path = tmp_dir / safe_name

    try:
        with open(tmp_path, "wb") as f:
            try:
                _copy_upload_limited(file.file, f, MAX_UPLOAD_SIZE)
            except ValueError as e:
                raise HTTPException(status_code=413, detail=str(e)) from e
        _invalidate_engine(slug)

        from config import get_embedding_config, get_collection_name
        emb_cfg = get_embedding_config()
        if not emb_cfg["api_key"]:
            raise HTTPException(status_code=500, detail="未配置 Embedding API Key")

        from memory.embedder import Embedder
        from memory.vector_store import VectorStore

        embedder = Embedder(api_key=emb_cfg["api_key"], base_url=emb_cfg["base_url"], model=emb_cfg["model"])
        vector_store = VectorStore(
            persist_dir=str(ex_dir / "chroma_db"),
            collection_name=get_collection_name(slug),
        )

        # 根据文件扩展名自动选择解析器
        ext = Path(safe_name).suffix.lower()
        if ext == ".mht" or ext == ".mhtml":
            from memory.ingest import ingest_qq_file
            messages, chunk_count = ingest_qq_file(str(tmp_path), slug, target_name, vector_store, embedder)
        elif ext == ".txt":
            # TXT 需要检测是微信还是 QQ 格式
            from parsers.wechat_parser import detect_format
            fmt = detect_format(str(tmp_path))
            if fmt == "plaintext":
                # 微信无法识别，尝试 QQ
                from memory.ingest import ingest_qq_file
                messages, chunk_count = ingest_qq_file(str(tmp_path), slug, target_name, vector_store, embedder)
            else:
                from memory.ingest import ingest_wechat_file
                messages, chunk_count = ingest_wechat_file(str(tmp_path), slug, target_name, vector_store, embedder)
        else:
            # JSON/JSONL 默认微信
            from memory.ingest import ingest_wechat_file
            messages, chunk_count = ingest_wechat_file(str(tmp_path), slug, target_name, vector_store, embedder)

        if not messages:
            return StatusResponse(message="未提取到有效消息")

        return StatusResponse(
            message=f"导入完成：解析 {len(messages)} 条消息，入库 {chunk_count} 个切片"
        )
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


# --- 贴纸 ---

@router.get("/stickers")
def list_all_stickers(
    category: str = Query("all", description="过滤分类"),
    user_id: int = Depends(require_auth),
):
    """返回所有可用贴纸（emoji + 图片 + GIF）。"""
    from core.sticker_selector import get_all_stickers
    from core.sticker_manager import list_stickers as list_image_stickers
    emoji_stickers = get_all_stickers()
    image_stickers = list_image_stickers(category=category, user_id=user_id)
    # emoji 贴纸始终返回（不过滤分类），图片贴纸按 category 过滤
    if category in ("all", "emoji"):
        combined = emoji_stickers + image_stickers
    elif category == "custom":
        combined = image_stickers
    else:
        combined = [s for s in emoji_stickers if s.get("emotion") == category] + image_stickers
    return {"stickers": combined}


@router.get("/stickers/{sticker_id}")
def get_sticker_route(sticker_id: str, user_id: int = Depends(require_auth)):
    """获取单个贴纸信息。"""
    from core.sticker_selector import STICKERS
    from core.sticker_manager import get_sticker as get_image_sticker
    if sticker_id in STICKERS:
        s = STICKERS[sticker_id]
        return {"id": sticker_id, "type": "emoji", "emoji": s["emoji"], "label": s["label"], "category": s["emotion"]}
    sticker = get_image_sticker(sticker_id, user_id=user_id)
    if sticker:
        return sticker
    raise HTTPException(status_code=404, detail="贴纸不存在")


@router.post("/stickers/upload")
async def upload_sticker(
    file: UploadFile = File(...),
    label: str = Form(""),
    category: str = Form("custom"),
    user_id: int = Depends(require_auth),
):
    """上传自定义贴纸。"""
    from core.sticker_manager import upload_sticker as _upload, ALLOWED_EXTENSIONS
    ext = Path(file.filename).suffix.lower() if file.filename else ""
    if ext not in ALLOWED_EXTENSIONS:
        raise HTTPException(status_code=400, detail=f"不支持的文件类型: {ext}")
    content = await file.read()
    try:
        result = _upload(content, file.filename, label, category, user_id)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return result


@router.delete("/stickers/{sticker_id}", response_model=StatusResponse)
def delete_custom_sticker(sticker_id: str, user_id: int = Depends(require_auth)):
    """删除自定义贴纸。"""
    from core.sticker_manager import delete_sticker
    if not delete_sticker(sticker_id, user_id):
        raise HTTPException(status_code=404, detail="贴纸不存在或为内置贴纸，不可删除")
    return StatusResponse(message="贴纸已删除")


# --- 钱包 ---

@router.get("/exes/{slug}/wallet")
def get_wallet(slug: str, user_id: int = Depends(require_auth)):
    """获取钱包信息。"""
    slug = _check_exe_access(slug, user_id)
    from core.wallet_manager import load_wallet, load_redpackets, load_transfers
    wallet = load_wallet(slug)
    packets = load_redpackets(slug)
    transfers = load_transfers(slug)
    return {
        "balance": wallet["balance"],
        "transactions": wallet["transactions"],
        "pending_red_packets": [rp for rp in packets if rp["status"] == "pending"],
        "pending_transfers": [tx for tx in transfers if tx["status"] == "pending"],
    }


# --- 红包 ---

@router.post("/exes/{slug}/redpacket/send", response_model=StatusResponse)
def send_redpacket(slug: str, user_id: int = Depends(require_auth)):
    """生成一个红包（模拟 ta 发红包）。"""
    slug = _check_exe_access(slug, user_id)
    from core.wallet_manager import create_redpacket
    rp = create_redpacket(slug)
    if rp is None:
        raise HTTPException(status_code=429, detail="红包太频繁，请稍后再试")
    return StatusResponse(message=f"红包已发送: {rp['note']} (¥{rp['amount']})")


@router.post("/exes/{slug}/redpacket/{rp_id}/open")
def open_redpacket(slug: str, rp_id: str, user_id: int = Depends(require_auth)):
    """打开红包。"""
    slug = _check_exe_access(slug, user_id)
    from core.wallet_manager import open_redpacket
    rp = open_redpacket(slug, rp_id)
    if rp is None:
        raise HTTPException(status_code=400, detail="红包不存在或已被打开")
    return {"amount": rp["amount"], "note": rp["note"], "status": "opened"}


# --- 转账 ---

@router.post("/exes/{slug}/transfer/send", response_model=StatusResponse)
def send_transfer(slug: str, req: TransferRequest, user_id: int = Depends(require_auth)):
    """发起转账。"""
    slug = _check_exe_access(slug, user_id)
    from core.wallet_manager import create_transfer
    tx = create_transfer(slug, req.amount, req.note, req.direction)
    return StatusResponse(message=f"转账已发起: {req.note} (¥{req.amount})")


@router.post("/exes/{slug}/transfer/{tx_id}/confirm")
def confirm_transfer(slug: str, tx_id: str, req: TransferConfirmRequest, user_id: int = Depends(require_auth)):
    """确认转账 (receive/return)。"""
    slug = _check_exe_access(slug, user_id)
    from core.wallet_manager import confirm_transfer
    tx = confirm_transfer(slug, tx_id, req.action)
    if tx is None:
        raise HTTPException(status_code=400, detail="转账不存在或已处理")
    return {"status": tx["status"], "amount": tx["amount"], "note": tx["note"]}


# --- Token 用量 ---

@router.get("/exes/{slug}/usage")
def get_usage(slug: str, user_id: int = Depends(require_auth)):
    """获取当前 session 的累计 Token 用量。"""
    slug = _check_exe_access(slug, user_id)
    with _counter_lock:
        counter = _session_counters.get((user_id, slug))
    if counter is None:
        return {"prompt_tokens": 0, "completion_tokens": 0, "turns": 0}
    return {
        "prompt_tokens": counter.total_prompt_tokens,
        "completion_tokens": counter.total_completion_tokens,
        "turns": counter.session_turns,
    }


@router.delete("/exes/{slug}/usage")
def reset_usage(slug: str, user_id: int = Depends(require_auth)):
    """重置 session Token 计数。"""
    slug = _check_exe_access(slug, user_id)
    with _counter_lock:
        _session_counters.pop((user_id, slug), None)
    return {"message": "已重置"}


# --- 对话 ---

@router.post("/chat", response_model=ChatResponse)
async def chat(req: ChatRequest, user_id: int = Depends(require_auth)):
    """单轮对话。"""
    try:
        slug = validate_slug(req.slug)
        message = validate_user_input(req.message) if req.message else ""
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    slug = _check_exe_access(slug, user_id)

    if req.sticker_id and not message:
        from core.sticker_manager import get_sticker
        from core.sticker_selector import STICKERS
        if req.sticker_id in STICKERS:
            message = STICKERS[req.sticker_id]["emoji"]
        else:
            sticker = get_sticker(req.sticker_id, user_id=user_id)
            message = f"[贴纸: {sticker['label']}]" if sticker else "[贴纸]"

    if not message:
        raise HTTPException(status_code=400, detail="消息不能为空")

    history = sanitize_chat_history(req.history)

    try:
        engine = _get_engine(slug)
        reply, stickers, usage = await run_in_threadpool(engine.chat, message, history)

        token_info = None
        if usage:
            prompt_tk = getattr(usage, "prompt_tokens", 0)
            completion_tk = getattr(usage, "completion_tokens", 0)
            token_info = {
                "prompt_tokens": prompt_tk,
                "completion_tokens": completion_tk,
            }
            # 累积 session 计数
            with _counter_lock:
                key = (user_id, slug)
                counter = _session_counters.get(key)
                if counter is None:
                    counter = TokenCounter()
                    _session_counters[key] = counter
                counter.update(usage)
                token_info["session"] = {
                    "prompt_tokens": counter.total_prompt_tokens,
                    "completion_tokens": counter.total_completion_tokens,
                    "turns": counter.session_turns,
                }
        return ChatResponse(reply=reply, stickers=stickers, tokens=token_info)
    except Exception as e:
        logger.error("对话失败: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail=_INTERNAL_ERROR)


@router.post("/chat/stream")
async def chat_stream(req: ChatRequest, user_id: int = Depends(require_auth)):
    """流式对话 (SSE)。"""
    try:
        slug = validate_slug(req.slug)
        message = validate_user_input(req.message) if req.message else ""
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    slug = _check_exe_access(slug, user_id)

    if req.sticker_id and not message:
        from core.sticker_manager import get_sticker
        from core.sticker_selector import STICKERS
        if req.sticker_id in STICKERS:
            message = STICKERS[req.sticker_id]["emoji"]
        else:
            sticker = get_sticker(req.sticker_id, user_id=user_id)
            message = f"[贴纸: {sticker['label']}]" if sticker else "[贴纸]"

    if not message:
        raise HTTPException(status_code=400, detail="消息不能为空")

    history = sanitize_chat_history(req.history)

    from core.validation import estimate_tokens

    async def generate():
        full_reply = ""
        try:
            engine = _get_engine(slug)
            for item in engine.chat_stream(message, history):
                if item.get("type") == "text":
                    full_reply += item.get("content", "")
                yield f"data: {json.dumps(item)}\n\n"

            # 流式无法获取精确 usage，用 token 估算
            est_prompt = estimate_tokens(message + "\n".join(
                m.get("content", "")[:200] for m in history[-20:]
            ))
            est_completion = estimate_tokens(full_reply)
            # 构造一个近似 usage 对象用于累计
            class _ApproxUsage:
                prompt_tokens = est_prompt
                completion_tokens = est_completion
            approx_usage = _ApproxUsage()

            with _counter_lock:
                key = (user_id, slug)
                counter = _session_counters.get(key)
                if counter is None:
                    counter = TokenCounter()
                    _session_counters[key] = counter
                counter.update(approx_usage)

            yield f"data: [DONE]\n\n"
        except Exception as e:
            logger.error("流式对话失败: %s", e, exc_info=True)
            yield f"data: {json.dumps({'error': _INTERNAL_ERROR})}\n\n"

    return StreamingResponse(generate(), media_type="text/event-stream")


# --- 更新 ---

@router.post("/exes/{slug}/update", response_model=StatusResponse)
def update_exe(slug: str, req: UpdateRequest, user_id: int = Depends(require_auth)):
    """向镜像追加新素材。"""
    slug = _check_exe_access(slug, user_id)

    from pipeline.merger import merge_new_material
    result = merge_new_material(slug, req.content, req.source_type)
    if result.get("error"):
        raise HTTPException(status_code=500, detail=_INTERNAL_ERROR)
    _invalidate_engine(slug)
    return StatusResponse(message="合并完成")


# --- 反思 ---

@router.post("/exes/{slug}/reflect", response_model=StatusResponse)
def reflect_exe(slug: str, user_id: int = Depends(require_auth)):
    """关系反思分析。"""
    slug = _check_exe_access(slug, user_id)

    from pipeline.reflector import run_reflection
    try:
        run_reflection(slug)
    except FileNotFoundError:
        raise HTTPException(status_code=400, detail="缺少 memory.md")
    except RuntimeError:
        raise HTTPException(status_code=500, detail=_INTERNAL_ERROR)
    return StatusResponse(message="反思完成")


# --- 朋友圈 ---

@router.get("/exes/{slug}/moments")
def list_moments(slug: str, user_id: int = Depends(require_auth)):
    """获取朋友圈时间线。"""
    slug = _check_exe_access(slug, user_id)
    ex_dir = get_ex_dir(slug)
    moments_path = ex_dir / "moments.json"
    if not moments_path.exists():
        return {"moments": []}
    moments = json.loads(moments_path.read_text(encoding="utf-8"))
    return {"moments": moments}


@router.post("/exes/{slug}/moments/generate", response_model=StatusResponse)
def generate_moment(slug: str, user_id: int = Depends(require_auth)):
    """生成一条朋友圈。"""
    slug = _check_exe_access(slug, user_id)

    from pipeline.moment_generator import generate_moment as _gen
    try:
        _gen(slug)
        return StatusResponse(message="朋友圈已生成")
    except FileNotFoundError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except RuntimeError:
        raise HTTPException(status_code=500, detail=_INTERNAL_ERROR)


# --- 版本管理 ---

@router.post("/exes/{slug}/backup", response_model=StatusResponse)
def backup_exe(slug: str, req: BackupRequest = None, user_id: int = Depends(require_auth)):
    """备份版本。"""
    slug = _check_exe_access(slug, user_id)
    from core.version_manager import backup
    version = backup(slug, req.version_name if req else "")
    return StatusResponse(message=f"备份成功: {version}")


@router.post("/exes/{slug}/rollback", response_model=StatusResponse)
def rollback_exe(slug: str, req: RollbackRequest, user_id: int = Depends(require_auth)):
    """回滚版本。"""
    slug = _check_exe_access(slug, user_id)
    from core.version_manager import rollback, list_versions
    try:
        rollback(slug, req.version)
        _invalidate_engine(slug)
        return StatusResponse(message=f"已回滚到 {req.version}")
    except FileNotFoundError:
        versions = list_versions(slug)
        raise HTTPException(
            status_code=404,
            detail=f"版本 {req.version} 不存在。可用: {versions}",
        )


@router.get("/exes/{slug}/versions")
def list_versions_route(slug: str, user_id: int = Depends(require_auth)):
    """列出版本。"""
    slug = _check_exe_access(slug, user_id)
    from core.version_manager import list_versions
    return {"slug": slug, "versions": list_versions(slug)}
