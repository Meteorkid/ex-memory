"""账号模型配置 API：不回显凭据，连校验和认证错误也禁止缓存。"""

import json
import logging

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute
from pydantic import BaseModel, ConfigDict, Field, SecretStr, ValidationError

from core import kv
from server.middleware import require_auth
from server.model_settings import (
    EmbeddingModelLocked,
    MissingModelSettings,
    SettingsConflict,
    SettingsUnavailable,
    SettingsValidationError,
    get_public_settings,
    update_model_settings,
)

logger = logging.getLogger("ex-memory")
_NO_STORE = {"Cache-Control": "no-store"}
_INVALIDATION_PREFIX = "@model-settings:"


class _PrivateRoute(APIRoute):
    def get_route_handler(self):
        original = super().get_route_handler()

        async def handler(request: Request):
            try:
                response = await original(request)
            except HTTPException as exc:
                response = JSONResponse(
                    {"detail": exc.detail},
                    status_code=exc.status_code,
                    headers=exc.headers,
                )
            except (SettingsConflict, EmbeddingModelLocked) as exc:
                response = JSONResponse({"detail": str(exc)}, status_code=409)
            except (SettingsValidationError, MissingModelSettings) as exc:
                response = JSONResponse({"detail": str(exc)}, status_code=400)
            except SettingsUnavailable as exc:
                response = JSONResponse({"detail": str(exc)}, status_code=503)
            except Exception as exc:  # noqa: BLE001 — 凭据接口不回传底层错误或请求体
                logger.error("模型配置接口失败 type=%s", type(exc).__name__)
                response = JSONResponse(
                    {"detail": "模型配置暂不可用，请稍后重试"}, status_code=500
                )
            response.headers.update(_NO_STORE)
            return response

        return handler


router = APIRouter(prefix="/api/account/model-settings", route_class=_PrivateRoute)


class ModelUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    provider: str = ""
    base_url: str = ""
    model: str = ""
    api_key: SecretStr = SecretStr("")
    delete_key: bool = False


class SettingsUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    expected_revision: int = Field(ge=0)
    llm: ModelUpdate = Field(default_factory=ModelUpdate)
    embedding: ModelUpdate = Field(default_factory=ModelUpdate)


def _evict_user_model_caches(message: str) -> None:
    if not message.startswith(_INVALIDATION_PREFIX):
        return
    suffix = message[len(_INVALIDATION_PREFIX) :]
    if not suffix.isascii() or not suffix.isdigit():
        return
    from server.routes import _engine_cache
    from config import _account_llm_clients
    from core.llm_router import _account_routers

    user_id = int(suffix)
    for cache in (_engine_cache, _account_llm_clients, _account_routers):
        cache.evict_where(lambda key: key[0] == user_id)


kv.on_invalidate(_evict_user_model_caches)


def invalidate_model_settings(user_id: int) -> None:
    kv.broadcast_invalidate(f"{_INVALIDATION_PREFIX}{user_id}")


def _reject_query_parameters(request: Request) -> None:
    if request.query_params:
        raise HTTPException(status_code=400, detail="模型配置接口不接受查询参数")


@router.get("")
def read_model_settings(request: Request, user_id: int = Depends(require_auth)):
    _reject_query_parameters(request)
    return get_public_settings(user_id)


@router.put("")
async def save_model_settings(request: Request, user_id: int = Depends(require_auth)):
    _reject_query_parameters(request)
    body = bytearray()
    async for chunk in request.stream():
        if len(body) + len(chunk) > 16384:
            raise HTTPException(status_code=413, detail="模型配置请求过大")
        body.extend(chunk)
    try:
        payload = SettingsUpdate.model_validate(json.loads(body))
    except (json.JSONDecodeError, UnicodeDecodeError):
        raise HTTPException(status_code=400, detail="请求必须是有效 JSON") from None
    except ValidationError:
        # Pydantic 默认 errors() 携带 input，SecretStr 的格式错误也可能包含原文。
        # 固定文案既不泄露输入值，也不把攻击者自造字段名当 loc 回显。
        raise HTTPException(status_code=422, detail="模型配置字段或类型无效") from None
    updates = payload.model_dump(exclude_unset=True, exclude={"expected_revision"})
    for update in updates.values():
        secret = update.get("api_key")
        if secret is not None:
            update["api_key"] = secret.get_secret_value()
    result = update_model_settings(user_id, payload.expected_revision, **updates)
    invalidate_model_settings(user_id)
    return result
