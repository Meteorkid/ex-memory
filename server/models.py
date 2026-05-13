"""Pydantic 请求/响应模型。"""

from pydantic import BaseModel, Field
from typing import Optional


# --- 请求模型 ---

class CreateRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=64, description="前任名字")
    slug: str = Field(..., min_length=1, max_length=64, pattern=r"^[a-zA-Z0-9_一-鿿]+$")
    answers: list[str] = Field(default_factory=list, description="intake 三问回答")

class ChatRequest(BaseModel):
    slug: str = Field(..., description="镜像名称")
    message: str = Field(..., min_length=1, max_length=8000)
    history: list[dict] = Field(default_factory=list)

class UpdateRequest(BaseModel):
    slug: str = Field(..., description="镜像名称")
    content: str = Field(..., min_length=1, description="新素材内容")
    source_type: str = Field(default="oral", pattern=r"^(wechat|oral|photo)$")

class ReflectRequest(BaseModel):
    slug: str = Field(..., description="镜像名称")

class BackupRequest(BaseModel):
    slug: str = Field(..., description="镜像名称")
    version_name: str = Field(default="")

class RollbackRequest(BaseModel):
    slug: str = Field(..., description="镜像名称")
    version: str = Field(..., min_length=1)

class DeleteRequest(BaseModel):
    confirm: bool = Field(default=False)

class TransferRequest(BaseModel):
    amount: float = Field(..., gt=0, le=200, description="转账金额")
    note: str = Field(default="", description="转账备注")
    direction: str = Field(default="ta_to_me", pattern=r"^(ta_to_me|me_to_ta)$")

class TransferConfirmRequest(BaseModel):
    action: str = Field(default="receive", pattern=r"^(receive|return)$")


# --- 响应模型 ---

class ExeInfo(BaseModel):
    slug: str
    name: str
    state: str
    created_at: str
    updated_at: Optional[str] = None

class ChatResponse(BaseModel):
    reply: str
    stickers: list[str] = Field(default_factory=list, description="AI 选择的贴纸 ID")
    tokens: Optional[dict] = None

class StatusResponse(BaseModel):
    ok: bool = True
    message: str = ""

class ErrorResponse(BaseModel):
    ok: bool = False
    error: str
    detail: Optional[str] = None


class AuthRequest(BaseModel):
    username: str = Field(..., min_length=2, max_length=64)
    password: str = Field(..., min_length=6, max_length=128)

class LogoutRequest(BaseModel):
    token: str = Field(..., description="要注销的 token")
