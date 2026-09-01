"""Pydantic 请求/响应模型。"""

from pydantic import BaseModel, Field
from typing import Optional


# --- 请求模型 ---


class CreateRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=64, description="前任名字")
    slug: str = Field(..., min_length=1, max_length=64, pattern=r"^[a-zA-Z0-9_一-鿿]+$")
    answers: list[str] = Field(default_factory=list, description="intake 三问回答")


class ResumeRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=64, description="前任名字")
    slug: str = Field(..., min_length=1, max_length=64, pattern=r"^[a-zA-Z0-9_一-鿿]+$")


class ChatRequest(BaseModel):
    slug: str = Field(..., description="镜像名称")
    message: str = Field(default="", max_length=8000)
    sticker_id: Optional[str] = Field(
        default=None, description="贴纸 ID（发送贴纸消息时使用）"
    )
    history: list[dict] = Field(default_factory=list)


class UpdateRequest(BaseModel):
    slug: str = Field(..., description="镜像名称")
    content: str = Field(..., min_length=1, max_length=500000, description="新素材内容")
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


class FeedbackRequest(BaseModel):
    feedback_type: str = Field(..., min_length=1, max_length=32, description="反馈类型")
    content: str = Field(..., min_length=1, max_length=2000, description="反馈正文")


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
    # 平台身份的通知（当前只有危机干预）。存在时 reply 为空，
    # 前端必须按系统消息渲染，不能显示成镜像说的话。
    notice: Optional[dict] = Field(default=None, description="平台通知，如危机干预响应")


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
    # 实名与年龄门槛（FR-020 / FR-022）。登录时不需要，仅注册校验。
    phone: Optional[str] = Field(default=None, max_length=20)
    code: Optional[str] = Field(default=None, max_length=10)
    age_confirmed: bool = Field(
        default=False, description="确认已成年。本产品性质不适合未成年人"
    )


class PhoneCodeRequest(BaseModel):
    phone: str = Field(..., max_length=20)


class LogoutRequest(BaseModel):
    token: str = Field(..., description="要注销的 token")


class ConsentRequest(BaseModel):
    """同意留痕。版本必须带上——协议改版要重新征得同意。"""

    policy_type: str = Field(
        description="terms / privacy / third_party_data / emotion_analysis"
    )
    policy_version: str = Field(min_length=1, max_length=32)


class SubjectRequestPayload(BaseModel):
    """数据主体请求：被模拟者投诉、逝者近亲属主张。免登录提交。"""

    claim_type: str = Field(description="subject_complaint / deceased_kin / other")
    contact: str = Field(min_length=1, max_length=200)
    target_slug: Optional[str] = Field(default=None, max_length=64)
    target_hint: Optional[str] = Field(
        default=None, max_length=500, description="定位线索：昵称、时间段等"
    )
    detail: Optional[str] = Field(default=None, max_length=2000)
    identity_evidence: Optional[str] = Field(
        default=None, max_length=500, description="身份材料的引用，不在此提交材料本身"
    )
