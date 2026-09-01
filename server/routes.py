"""REST API 路由。"""

import json
import logging
import shutil
import uuid
import threading
import time
from datetime import datetime
from types import SimpleNamespace
from typing import Optional
from pathlib import Path
from fastapi import (
    APIRouter,
    BackgroundTasks,
    HTTPException,
    UploadFile,
    File,
    Form,
    Depends,
    Request,
    Query,
)
from fastapi.responses import StreamingResponse
from starlette.concurrency import run_in_threadpool

from config import PROJECT_DIR, resolve_ex_dir, DISABLE_REGISTRATION
from core.validation import validate_slug, validate_user_input, sanitize_chat_history
from core.exe_access import assert_exe_access, set_owner_user_id, iter_accessible_exes
from core.path_safety import safe_filename
import config
from core import kv
from core.bounded_cache import BoundedCache
from core.token_counter import TokenCounter
from core.logging import get_audit_logger
from server.usage_guard import check_limit
from server.safety_gate import (
    check_crisis,
    check_input,
    check_output,
    check_output_streaming,
)
from server.middleware import require_admin, require_auth, _get_client_ip, security
from server.models import (
    AuthRequest,
    BackupRequest,
    ChatRequest,
    ChatResponse,
    CreateRequest,
    DeleteRequest,
    ExeInfo,
    FeedbackRequest,
    ResumeRequest,
    RollbackRequest,
    StatusResponse,
    TransferConfirmRequest,
    TransferRequest,
    UpdateRequest,
    ConsentRequest,
    SubjectRequestPayload,
    PhoneCodeRequest,
    ReviewResolution,
    TaskAccepted,
    RefreshRequest,
)
from fastapi.security import HTTPAuthorizationCredentials

# ═══════════════════════════════════════
# 简单内存缓存
# ═══════════════════════════════════════


class SimpleCache:
    """内存缓存：TTL + LRU 淘汰，防止内存泄漏。"""

    def __init__(self, default_ttl=60, maxsize=256):
        from collections import OrderedDict

        self._cache = OrderedDict()
        self._default_ttl = default_ttl
        self._maxsize = maxsize

    def get(self, key):
        if key in self._cache:
            data, expiry = self._cache[key]
            if time.time() < expiry:
                self._cache.move_to_end(key)
                return data
            del self._cache[key]
        return None

    def set(self, key, value, ttl=None):
        if key in self._cache:
            del self._cache[key]
        elif len(self._cache) >= self._maxsize:
            self._cache.popitem(last=False)
        self._cache[key] = (value, time.time() + (ttl or self._default_ttl))

    def delete(self, key):
        self._cache.pop(key, None)

    def clear(self):
        self._cache.clear()


# 全局缓存实例
cache = SimpleCache(default_ttl=30)  # 30秒 TTL

# ═══════════════════════════════════════
# meta.json 读写工具
# ═══════════════════════════════════════


def _load_meta(slug: str, owner: Optional[int] = None) -> dict:
    """读取镜像 meta.json，不存在则抛 404。"""
    meta_file = resolve_ex_dir(slug, owner) / "meta.json"
    if not meta_file.exists():
        raise HTTPException(status_code=404, detail="镜像不存在")
    return json.loads(meta_file.read_text(encoding="utf-8"))


def _save_meta(slug: str, meta: dict, owner: Optional[int] = None) -> None:
    """写入镜像 meta.json。"""
    meta_file = resolve_ex_dir(slug, owner) / "meta.json"
    meta_file.write_text(
        json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8"
    )


logger = logging.getLogger("ex-memory")
router = APIRouter(prefix="/api")
_INTERNAL_ERROR = "服务器内部错误，请稍后重试"

# 服务端 session 级 token 累计计数器（内存存储，重启后清零）
# 会话级 token 计数。有上限：原为裸 dict，每个 (user, slug) 组合都会
# 永久驻留，长期运行必然涨爆内存
# BoundedCache 本身线程安全，但 TokenCounter 的累加不是，仍需一把锁
_counter_lock = threading.Lock()
_session_counters = BoundedCache(
    maxsize=config.SESSION_COUNTER_CACHE_SIZE,
    ttl_seconds=config.SESSION_COUNTER_TTL_SECONDS,
)

# Engine 缓存（避免每次请求重建 SKILL.md / ChromaDB 连接）
# 键必须含用户维度：slug 是全局命名空间，仅按 slug 缓存会在
# 同名镜像删除重建后把上一任 owner 的人格内容泄漏给新用户。
# 同样有上限：每个引擎持有数万字符人格文本与向量库客户端
_engine_cache = BoundedCache(
    maxsize=config.ENGINE_CACHE_SIZE,
    ttl_seconds=config.ENGINE_CACHE_TTL_SECONDS,
)


def _evict_engines_for_slug(slug: str) -> None:
    """清掉本副本上该 slug 的全部引擎缓存（所有用户）。"""
    _engine_cache.evict_where(lambda key: key[1] == slug)


# 模块导入时注册一次：放在 create_app 里会每建一次 app 就追加一个处理器
kv.on_invalidate(_evict_engines_for_slug)

# 登录限流 + 审计日志
_login_limiter = None
_audit_logger = None


def _load_history(
    slug: str, *, with_time: bool = False, owner: Optional[int] = None
) -> list[dict]:
    """读取对话历史并归一化为 {role, content[, created_at]} 列表。"""
    from core.conversation_store import load_jsonl_messages

    keys = ("role", "content", "created_at") if with_time else ("role", "content")
    return [{k: m.get(k, "") for k in keys} for m in load_jsonl_messages(slug, owner)]


@router.get("/local-helper/config")
def get_local_helper_config():
    """返回不含本机隐私数据的助手版本和本站下载信息。"""
    from core.local_helper_release import get_local_helper_release

    return get_local_helper_release()


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
        _get_audit().info(
            json.dumps(
                {
                    "event": event,
                    "username": username,
                    "ip": client_ip,
                    "detail": detail,
                },
                ensure_ascii=False,
            )
        )
    except (OSError, TypeError, ValueError) as e:
        # 审计写入失败不阻断主流程，但必须留痕，否则审计缺口无人察觉
        logger.warning("审计日志写入失败 event=%s: %s", event, e)


def _get_engine(slug: str, user_id: int):
    """获取或创建 ChatEngine（按 (user_id, slug) 缓存，LRU + TTL 有界）。"""
    from core.factory import create_engine_and_store

    return _engine_cache.get_or_create(
        (user_id, slug), lambda: create_engine_and_store(slug, owner=user_id)[0]
    )


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
    """使缓存的 engine 失效（纠正/更新/删除 SKILL.md 后调用）。

    清掉该 slug 下所有用户的缓存条目。
    """
    # 广播而非只清本地：多副本下用户纠正「ta 不会这样」若只在一个副本生效，
    # 其余副本会继续用旧人格回话
    kv.broadcast_invalidate(slug)


# 流式输出审核策略：下发前对「累计全文」过一遍本地词表。
#
# 保证由此而来——违规词一旦完整出现就被拦下，承载它的那个分块不会下发，
# 所以完整的违规词到不了客户端。曾额外做过「扣住尾部若干字符」的设计，
# 实测去掉后保证依然成立（累计审核已经覆盖），属于纯粹的延迟浪费，已移除。
#
# 热路径只用本地词表：主通道若逐块调用，就是每个分块一次网络请求，
# 延迟与成本都不可接受。完整的主通道检查放在流结束后做一次。


def _persist_stream_turn(
    slug: str,
    user_id: int,
    message: str,
    full_reply: str,
    stickers: list[str],
) -> None:
    """流式对话落库，口径与 /chat 一致：剥离贴纸标签、source=web。

    持久化失败只记日志，不影响响应；无正文时不落库。
    """
    if not full_reply.strip():
        return
    from core.conversation_store import append_turn
    from core.engine import ChatEngine

    clean_reply, inline_stickers = ChatEngine._extract_sticker_tags(full_reply)
    merged = list(dict.fromkeys(inline_stickers + stickers))
    try:
        append_turn(slug, user_id, message, clean_reply, stickers=merged, source="web")
    except (OSError, ValueError) as e:
        # 持久化失败不影响本次回复，但会造成搜索/统计缺数据
        logger.warning("流式对话持久化失败 slug=%s: %s", slug, e)


def _run_session_archive(
    slug: str, vector_store, embedder, owner: Optional[int] = None
) -> None:
    """后台任务：累计轮数达到阈值时归档会话并生成 LLM 摘要。"""
    from core.session_archive import maybe_archive

    try:
        if maybe_archive(
            slug, vector_store=vector_store, embedder=embedder, owner=owner
        ):
            # 摘要写入了 sessions/ 与 SKILL.md，缓存引擎需要重建才会带上记忆层
            _invalidate_engine(slug)
    except Exception as e:
        # 后台任务异常不能冒泡到响应收尾，留告警即可
        logger.warning("会话归档后台任务失败 slug=%s: %s", slug, e)


# --- 用户认证 ---


@router.post("/auth/register", response_model=StatusResponse)
def register(req: AuthRequest, request: Request):
    """注册新用户。"""
    import config

    if config.METEOR_STORE_SSO_ENABLED:
        raise HTTPException(status_code=404, detail="Not found")
    if DISABLE_REGISTRATION:
        raise HTTPException(status_code=403, detail="注册已关闭")
    client_ip = _get_client_ip(request)
    _get_login_limiter().check(req.username, client_ip)

    from server.phone_verify import (
        bind_phone,
        get_user_id_by_username,
        mark_age_confirmed,
        normalize_phone,
        phone_in_use,
        verify_code,
    )

    # 年龄门槛：本产品服务的是失恋、丧失等情绪场景，性质不适合未成年人
    if config.REQUIRE_AGE_CONFIRMATION and not req.age_confirmed:
        raise HTTPException(status_code=400, detail="请确认你已年满 18 周岁")

    phone = None
    if config.REQUIRE_PHONE_VERIFICATION:
        try:
            phone = normalize_phone(req.phone or "")
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e
        if phone_in_use(phone):
            raise HTTPException(status_code=400, detail="该手机号已注册")
        if not verify_code(phone, req.code or ""):
            raise HTTPException(status_code=400, detail="验证码错误或已过期")

    from server.auth import register_user

    error = register_user(req.username, req.password)
    if error:
        _audit(
            "register_failed", username=req.username, client_ip=client_ip, detail=error
        )
        raise HTTPException(status_code=400, detail=error)

    new_user_id = get_user_id_by_username(req.username)
    if new_user_id is not None:
        if phone:
            bind_phone(new_user_id, phone)
        if req.age_confirmed:
            mark_age_confirmed(new_user_id)

    _audit("register_success", username=req.username, client_ip=client_ip)
    return StatusResponse(message="注册成功，请登录")


@router.post("/auth/phone/send-code", response_model=StatusResponse)
def send_phone_code(req: PhoneCodeRequest, request: Request):
    """签发注册用的手机验证码。"""
    from server.phone_verify import issue_code, normalize_phone, phone_in_use

    client_ip = _get_client_ip(request)
    _get_login_limiter().check(f"sms:{req.phone[:20]}", client_ip)

    try:
        phone = normalize_phone(req.phone)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    if phone_in_use(phone):
        raise HTTPException(status_code=400, detail="该手机号已注册")

    ok, message = issue_code(phone)
    if not ok:
        raise HTTPException(status_code=429, detail=message)
    return StatusResponse(message=message)


@router.post("/auth/login")
def login(req: AuthRequest, request: Request):
    """登录获取 token。"""
    import config

    if config.METEOR_STORE_SSO_ENABLED:
        raise HTTPException(status_code=404, detail="Not found")
    client_ip = _get_client_ip(request)
    _get_login_limiter().check(req.username, client_ip)

    from server.auth import login_user_with_refresh

    session = login_user_with_refresh(
        req.username,
        req.password,
        user_agent=request.headers.get("User-Agent", ""),
        ip=client_ip,
    )

    if session is None:
        _audit("login_failed", username=req.username, client_ip=client_ip)
        raise HTTPException(status_code=401, detail="用户名或密码错误")
    _audit("login_success", username=req.username, client_ip=client_ip)
    # token 字段保留，老客户端不受影响；refresh_token 与 session_id 为新增
    return {**session, "token_type": "bearer"}


@router.post("/auth/logout", response_model=StatusResponse)
def logout(
    credentials: HTTPAuthorizationCredentials = Depends(security),
    user_id: int = Depends(require_auth),
):
    """注销当前 Bearer token。"""
    import config

    if config.METEOR_STORE_SSO_ENABLED:
        raise HTTPException(status_code=404, detail="Not found")
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
            exes.append(
                ExeInfo(
                    slug=d.name,
                    name=meta.get("name", d.name),
                    state=meta.get("pipeline_state", "unknown"),
                    created_at=meta.get("created_at", ""),
                    updated_at=meta.get("updated_at"),
                )
            )
        except (OSError, json.JSONDecodeError) as e:
            logger.warning("跳过损坏的 meta.json slug=%s: %s", d.name, e)
    return sorted(exes, key=lambda e: e.created_at, reverse=True)


@router.post("/exes", response_model=StatusResponse)
def create_exe(req: CreateRequest, user_id: int = Depends(require_auth)):
    """创建新镜像。"""
    try:
        slug = validate_slug(req.slug)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    ex_dir = resolve_ex_dir(slug, user_id)
    if ex_dir.exists():
        raise HTTPException(status_code=409, detail=f"镜像 [{slug}] 已存在")

    # 目录虽不存在，同名旧镜像的引擎可能仍残留在缓存中，必须先清掉
    _invalidate_engine(slug)

    from pipeline.orchestrator import run_create_flow_api

    result = run_create_flow_api(
        slug=slug,
        name=req.name,
        answers=req.answers,
        owner_user_id=user_id,
    )
    if result.get("error"):
        raise HTTPException(status_code=500, detail=result["error"])
    try:
        set_owner_user_id(slug, user_id)
    except (OSError, ValueError, FileNotFoundError) as e:
        # 绑定失败会让镜像在多用户模式下永久不可访问，必须显式暴露
        logger.error("镜像 [%s] 绑定 owner 失败: %s", slug, e)
        raise HTTPException(
            status_code=500, detail="镜像创建成功但归属绑定失败，请联系管理员"
        )
    return StatusResponse(message=f"镜像 [{slug}] 创建成功")


@router.post("/exes/{slug}/resume", response_model=StatusResponse)
def resume_exe(slug: str, req: ResumeRequest, user_id: int = Depends(require_auth)):
    """从上次失败步骤恢复创建。"""
    slug = _check_exe_access(slug, user_id)

    from pipeline.orchestrator import run_create_flow_api

    result = run_create_flow_api(
        slug=slug, name=req.name, answers=[], resume=True, owner_user_id=user_id
    )
    if result.get("error"):
        raise HTTPException(status_code=500, detail=result["error"])
    # 恢复流程会重写 SKILL.md 等人格文件，缓存引擎已过期
    _invalidate_engine(slug)
    return StatusResponse(message=f"镜像 [{slug}] 恢复创建成功")


@router.delete("/exes/{slug}", response_model=StatusResponse)
def delete_exe(slug: str, req: DeleteRequest, user_id: int = Depends(require_auth)):
    """删除镜像。"""
    if not req.confirm:
        raise HTTPException(status_code=400, detail="需要确认删除")
    slug = _check_exe_access(slug, user_id)
    ex_dir = resolve_ex_dir(slug, user_id)
    import shutil

    shutil.rmtree(ex_dir)
    # 不清缓存的话，同名镜像重建后会命中上一任 owner 的引擎（跨用户人格泄漏）
    _invalidate_engine(slug)
    _audit("exe_deleted", username=f"user_id={user_id}", detail=f"slug={slug}")
    return StatusResponse(message=f"镜像 [{slug}] 已删除")


# --- 数据导入 ---

MAX_UPLOAD_SIZE = 100 * 1024 * 1024  # 100MB


@router.post("/exes/{slug}/import", response_model=TaskAccepted)
def import_data(
    slug: str,
    file: UploadFile = File(...),
    target_name: str = Form(""),
    user_id: int = Depends(require_auth),
):
    """导入聊天记录数据源（自动检测微信/QQ 格式）。

    函数体全是同步阻塞调用（文件拷贝、Embedding 网络请求、CPU 切片），
    必须声明为 def 让 FastAPI 放进线程池，否则会冻结整个事件循环。
    """
    slug = _check_exe_access(slug, user_id)

    # 🔴 上传的是用户与他人的聊天记录，处理的是第三方的个人信息。
    # 必须有针对这一项的独立同意，不能与总协议捆绑，也不能静默通过。
    from config import THIRD_PARTY_DATA_POLICY_VERSION
    from server.consent_store import POLICY_THIRD_PARTY_DATA, has_consented

    if not has_consented(
        user_id, POLICY_THIRD_PARTY_DATA, THIRD_PARTY_DATA_POLICY_VERSION
    ):
        raise HTTPException(
            status_code=403,
            detail=(
                "导入前需确认你对这份聊天记录拥有合法处理基础。"
                f"请先提交 {POLICY_THIRD_PARTY_DATA} 协议"
                f"（版本 {THIRD_PARTY_DATA_POLICY_VERSION}）的同意。"
            ),
        )

    if file.size is not None and file.size > MAX_UPLOAD_SIZE:
        raise HTTPException(status_code=413, detail="文件过大，最大支持 100MB")

    try:
        safe_name = safe_filename(file.filename or "upload.dat")
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e

    # 落到暂存区而不是请求级临时目录：请求返回后 worker 还要读它。
    # 任务处理器读完即删——原始聊天记录是最敏感的那份数据。
    staging = config.PROJECT_DIR / "data" / "uploads" / uuid.uuid4().hex
    staging.mkdir(parents=True, exist_ok=True)
    staged_path = staging / safe_name
    try:
        with open(staged_path, "wb") as f:
            _copy_upload_limited(file.file, f, MAX_UPLOAD_SIZE)
    except ValueError as e:
        shutil.rmtree(staging, ignore_errors=True)
        raise HTTPException(status_code=413, detail=str(e)) from e
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise

    _invalidate_engine(slug)

    from core.tasks import enqueue
    from server.task_handlers import TASK_IMPORT

    task_id = enqueue(
        TASK_IMPORT,
        user_id,
        {
            "slug": slug,
            "owner": user_id,
            "source_path": str(staged_path),
            "target_name": target_name,
        },
        slug=slug,
    )
    return TaskAccepted(task_id=task_id, message="导入已开始，可通过任务接口查看进度")


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
        combined = [
            s for s in emoji_stickers if s.get("emotion") == category
        ] + image_stickers
    return {"stickers": combined}


@router.get("/stickers/{sticker_id}")
def get_sticker_route(sticker_id: str, user_id: int = Depends(require_auth)):
    """获取单个贴纸信息。"""
    from core.sticker_selector import STICKERS
    from core.sticker_manager import get_sticker as get_image_sticker

    if sticker_id in STICKERS:
        s = STICKERS[sticker_id]
        return {
            "id": sticker_id,
            "type": "emoji",
            "emoji": s["emoji"],
            "label": s["label"],
            "category": s["emotion"],
        }
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
        result = _upload(content, file.filename or "sticker", label, category, user_id)
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

    wallet = load_wallet(slug, owner=user_id)
    packets = load_redpackets(slug, owner=user_id)
    transfers = load_transfers(slug, owner=user_id)
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

    rp = create_redpacket(slug, owner=user_id)
    if rp is None:
        raise HTTPException(status_code=429, detail="红包太频繁，请稍后再试")
    return StatusResponse(message=f"红包已发送: {rp['note']} (¥{rp['amount']})")


@router.post("/exes/{slug}/redpacket/{rp_id}/open")
def open_redpacket(slug: str, rp_id: str, user_id: int = Depends(require_auth)):
    """打开红包。"""
    slug = _check_exe_access(slug, user_id)
    from core.wallet_manager import open_redpacket

    rp = open_redpacket(slug, rp_id, owner=user_id)
    if rp is None:
        raise HTTPException(status_code=400, detail="红包不存在或已被打开")
    return {"amount": rp["amount"], "note": rp["note"], "status": "opened"}


# --- 转账 ---


@router.post("/exes/{slug}/transfer/send", response_model=StatusResponse)
def send_transfer(
    slug: str, req: TransferRequest, user_id: int = Depends(require_auth)
):
    """发起转账。"""
    slug = _check_exe_access(slug, user_id)
    from core.wallet_manager import create_transfer

    create_transfer(slug, req.amount, req.note, req.direction, owner=user_id)
    return StatusResponse(message=f"转账已发起: {req.note} (¥{req.amount})")


@router.post("/exes/{slug}/transfer/{tx_id}/confirm")
def confirm_transfer(
    slug: str,
    tx_id: str,
    req: TransferConfirmRequest,
    user_id: int = Depends(require_auth),
):
    """确认转账 (receive/return)。"""
    slug = _check_exe_access(slug, user_id)
    from core.wallet_manager import confirm_transfer

    tx = confirm_transfer(slug, tx_id, req.action, owner=user_id)
    if tx is None:
        raise HTTPException(status_code=400, detail="转账不存在或已处理")
    return {"status": tx["status"], "amount": tx["amount"], "note": tx["note"]}


# --- Token 用量 ---


@router.get("/exes/{slug}/usage")
def get_usage(slug: str, user_id: int = Depends(require_auth)):
    """获取当前 session 的累计 Token 用量。"""
    slug = _check_exe_access(slug, user_id)
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
    _session_counters.pop((user_id, slug))
    return {"message": "已重置"}


# --- 对话 ---


@router.post("/chat", response_model=ChatResponse)
async def chat(
    req: ChatRequest,
    background_tasks: BackgroundTasks,
    user_id: int = Depends(require_auth),
):
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

    # 🔴 安全闸门必须在这里：构建人格 prompt、检索 RAG、调用 LLM 之前。
    # 命中危机时直接返回，绝不进入人格模拟——「前任」的回应恰恰可能是
    # 最危险的那类内容。
    notice = check_crisis(user_id, slug, message)
    if notice is not None:
        return ChatResponse(reply="", stickers=[], tokens=None, notice=notice)

    # 🔴 强度保护排在危机之后：达到时长上限的用户仍然拿得到危机响应
    limit_notice = check_limit(user_id)
    if limit_notice is not None:
        return ChatResponse(reply="", stickers=[], tokens=None, notice=limit_notice)

    # 内容审核排在危机之后：既命中危机又命中违规词时走危机流程，
    # 不能因为「违规」把正在求救的人挡回去
    blocked = check_input(user_id, slug, message)
    if blocked is not None:
        return ChatResponse(reply="", stickers=[], tokens=None, notice=blocked)

    try:
        engine = _get_engine(slug, user_id)
        reply, stickers, usage = await run_in_threadpool(engine.chat, message, history)

        # 违规输出既不下发也不落库
        blocked_output = check_output(user_id, slug, reply)
        if blocked_output is not None:
            return ChatResponse(
                reply="", stickers=[], tokens=None, notice=blocked_output
            )

        token_info = None
        if usage:
            prompt_tk = getattr(usage, "prompt_tokens", 0)
            completion_tk = getattr(usage, "completion_tokens", 0)
            token_info = {
                "prompt_tokens": prompt_tk,
                "completion_tokens": completion_tk,
            }
            # 累积 session 计数
            counter = _session_counters.get_or_create((user_id, slug), TokenCounter)
            with _counter_lock:
                counter.update(usage)
                token_info["session"] = {
                    "prompt_tokens": counter.total_prompt_tokens,
                    "completion_tokens": counter.total_completion_tokens,
                    "turns": counter.session_turns,
                }
        # 持久化对话（供搜索/统计使用）
        try:
            from core.conversation_store import append_turn

            append_turn(slug, user_id, message, reply, stickers=stickers, source="web")
        except (OSError, ValueError) as e:
            # 持久化失败不影响本次回复，但会造成搜索/统计缺数据
            logger.warning("对话持久化失败 slug=%s: %s", slug, e)
        # 满阈值时会话归档（含 LLM 摘要），放后台执行不阻塞响应
        background_tasks.add_task(
            _run_session_archive, slug, engine.vector_store, engine.embedder, user_id
        )

        return ChatResponse(reply=reply, stickers=stickers, tokens=token_info)
    except Exception as e:
        logger.error("对话失败: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail=_INTERNAL_ERROR)


@router.post("/chat/stream")
async def chat_stream(
    req: ChatRequest,
    background_tasks: BackgroundTasks,
    user_id: int = Depends(require_auth),
):
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

    # 🔴 与 /chat 同一道闸门，且同样在进入 generate()、拿 engine 之前。
    # 两条路径必须都接——M-1 的教训就是流式漏了落库，整个功能形同虚设。
    notice = check_crisis(user_id, slug, message)
    if notice is not None:

        async def crisis_stream():
            yield f"data: {json.dumps(notice)}\n\n"
            yield "data: [DONE]\n\n"

        return StreamingResponse(crisis_stream(), media_type="text/event-stream")

    limit_notice = check_limit(user_id)
    if limit_notice is not None:

        async def limit_stream():
            yield f"data: {json.dumps(limit_notice)}\n\n"
            yield "data: [DONE]\n\n"

        return StreamingResponse(limit_stream(), media_type="text/event-stream")

    blocked = check_input(user_id, slug, message)
    if blocked is not None:

        async def blocked_stream():
            yield f"data: {json.dumps(blocked)}\n\n"
            yield "data: [DONE]\n\n"

        return StreamingResponse(blocked_stream(), media_type="text/event-stream")

    async def generate():
        full_reply = ""
        collected_stickers: list[str] = []
        stream_usage: Optional[dict] = None
        engine = None
        try:
            engine = _get_engine(slug, user_id)
            released = 0  # 已下发字符数
            for item in engine.chat_stream(message, history):
                if item.get("type") == "text":
                    full_reply += item.get("content", "")
                    hit = check_output_streaming(user_id, slug, full_reply)
                    if hit is not None:
                        # 违规词所在的这一块尚未下发，回退到已下发部分
                        full_reply = full_reply[:released]
                        yield f"data: {json.dumps(hit)}\n\n"
                        yield "data: [DONE]\n\n"
                        return
                    if len(full_reply) > released:
                        segment = full_reply[released:]
                        released = len(full_reply)
                        yield f"data: {json.dumps({'type': 'text', 'content': segment})}\n\n"
                    continue
                elif item.get("type") == "sticker" and item.get("id"):
                    collected_stickers.append(item["id"])
                elif item.get("type") == "usage":
                    stream_usage = item
                yield f"data: {json.dumps(item)}\n\n"

            # 收尾：对完整回复做一次主通道检查。此时文本已下发，只能事后
            # 撤回并且不落库——这是流式与「违规内容一个字都不到前端」之间
            # 无法两全的地方，本地词表挡住已知词，主通道兜住其余。
            if full_reply.strip():
                late_hit = check_output(user_id, slug, full_reply)
                if late_hit is not None:
                    full_reply = ""
                    yield f"data: {json.dumps(late_hit)}\n\n"
                    yield "data: [DONE]\n\n"
                    return

            # 计量优先用真实 usage（engine 已开 stream_options，端点不支持时
            # 退回含 system prompt 的估算），口径与 /chat 一致
            if stream_usage is not None:
                usage_obj = SimpleNamespace(
                    prompt_tokens=int(stream_usage.get("prompt_tokens") or 0),
                    completion_tokens=int(stream_usage.get("completion_tokens") or 0),
                )
                counter = _session_counters.get_or_create((user_id, slug), TokenCounter)
                with _counter_lock:
                    counter.update(usage_obj)

            yield "data: [DONE]\n\n"
        except Exception as e:
            logger.error("流式对话失败: %s", e, exc_info=True)
            yield f"data: {json.dumps({'error': _INTERNAL_ERROR})}\n\n"
        finally:
            # 正常结束、生成异常、客户端中断（GeneratorExit/CancelledError，
            # 均非 Exception 子类，不会被上面捕获）都走这里：已生成的部分必须落库。
            # 注意 finally 里禁止 yield（GeneratorExit 期间 yield 会直接报错）
            _persist_stream_turn(slug, user_id, message, full_reply, collected_stickers)
            if engine is not None and full_reply.strip():
                # 满阈值时会话归档（含 LLM 摘要），放后台执行不阻塞流式响应
                background_tasks.add_task(
                    _run_session_archive,
                    slug,
                    engine.vector_store,
                    engine.embedder,
                    user_id,
                )

    return StreamingResponse(generate(), media_type="text/event-stream")


# --- 更新 ---


@router.post("/exes/{slug}/update", response_model=StatusResponse)
def update_exe(slug: str, req: UpdateRequest, user_id: int = Depends(require_auth)):
    """向镜像追加新素材。"""
    slug = _check_exe_access(slug, user_id)

    from pipeline.merger import merge_new_material

    result = merge_new_material(slug, req.content, req.source_type, owner=user_id)
    if result.get("error"):
        raise HTTPException(status_code=500, detail=_INTERNAL_ERROR)
    _invalidate_engine(slug)
    return StatusResponse(message="合并完成")


# --- 反思 ---


@router.post("/exes/{slug}/reflect", response_model=TaskAccepted)
def reflect_exe(slug: str, user_id: int = Depends(require_auth)):
    """关系反思分析。"""
    slug = _check_exe_access(slug, user_id)

    from core.tasks import enqueue
    from server.task_handlers import TASK_REFLECT

    task_id = enqueue(
        TASK_REFLECT, user_id, {"slug": slug, "owner": user_id}, slug=slug
    )
    return TaskAccepted(task_id=task_id, message="反思已开始")


# --- 朋友圈 ---


@router.get("/exes/{slug}/moments")
def list_moments(slug: str, user_id: int = Depends(require_auth)):
    """获取朋友圈时间线。"""
    slug = _check_exe_access(slug, user_id)
    ex_dir = resolve_ex_dir(slug, user_id)
    moments_path = ex_dir / "moments.json"
    if not moments_path.exists():
        return {"moments": []}
    moments = json.loads(moments_path.read_text(encoding="utf-8"))
    return {"moments": moments}


@router.post("/exes/{slug}/moments/generate", response_model=TaskAccepted)
def generate_moment(slug: str, user_id: int = Depends(require_auth)):
    """生成一条朋友圈。"""
    slug = _check_exe_access(slug, user_id)

    from core.tasks import enqueue
    from server.task_handlers import TASK_MOMENT

    task_id = enqueue(TASK_MOMENT, user_id, {"slug": slug, "owner": user_id}, slug=slug)
    return TaskAccepted(task_id=task_id, message="朋友圈生成中")


# --- 版本管理 ---


@router.post("/exes/{slug}/backup", response_model=TaskAccepted)
def backup_exe(
    slug: str, req: Optional[BackupRequest] = None, user_id: int = Depends(require_auth)
):
    """备份版本。"""
    slug = _check_exe_access(slug, user_id)

    from core.tasks import enqueue
    from server.task_handlers import TASK_BACKUP

    task_id = enqueue(
        TASK_BACKUP,
        user_id,
        {
            "slug": slug,
            "owner": user_id,
            "version_name": req.version_name if req else "",
        },
        slug=slug,
    )
    return TaskAccepted(task_id=task_id, message="备份已开始")


@router.post("/exes/{slug}/rollback", response_model=StatusResponse)
def rollback_exe(slug: str, req: RollbackRequest, user_id: int = Depends(require_auth)):
    """回滚版本。"""
    slug = _check_exe_access(slug, user_id)
    from core.version_manager import rollback, list_versions

    try:
        rollback(slug, req.version, owner=user_id)
        _invalidate_engine(slug)
        return StatusResponse(message=f"已回滚到 {req.version}")
    except FileNotFoundError:
        versions = list_versions(slug, owner=user_id)
        raise HTTPException(
            status_code=404,
            detail=f"版本 {req.version} 不存在。可用: {versions}",
        )


@router.get("/exes/{slug}/versions")
def list_versions_route(slug: str, user_id: int = Depends(require_auth)):
    """列出版本。"""
    slug = _check_exe_access(slug, user_id)
    from core.version_manager import list_versions

    return {"slug": slug, "versions": list_versions(slug, owner=user_id)}


# --- 对话搜索 ---


@router.get("/exes/{slug}/messages/search")
def search_messages(
    slug: str,
    q: str = Query(..., min_length=1, max_length=200),
    user_id: int = Depends(require_auth),
):
    """全文搜索对话内容（基于 JSONL 归档）。"""
    slug = _check_exe_access(slug, user_id)
    from core.conversation_store import load_jsonl_messages

    messages = load_jsonl_messages(slug, owner=user_id)
    q_lower = q.lower()
    results = []
    for msg in messages:
        content = msg.get("content", "")
        if q_lower in content.lower():
            # 高亮匹配片段
            idx = content.lower().find(q_lower)
            start = max(0, idx - 30)
            end = min(len(content), idx + len(q) + 30)
            snippet = content[start:end]
            if start > 0:
                snippet = "…" + snippet
            if end < len(content):
                snippet = snippet + "…"
            results.append(
                {
                    "id": msg.get("id", ""),
                    "role": msg.get("role", ""),
                    "content": content,
                    "snippet": snippet,
                    "created_at": msg.get("created_at", ""),
                }
            )
    return {"results": results[-100:], "total": len(results)}


# --- 对话统计 ---


@router.get("/exes/{slug}/stats")
def get_stats(slug: str, user_id: int = Depends(require_auth)):
    """对话统计数据：总消息数、消息频率、活跃时段。"""
    slug = _check_exe_access(slug, user_id)

    # 检查缓存（键含用户维度，避免同名镜像统计串号）
    cache_key = f"stats:{user_id}:{slug}"
    cached = cache.get(cache_key)
    if cached:
        return cached

    from core.conversation_store import load_jsonl_messages
    from collections import Counter

    messages = load_jsonl_messages(slug, owner=user_id)
    total = len(messages)
    user_msgs = [m for m in messages if m.get("role") == "user"]
    assistant_msgs = [m for m in messages if m.get("role") == "assistant"]

    # 按日期统计消息频率
    daily: Counter[str] = Counter()
    hourly: Counter[int] = Counter()
    for m in messages:
        ts = m.get("created_at", "")
        if ts:
            try:
                dt = datetime.fromisoformat(ts)
                daily[dt.strftime("%Y-%m-%d")] += 1
                hourly[dt.hour] += 1
            except (ValueError, TypeError):
                pass

    # 活跃时段分布
    time_periods = {"凌晨(0-6)": 0, "上午(6-12)": 0, "下午(12-18)": 0, "晚上(18-24)": 0}
    for h, count in hourly.items():
        if h < 6:
            time_periods["凌晨(0-6)"] += count
        elif h < 12:
            time_periods["上午(6-12)"] += count
        elif h < 18:
            time_periods["下午(12-18)"] += count
        else:
            time_periods["晚上(18-24)"] += count

    result = {
        "total_messages": total,
        "user_messages": len(user_msgs),
        "assistant_messages": len(assistant_msgs),
        "daily_frequency": dict(sorted(daily.items(), reverse=True)[:30]),
        "hourly_distribution": dict(sorted(hourly.items())),
        "time_periods": time_periods,
    }

    # 缓存结果
    cache.set(cache_key, result, ttl=60)  # 缓存60秒
    return result


# --- 情感分析 ---


@router.get("/exes/{slug}/emotion")
def get_emotion(slug: str, user_id: int = Depends(require_auth)):
    """对话情感分析：整体情感倾向 + 情感曲线。"""
    slug = _check_exe_access(slug, user_id)
    from core.emotion_tracker import analyze_history, generate_emotion_curve

    history = _load_history(slug, owner=user_id)
    analysis = analyze_history(history)
    curve = generate_emotion_curve(history)
    return {"analysis": analysis, "curve": curve}


# --- 健康提醒 ---


@router.get("/user/health/check")
def health_check(user_id: int = Depends(require_auth)):
    """检查是否需要显示使用时长提醒。"""
    from core.health_tracker import health_tracker

    health_tracker.start_session(user_id)
    should_remind = health_tracker.should_remind(user_id)
    tip = health_tracker.get_health_tip()
    return {"should_remind": should_remind, "health_tip": tip}


@router.get("/user/health/stats")
def health_stats(user_id: int = Depends(require_auth)):
    """获取用户使用统计。

    数据源已从 HealthTracker 的进程内字典改为 user_activity 表：
    原实现重启清零、多设备各算各的，多副本下彻底错乱。
    """
    from server.usage_guard import status

    return status(user_id)


@router.get("/user/health/mindful")
def mindful_message(user_id: int = Depends(require_auth)):
    """获取正念引导消息。"""
    from core.health_tracker import health_tracker

    return {"message": health_tracker.get_mindful_message()}


# --- 多人镜像管理 ---


@router.put("/exes/{slug}/group")
def set_group(slug: str, group: str = Query(...), user_id: int = Depends(require_auth)):
    """设置镜像分组。"""
    slug = _check_exe_access(slug, user_id)
    meta = _load_meta(slug, user_id)
    meta["group"] = group
    _save_meta(slug, meta, user_id)
    return {"ok": True, "slug": slug, "group": group}


@router.get("/exes/groups")
def list_groups(user_id: int = Depends(require_auth)):
    """获取所有分组列表。"""
    groups: dict[str, list] = {}
    # 必须按归属过滤：镜像 slug/name 是前任昵称，泄露给其他用户属于越权信息暴露
    for exe_dir in iter_accessible_exes(user_id):
        meta_file = exe_dir / "meta.json"
        try:
            meta = json.loads(meta_file.read_text(encoding="utf-8"))
            group = meta.get("group", "默认")
            if group not in groups:
                groups[group] = []
            groups[group].append(
                {
                    "slug": exe_dir.name,
                    "name": meta.get("name", exe_dir.name),
                }
            )
        except (OSError, json.JSONDecodeError) as e:
            logger.warning("跳过损坏的 meta.json slug=%s: %s", exe_dir.name, e)

    return {"groups": groups}


# --- 关系阶段管理 ---


@router.get("/exes/{slug}/stage")
def get_stage(slug: str, user_id: int = Depends(require_auth)):
    """获取当前关系阶段。"""
    slug = _check_exe_access(slug, user_id)
    meta = _load_meta(slug, user_id)
    return {"stage": meta.get("stage", "dating")}


@router.put("/exes/{slug}/stage")
def set_stage(slug: str, stage: str = Query(...), user_id: int = Depends(require_auth)):
    """设置关系阶段。"""
    slug = _check_exe_access(slug, user_id)
    valid_stages = ["dating", "conflicted", "broken", "healing"]
    if stage not in valid_stages:
        raise HTTPException(status_code=400, detail=f"无效阶段，可选: {valid_stages}")
    meta = _load_meta(slug, user_id)
    meta["stage"] = stage
    _save_meta(slug, meta, user_id)
    # 缓存中的引擎还带着旧阶段，必须失效重建
    _invalidate_engine(slug)
    return {"ok": True, "slug": slug, "stage": stage}


@router.get("/exes/{slug}/stage/suggest")
def suggest_stage(slug: str, user_id: int = Depends(require_auth)):
    """根据对话情感趋势建议阶段变化。"""
    slug = _check_exe_access(slug, user_id)
    from core.emotion_tracker import analyze_history

    history = _load_history(slug, owner=user_id)
    if len(history) < 10:
        return {"suggestion": None, "reason": "对话记录不足"}

    # 分析最近 20 条消息的情感
    analysis = analyze_history(history[-20:])

    overall = analysis["overall"]["score"]
    user_score = analysis["user_sentiment"]["score"]

    # 读取当前阶段
    try:
        meta = _load_meta(slug, user_id)
        current_stage = meta.get("stage", "dating")
    except HTTPException:
        current_stage = "dating"

    # 根据情感趋势建议阶段
    suggestion = None
    reason = ""

    if current_stage == "dating" and overall < -0.3:
        suggestion = "conflicted"
        reason = "近期对话情感偏负面，可能进入磨合期"
    elif current_stage == "conflicted" and overall < -0.5:
        suggestion = "broken"
        reason = "情感持续恶化，可能已分手"
    elif current_stage == "broken" and overall > 0.2:
        suggestion = "healing"
        reason = "情感开始好转，可能在治愈中"
    elif current_stage == "healing" and overall > 0.4:
        suggestion = "dating"
        reason = "情感恢复正面，可能重新开始"

    return {
        "current_stage": current_stage,
        "suggestion": suggestion,
        "reason": reason,
        "overall_score": overall,
        "user_score": user_score,
    }


# --- 使用统计 ---


@router.get("/stats/usage")
def get_usage_stats(user_id: int = Depends(require_auth)):
    """获取用户使用统计。"""
    total_exes = 0
    total_messages = 0

    # 直接复用统一的归属过滤，避免和 list_exes 出现两套访问控制逻辑
    for exe_dir in iter_accessible_exes(user_id):
        total_exes += 1
        conv_file = exe_dir / "conversations" / "conversation.jsonl"
        if not conv_file.exists():
            continue
        try:
            with open(conv_file, "r", encoding="utf-8") as f:
                total_messages += sum(1 for _ in f)
        except OSError as e:
            logger.warning("统计对话数失败 slug=%s: %s", exe_dir.name, e)

    return {
        "total_exes": total_exes,
        "total_messages": total_messages,
        "user_id": user_id,
        "timestamp": datetime.now().isoformat(),
    }


@router.post("/feedback")
def submit_feedback(req: FeedbackRequest, user_id: int = Depends(require_auth)):
    """提交用户反馈。"""
    feedback_file = PROJECT_DIR / "data" / "feedback.jsonl"
    feedback_file.parent.mkdir(parents=True, exist_ok=True)

    entry = {
        "user_id": user_id,
        "type": req.feedback_type,
        "content": req.content,
        "timestamp": datetime.now().isoformat(),
    }

    try:
        with open(feedback_file, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except OSError as e:
        logger.error("写入反馈失败: %s", e)
        raise HTTPException(status_code=500, detail="反馈提交失败")

    return {"ok": True, "message": "感谢你的反馈"}


# --- 情感记忆 ---


@router.get("/exes/{slug}/emotional-memories")
def get_emotional_memories(slug: str, user_id: int = Depends(require_auth)):
    """获取情感记忆。"""
    slug = _check_exe_access(slug, user_id)
    from core.emotional_memory import load_emotional_memories

    memories = load_emotional_memories(slug, owner=user_id)
    return {"memories": memories}


@router.post("/exes/{slug}/emotional-memories/extract")
def extract_memories(slug: str, user_id: int = Depends(require_auth)):
    """从对话历史中提取情感记忆。"""
    slug = _check_exe_access(slug, user_id)
    from core.emotional_memory import (
        extract_emotional_memories,
        save_emotional_memories,
    )

    history = _load_history(slug, with_time=True, owner=user_id)

    memories = extract_emotional_memories(history)
    save_emotional_memories(slug, memories, owner=user_id)

    return {"ok": True, "memories": memories}


# --- 个性化 ---


@router.get("/exes/{slug}/user-profile")
def get_user_profile(slug: str, user_id: int = Depends(require_auth)):
    """获取用户画像。"""
    slug = _check_exe_access(slug, user_id)
    from core.personalization import load_user_profile

    profile = load_user_profile(slug, owner=user_id)
    return {"profile": profile}


@router.post("/exes/{slug}/user-profile/analyze")
def analyze_user_profile(slug: str, user_id: int = Depends(require_auth)):
    """分析用户对话风格。"""
    slug = _check_exe_access(slug, user_id)
    from core.personalization import (
        analyze_user_style,
        calculate_relationship_temperature,
        save_user_profile,
    )

    history = _load_history(slug, with_time=True, owner=user_id)

    style = analyze_user_style(history)
    temperature = calculate_relationship_temperature(slug, history)

    profile = {
        "style": style,
        "temperature": temperature,
        "analyzed_at": datetime.now().isoformat(),
    }

    save_user_profile(slug, profile, owner=user_id)
    return {"ok": True, "profile": profile}


@router.get("/exes/{slug}/relationship-temperature")
def get_relationship_temperature(slug: str, user_id: int = Depends(require_auth)):
    """获取关系温度。"""
    slug = _check_exe_access(slug, user_id)
    from core.personalization import calculate_relationship_temperature

    history = _load_history(slug, with_time=True, owner=user_id)

    temperature = calculate_relationship_temperature(slug, history)
    return {"temperature": temperature}


# --- 同意留痕与数据主体权利（M0：FR-015 ~ FR-019）---


@router.get("/consents")
def get_consents(user_id: int = Depends(require_auth)):
    """本人的完整同意历史，供数据主体查询与举证。"""
    from server.consent_store import list_consents

    return {"consents": list_consents(user_id)}


@router.post("/consents", response_model=StatusResponse)
def post_consent(
    req: ConsentRequest, request: Request, user_id: int = Depends(require_auth)
):
    """记录一次同意。协议改版需重新征得同意，所以版本必须带上。"""
    from server.consent_store import record_consent

    try:
        record_consent(
            user_id,
            req.policy_type,
            req.policy_version,
            ip=_get_client_ip(request),
            user_agent=request.headers.get("User-Agent", "")[:200],
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    return StatusResponse(message="已记录")


@router.get("/safety/crisis-resources")
def crisis_resources():
    """求助资源。刻意免登录：需要它的人未必登录得进来。"""
    from core.safety.resources import get_crisis_response

    resp = get_crisis_response()
    return {
        "message": resp.message,
        "hotlines": resp.hotlines,
        "reviewed": resp.reviewed,
    }


@router.post("/safety/report", response_model=StatusResponse)
def submit_subject_request(req: SubjectRequestPayload, request: Request):
    """被模拟者投诉与逝者近亲属主张。

    🔴 刻意免登录——被模拟者从来不是本站用户，逝者近亲属也多半不是。
    代价是必须有独立限流，否则就是个开放的滥用入口。
    """
    from server.consent_store import create_subject_request

    _get_login_limiter().check(
        f"subject_request:{req.contact[:64]}", _get_client_ip(request)
    )

    try:
        request_id = create_subject_request(
            req.claim_type,
            req.contact,
            target_slug=req.target_slug,
            target_hint=req.target_hint,
            detail=req.detail,
            identity_evidence=req.identity_evidence,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e

    _audit(
        "subject_request_received",
        client_ip=_get_client_ip(request),
        detail=f"id={request_id} type={req.claim_type}",
    )
    return StatusResponse(message=f"已受理，受理编号 {request_id}")


@router.post("/account/export")
def export_account_data(user_id: int = Depends(require_auth)):
    """导出本账号的全部个人信息。"""
    from fastapi.responses import FileResponse

    from server.account_lifecycle import export_account

    try:
        zip_path = export_account(user_id)
    except LookupError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    return FileResponse(
        str(zip_path),
        media_type="application/zip",
        filename=f"ex-memory-account-{user_id}.zip",
    )


@router.delete("/account", response_model=StatusResponse)
def delete_account_data(req: DeleteRequest, user_id: int = Depends(require_auth)):
    """注销账号并级联删除全部个人信息。"""
    if not req.confirm:
        raise HTTPException(status_code=400, detail="需要确认注销")

    from server.account_lifecycle import delete_account, verify_deletion

    receipt = delete_account(user_id)
    residues = verify_deletion(user_id)
    if residues:
        # 删不干净必须显式暴露，不能给用户一张假的删除回执
        logger.error("账号删除残留 user_id=%s: %s", user_id, residues)
        raise HTTPException(status_code=500, detail="删除未完成，请联系管理员")

    _audit(
        "account_deleted", username=f"user_id={user_id}", detail=str(receipt["exes"])
    )
    return StatusResponse(message="账号及全部数据已删除")


# --- 管理员：安全复核与数据主体请求处置 ---
#
# 🔴 这些端点必须是管理员专属：复核队列里是用户最脆弱时刻的记录与
# 被模拟者的投诉，任何登录用户都能翻看是不可接受的。


@router.get("/admin/safety/reviews")
def list_safety_reviews(limit: int = 50, admin_id: int = Depends(require_admin)):
    """待人工复核的安全事件队列，高严重度优先。"""
    from server.safety_store import list_pending_reviews

    return {"events": list_pending_reviews(limit=min(limit, 200))}


@router.post("/admin/safety/reviews/{event_id}", response_model=StatusResponse)
def resolve_safety_review(
    event_id: int, req: ReviewResolution, admin_id: int = Depends(require_admin)
):
    """标记安全事件的复核结果。"""
    from server.safety_store import resolve_review

    try:
        ok = resolve_review(event_id, f"user_id={admin_id}", req.status, req.note)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    if not ok:
        raise HTTPException(status_code=404, detail="事件不存在")
    _audit(
        "safety_review_resolved",
        username=f"user_id={admin_id}",
        detail=f"event={event_id} status={req.status}",
    )
    return StatusResponse(message="已记录复核结果")


@router.get("/admin/subject-requests")
def list_subject_request_queue(
    status: str = "received", admin_id: int = Depends(require_admin)
):
    """被模拟者投诉与近亲属主张的工单队列。"""
    from server.consent_store import list_subject_requests

    return {"requests": list_subject_requests(status=status)}


@router.post("/admin/subject-requests/{request_id}", response_model=StatusResponse)
def resolve_subject_request_route(
    request_id: int, req: ReviewResolution, admin_id: int = Depends(require_admin)
):
    """处置一条数据主体请求。"""
    from server.consent_store import resolve_subject_request

    try:
        ok = resolve_subject_request(
            request_id, f"user_id={admin_id}", req.status, req.note
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    if not ok:
        raise HTTPException(status_code=404, detail="请求不存在")
    _audit(
        "subject_request_resolved",
        username=f"user_id={admin_id}",
        detail=f"request={request_id} status={req.status}",
    )
    return StatusResponse(message="已处置")


# --- 异步任务（FR-036 / FR-037）---


@router.get("/tasks")
def list_user_tasks(limit: int = 20, user_id: int = Depends(require_auth)):
    """本人的任务列表。"""
    from core.tasks import list_tasks

    return {"tasks": [_task_view(t) for t in list_tasks(user_id, min(limit, 100))]}


@router.get("/tasks/{task_id}")
def get_task_status(task_id: str, user_id: int = Depends(require_auth)):
    """查询单个任务。"""
    from core.tasks import get_task

    task = get_task(task_id, user_id)
    if task is None:
        # 不区分「不存在」与「不属于你」，避免任务 ID 的存在性泄漏
        raise HTTPException(status_code=404, detail="任务不存在")
    return _task_view(task)


@router.post("/tasks/{task_id}/retry", response_model=TaskAccepted)
def retry_task(task_id: str, user_id: int = Depends(require_auth)):
    """重试失败的任务。只有失败态可重试。"""
    from core.tasks import retry

    if not retry(task_id, user_id):
        raise HTTPException(status_code=400, detail="任务不存在或当前状态不可重试")
    return TaskAccepted(task_id=task_id, message="已重新排队")


@router.get("/tasks/{task_id}/stream")
async def stream_task_progress(task_id: str, user_id: int = Depends(require_auth)):
    """SSE 推送任务进度，终态后结束。

    轮询接口已经够用，这个是给导入这类长任务的进度条用的——
    每秒一次轮询在导入几分钟的场景下噪音太大。
    """
    from core.tasks import STATUS_FAILED, STATUS_SUCCEEDED, get_task

    if get_task(task_id, user_id) is None:
        raise HTTPException(status_code=404, detail="任务不存在")

    async def generate():
        import asyncio

        last_payload = None
        # 兜底超时：任务卡死时不能让连接永远挂着
        for _ in range(int(config.TASK_STREAM_TIMEOUT_SECONDS * 2)):
            task = get_task(task_id, user_id)
            if task is None:
                break
            payload = _task_view(task)
            if payload != last_payload:
                yield f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"
                last_payload = payload
            if task["status"] in (STATUS_SUCCEEDED, STATUS_FAILED):
                break
            await asyncio.sleep(0.5)
        yield "data: [DONE]\n\n"

    return StreamingResponse(generate(), media_type="text/event-stream")


def _task_view(task: dict) -> dict:
    """任务的对外视图。不暴露 payload——里面可能有暂存文件路径。"""
    result = task.get("result")
    return {
        "task_id": task["id"],
        "type": task["task_type"],
        "slug": task.get("slug"),
        "status": task["status"],
        "progress": task.get("progress", 0),
        "detail": task.get("detail"),
        "result": json.loads(result) if result else None,
        "error": task.get("error"),
        "attempts": task.get("attempts", 0),
        "created_at": task.get("created_at"),
        "finished_at": task.get("finished_at"),
    }


# --- 会话（FR-040）---


@router.post("/auth/refresh")
def refresh_token_route(req: RefreshRequest, request: Request):
    """用 refresh 换一对新令牌。旧的立即作废。"""
    from server.auth import refresh_session

    session = refresh_session(
        req.refresh_token,
        user_agent=request.headers.get("User-Agent", ""),
        ip=_get_client_ip(request),
    )
    if session is None:
        raise HTTPException(status_code=401, detail="刷新令牌无效或已过期，请重新登录")
    return session


@router.post("/auth/revoke-all", response_model=StatusResponse)
def revoke_all(user_id: int = Depends(require_auth)):
    """登出全部设备。"""
    from server.auth import revoke_all_sessions

    count = revoke_all_sessions(user_id)
    _audit("sessions_revoked", username=f"user_id={user_id}", detail=f"count={count}")
    return StatusResponse(message=f"已登出 {count} 个会话")


@router.get("/auth/sessions")
def list_active_sessions(user_id: int = Depends(require_auth)):
    """查看当前有哪些设备登录着。"""
    from server.auth import list_sessions

    return {"sessions": list_sessions(user_id)}
