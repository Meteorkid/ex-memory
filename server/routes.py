"""REST API 路由。"""

import json
import logging
import threading
from pathlib import Path
from fastapi import APIRouter, HTTPException, UploadFile, File, Form, Depends
from fastapi.responses import StreamingResponse
from starlette.concurrency import run_in_threadpool

from config import EXES_DIR, get_ex_dir, get_collection_name
from core.validation import validate_slug, validate_user_input
from core.file_utils import atomic_write, atomic_write_json
from core.token_counter import TokenCounter
from server.middleware import require_auth
from server.models import (
    CreateRequest, ChatRequest, UpdateRequest,
    BackupRequest, RollbackRequest, DeleteRequest,
    ExeInfo, ChatResponse, StatusResponse, ErrorResponse, AuthRequest, LogoutRequest,
    TransferRequest, TransferConfirmRequest,
)

logger = logging.getLogger("ex-memory")
router = APIRouter(prefix="/api")

# 服务端 session 级 token 累计计数器（内存存储，重启后清零）
_session_counters: dict[tuple[int, str], TokenCounter] = {}
_counter_lock = threading.Lock()

# Engine 缓存（避免每次请求重建 SKILL.md / ChromaDB 连接）
_engine_cache: dict[str, object] = {}
_engine_cache_lock = threading.Lock()


def _get_engine(slug: str):
    """获取或创建 ChatEngine（带缓存）。"""
    with _engine_cache_lock:
        engine = _engine_cache.get(slug)
        if engine is not None:
            return engine
    from core.factory import create_engine_and_store
    engine, _, _ = create_engine_and_store(slug)
    with _engine_cache_lock:
        _engine_cache[slug] = engine
    return engine


def _invalidate_engine(slug: str):
    """使缓存的 engine 失效（纠正/更新 SKILL.md 后调用）。"""
    with _engine_cache_lock:
        _engine_cache.pop(slug, None)


# --- 用户认证 ---

@router.post("/auth/register", response_model=StatusResponse)
def register(req: AuthRequest):
    """注册新用户。"""
    from server.auth import register_user
    error = register_user(req.username, req.password)
    if error:
        raise HTTPException(status_code=400, detail=error)
    return StatusResponse(message="注册成功，请登录")


@router.post("/auth/login")
def login(req: AuthRequest):
    """登录获取 token。"""
    from server.auth import login_user
    token = login_user(req.username, req.password)
    if token is None:
        raise HTTPException(status_code=401, detail="用户名或密码错误")
    return {"token": token, "token_type": "bearer"}


@router.post("/auth/logout", response_model=StatusResponse)
def logout(req: LogoutRequest):
    """注销 token。"""
    from server.auth import revoke_token
    revoke_token(req.token)
    return StatusResponse(message="已注销")


# --- 镜像管理 ---

@router.get("/exes", response_model=list[ExeInfo])
def list_exes(user_id: int = Depends(require_auth)):
    """列出所有镜像。"""
    exes = []
    if not EXES_DIR.exists():
        return exes
    for d in EXES_DIR.iterdir():
        if not d.is_dir():
            continue
        meta_path = d / "meta.json"
        if not meta_path.exists():
            continue
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
    result = run_create_flow_api(slug=slug, name=req.name, answers=req.answers)
    if result.get("error"):
        raise HTTPException(status_code=500, detail=result["error"])
    return StatusResponse(message=f"镜像 [{slug}] 创建成功")


@router.delete("/exes/{slug}", response_model=StatusResponse)
def delete_exe(slug: str, req: DeleteRequest, user_id: int = Depends(require_auth)):
    """删除镜像。"""
    if not req.confirm:
        raise HTTPException(status_code=400, detail="需要确认删除")
    try:
        slug = validate_slug(slug)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    ex_dir = get_ex_dir(slug)
    if not ex_dir.exists():
        raise HTTPException(status_code=404, detail=f"镜像 [{slug}] 不存在")
    import shutil
    shutil.rmtree(ex_dir)
    return StatusResponse(message=f"镜像 [{slug}] 已删除")


# --- 数据导入 ---

MAX_UPLOAD_SIZE = 100 * 1024 * 1024  # 100MB


@router.post("/exes/{slug}/import", response_model=StatusResponse)
async def import_data(slug: str, file: UploadFile = File(...), target_name: str = Form(""), user_id: int = Depends(require_auth)):
    """导入聊天记录数据源（自动检测微信/QQ 格式）。"""
    try:
        slug = validate_slug(slug)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    ex_dir = get_ex_dir(slug)
    if not ex_dir.exists():
        raise HTTPException(status_code=404, detail=f"镜像 [{slug}] 不存在")

    if file.size is not None and file.size > MAX_UPLOAD_SIZE:
        raise HTTPException(status_code=413, detail="文件过大，最大支持 100MB")

    import tempfile
    import shutil

    tmp_dir = Path(tempfile.mkdtemp())
    tmp_path = tmp_dir / file.filename

    try:
        with open(tmp_path, "wb") as f:
            shutil.copyfileobj(file.file, f)

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
        ext = Path(file.filename).suffix.lower()
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
def list_stickers():
    """返回所有可用贴纸。"""
    from core.sticker_selector import get_all_stickers
    return {"stickers": get_all_stickers()}


# --- 钱包 ---

@router.get("/exes/{slug}/wallet")
def get_wallet(slug: str, user_id: int = Depends(require_auth)):
    """获取钱包信息。"""
    try:
        slug = validate_slug(slug)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    ex_dir = get_ex_dir(slug)
    if not ex_dir.exists():
        raise HTTPException(status_code=404, detail=f"镜像 [{slug}] 不存在")
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
    try:
        slug = validate_slug(slug)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    ex_dir = get_ex_dir(slug)
    if not ex_dir.exists():
        raise HTTPException(status_code=404, detail=f"镜像 [{slug}] 不存在")
    from core.wallet_manager import create_redpacket
    rp = create_redpacket(slug)
    if rp is None:
        raise HTTPException(status_code=429, detail="红包太频繁，请稍后再试")
    return StatusResponse(message=f"红包已发送: {rp['note']} (¥{rp['amount']})")


@router.post("/exes/{slug}/redpacket/{rp_id}/open")
def open_redpacket(slug: str, rp_id: str, user_id: int = Depends(require_auth)):
    """打开红包。"""
    try:
        slug = validate_slug(slug)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    ex_dir = get_ex_dir(slug)
    if not ex_dir.exists():
        raise HTTPException(status_code=404, detail=f"镜像 [{slug}] 不存在")
    from core.wallet_manager import open_redpacket
    rp = open_redpacket(slug, rp_id)
    if rp is None:
        raise HTTPException(status_code=400, detail="红包不存在或已被打开")
    return {"amount": rp["amount"], "note": rp["note"], "status": "opened"}


# --- 转账 ---

@router.post("/exes/{slug}/transfer/send", response_model=StatusResponse)
def send_transfer(slug: str, req: TransferRequest, user_id: int = Depends(require_auth)):
    """发起转账。"""
    try:
        slug = validate_slug(slug)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    ex_dir = get_ex_dir(slug)
    if not ex_dir.exists():
        raise HTTPException(status_code=404, detail=f"镜像 [{slug}] 不存在")
    from core.wallet_manager import create_transfer
    tx = create_transfer(slug, req.amount, req.note, req.direction)
    return StatusResponse(message=f"转账已发起: {req.note} (¥{req.amount})")


@router.post("/exes/{slug}/transfer/{tx_id}/confirm")
def confirm_transfer(slug: str, tx_id: str, req: TransferConfirmRequest, user_id: int = Depends(require_auth)):
    """确认转账 (receive/return)。"""
    try:
        slug = validate_slug(slug)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    ex_dir = get_ex_dir(slug)
    if not ex_dir.exists():
        raise HTTPException(status_code=404, detail=f"镜像 [{slug}] 不存在")
    from core.wallet_manager import confirm_transfer
    tx = confirm_transfer(slug, tx_id, req.action)
    if tx is None:
        raise HTTPException(status_code=400, detail="转账不存在或已处理")
    return {"status": tx["status"], "amount": tx["amount"], "note": tx["note"]}


# --- Token 用量 ---

@router.get("/exes/{slug}/usage")
def get_usage(slug: str, user_id: int = Depends(require_auth)):
    """获取当前 session 的累计 Token 用量。"""
    try:
        slug = validate_slug(slug)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
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
    try:
        slug = validate_slug(slug)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    with _counter_lock:
        _session_counters.pop((user_id, slug), None)
    return {"message": "已重置"}


# --- 对话 ---

@router.post("/chat", response_model=ChatResponse)
async def chat(req: ChatRequest, user_id: int = Depends(require_auth)):
    """单轮对话。"""
    try:
        slug = validate_slug(req.slug)
        message = validate_user_input(req.message)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    ex_dir = get_ex_dir(slug)
    if not ex_dir.exists():
        raise HTTPException(status_code=404, detail=f"镜像 [{slug}] 不存在")

    try:
        engine = _get_engine(slug)
        reply, stickers, usage = await run_in_threadpool(engine.chat, message, req.history[-100:])

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
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/chat/stream")
async def chat_stream(req: ChatRequest, user_id: int = Depends(require_auth)):
    """流式对话 (SSE)。"""
    try:
        slug = validate_slug(req.slug)
        message = validate_user_input(req.message)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    ex_dir = get_ex_dir(slug)
    if not ex_dir.exists():
        raise HTTPException(status_code=404, detail=f"镜像 [{slug}] 不存在")

    from core.validation import estimate_tokens

    async def generate():
        full_reply = ""
        try:
            engine = _get_engine(slug)
            for item in engine.chat_stream(message, req.history[-100:]):
                if item.get("type") == "text":
                    full_reply += item.get("content", "")
                yield f"data: {json.dumps(item)}\n\n"

            # 流式无法获取精确 usage，用 token 估算
            est_prompt = estimate_tokens(message + "\n".join(
                m.get("content", "")[:200] for m in (req.history or [])[-20:]
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
            yield f"data: {json.dumps({'error': str(e)})}\n\n"

    return StreamingResponse(generate(), media_type="text/event-stream")


# --- 更新 ---

@router.post("/exes/{slug}/update", response_model=StatusResponse)
def update_exe(slug: str, req: UpdateRequest, user_id: int = Depends(require_auth)):
    """向镜像追加新素材。"""
    try:
        slug = validate_slug(slug)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    ex_dir = get_ex_dir(slug)
    if not ex_dir.exists():
        raise HTTPException(status_code=404, detail=f"镜像 [{slug}] 不存在")

    from pipeline.merger import merge_new_material
    result = merge_new_material(slug, req.content, req.source_type)
    if result.get("error"):
        raise HTTPException(status_code=500, detail=result["error"])
    _invalidate_engine(slug)
    return StatusResponse(message="合并完成")


# --- 反思 ---

@router.post("/exes/{slug}/reflect", response_model=StatusResponse)
def reflect_exe(slug: str, user_id: int = Depends(require_auth)):
    """关系反思分析。"""
    try:
        slug = validate_slug(slug)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    ex_dir = get_ex_dir(slug)
    if not ex_dir.exists():
        raise HTTPException(status_code=404, detail=f"镜像 [{slug}] 不存在")

    from pipeline.reflector import run_reflection
    try:
        run_reflection(slug)
    except FileNotFoundError:
        raise HTTPException(status_code=400, detail="缺少 memory.md")
    except RuntimeError as e:
        raise HTTPException(status_code=500, detail=str(e))
    return StatusResponse(message="反思完成")


# --- 朋友圈 ---

@router.get("/exes/{slug}/moments")
def list_moments(slug: str, user_id: int = Depends(require_auth)):
    """获取朋友圈时间线。"""
    try:
        slug = validate_slug(slug)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    ex_dir = get_ex_dir(slug)
    if not ex_dir.exists():
        raise HTTPException(status_code=404, detail=f"镜像 [{slug}] 不存在")
    moments_path = ex_dir / "moments.json"
    if not moments_path.exists():
        return {"moments": []}
    moments = json.loads(moments_path.read_text(encoding="utf-8"))
    return {"moments": moments}


@router.post("/exes/{slug}/moments/generate", response_model=StatusResponse)
def generate_moment(slug: str, user_id: int = Depends(require_auth)):
    """生成一条朋友圈。"""
    try:
        slug = validate_slug(slug)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    ex_dir = get_ex_dir(slug)
    if not ex_dir.exists():
        raise HTTPException(status_code=404, detail=f"镜像 [{slug}] 不存在")

    from pipeline.moment_generator import generate_moment as _gen
    try:
        _gen(slug)
        return StatusResponse(message="朋友圈已生成")
    except FileNotFoundError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except RuntimeError as e:
        raise HTTPException(status_code=500, detail=str(e))


# --- 版本管理 ---

@router.post("/exes/{slug}/backup", response_model=StatusResponse)
def backup_exe(slug: str, req: BackupRequest = None, user_id: int = Depends(require_auth)):
    """备份版本。"""
    try:
        slug = validate_slug(slug)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    from core.version_manager import backup
    version = backup(slug, req.version_name if req else "")
    return StatusResponse(message=f"备份成功: {version}")


@router.post("/exes/{slug}/rollback", response_model=StatusResponse)
def rollback_exe(slug: str, req: RollbackRequest, user_id: int = Depends(require_auth)):
    """回滚版本。"""
    try:
        slug = validate_slug(slug)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    from core.version_manager import rollback, list_versions
    try:
        rollback(slug, req.version)
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
    try:
        slug = validate_slug(slug)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    from core.version_manager import list_versions
    return {"slug": slug, "versions": list_versions(slug)}
