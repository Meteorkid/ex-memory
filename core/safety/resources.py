"""危机干预文案与求助资源的加载与审阅门禁。

文案与热线号码不写在代码里，放在 content/crisis_resources.zh-CN.json，
由具备心理专业背景的人员审阅后才允许启用。未审阅时只用最保守的兜底文案，
不展示任何具体号码 —— 宁可不给号码，也不给错号码。
"""

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

logger = logging.getLogger("ex-memory")

CONTENT_PATH = (
    Path(__file__).resolve().parent.parent.parent
    / "content"
    / "crisis_resources.zh-CN.json"
)


@dataclass(frozen=True)
class CrisisResponse:
    """呈现给用户的危机响应内容。"""

    message: str
    hotlines: list[dict] = field(default_factory=list)
    followup_note: str = ""
    reviewed: bool = False


# 代码内兜底：内容文件缺失或损坏时仍要有话可说，绝不能因为读文件失败
# 就让危机流程静默失效、把用户丢回人格模拟。
_HARDCODED_FALLBACK = (
    "抱歉，我没有办法继续这段对话了。\n"
    "如果你正在经历难以承受的痛苦，请立刻联系你信任的人，"
    "或前往就近的医院急诊科寻求专业帮助。"
)

_cache: Optional[dict] = None
_warned = False


def _load() -> dict:
    global _cache
    if _cache is not None:
        return _cache
    try:
        _cache = json.loads(CONTENT_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        logger.error("危机资源文件不可用（%s），退回代码内兜底文案", e)
        _cache = {}
    return _cache


def reset_cache() -> None:
    """测试用：清掉缓存，让下次读取重新加载。"""
    global _cache, _warned
    _cache = None
    _warned = False


def is_reviewed() -> bool:
    """文案是否已通过专业审阅。"""
    return bool(_load().get("reviewed"))


def warn_if_unreviewed() -> None:
    """启动时检查。未审阅不阻止启动，但必须让运维看见。"""
    global _warned
    if is_reviewed() or _warned:
        return
    _warned = True
    logger.warning(
        "危机干预文案尚未经专业审阅（content/crisis_resources.zh-CN.json 的 "
        "reviewed=false）。当前只使用保守兜底文案，不展示任何求助热线号码。"
        "上线前必须完成审阅并核实热线有效性。"
    )


def get_crisis_response() -> CrisisResponse:
    """取危机响应内容。未审阅时只给兜底文案，不带热线。"""
    data = _load()
    if not data.get("reviewed"):
        return CrisisResponse(
            message=data.get("fallback_message") or _HARDCODED_FALLBACK,
            hotlines=[],
            followup_note=data.get("followup_note", ""),
            reviewed=False,
        )
    return CrisisResponse(
        message=data.get("reviewed_message") or _HARDCODED_FALLBACK,
        hotlines=[h for h in data.get("hotlines", []) if h.get("number")],
        followup_note=data.get("followup_note", ""),
        reviewed=True,
    )
