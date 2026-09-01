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


def _review_gaps(data: dict) -> list[str]:
    """列出「声称已审阅」与「确实审阅过」之间的缺口。

    把 reviewed 做成一个不能随手翻的开关：光写 true 不算数，必须留下
    是谁、什么时候审的，以及每条对外展示的热线都被逐条核实过。
    否则一次顺手的改动就能让未经核对的号码上线。
    """
    gaps: list[str] = []
    if not str(data.get("reviewed_by", "")).strip():
        gaps.append("缺少 reviewed_by（审阅人署名）")
    if not str(data.get("reviewed_at", "")).strip():
        gaps.append("缺少 reviewed_at（审阅日期）")
    if not str(data.get("hotlines_verified_at", "")).strip():
        gaps.append("缺少 hotlines_verified_at（热线核实日期）")
    return gaps


def _verified_hotlines(data: dict) -> list[dict]:
    """只返回既有号码又逐条核实过的热线，并对未核实项告警。

    刻意不让「有一条未核实」整体阻断：审阅人往文件里加一条待研究的备选，
    不应该让所有热线一起消失——那个失败模式比它防的问题更糟。
    未核实的条目不展示即可，同时留下告警让人发现。
    """
    verified, pending = [], []
    for hotline in data.get("hotlines", []):
        if not hotline.get("number"):
            continue  # 空号码是占位，本就不展示
        if str(hotline.get("verified_at", "")).strip():
            verified.append(hotline)
        else:
            pending.append(str(hotline.get("number")))
    if pending:
        logger.warning("以下热线尚未逐条核实，不会展示给用户：%s", "、".join(pending))
    return verified


def is_reviewed() -> bool:
    """文案是否已通过专业审阅，且审阅痕迹齐备。

    任一条件不满足都按未审阅处理——降级是安全的方向。
    """
    data = _load()
    if not data.get("reviewed"):
        return False
    gaps = _review_gaps(data)
    if gaps:
        logger.error(
            "危机资源标记为已审阅，但审阅痕迹不完整，按未审阅处理：%s",
            "；".join(gaps),
        )
        return False
    return True


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
    if not is_reviewed():
        return CrisisResponse(
            message=data.get("fallback_message") or _HARDCODED_FALLBACK,
            hotlines=[],
            followup_note=data.get("followup_note", ""),
            reviewed=False,
        )
    hotlines = _verified_hotlines(data)
    if not hotlines:
        # 已审阅但没有任何号码通过核实：reviewed_message 里写着「请联系下面的
        # 热线」，此时展示它就是承诺了不存在的东西。退回不承诺热线的兜底文案。
        logger.error("危机资源已审阅，但没有任何热线通过核实，退回兜底文案")
        return CrisisResponse(
            message=data.get("fallback_message") or _HARDCODED_FALLBACK,
            hotlines=[],
            followup_note=data.get("followup_note", ""),
            reviewed=False,
        )
    return CrisisResponse(
        message=data.get("reviewed_message") or _HARDCODED_FALLBACK,
        hotlines=hotlines,
        followup_note=data.get("followup_note", ""),
        reviewed=True,
    )
