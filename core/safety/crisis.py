"""危机意念识别。

设计取舍：**宁可误报不可漏报**。漏掉一条真实求救的代价，远大于
误打断一次正常对话。因此规则倾向宽松，由上层用「中断角色扮演 +
转介求助资源」这种低伤害的响应来吸收误报成本。

两条通道：
1. 规则通道（本模块，本地、无网络、亚毫秒级）—— 已实现
2. 语义通道 —— 预留接口 `SemanticDetector`，默认关闭。
   规则通道覆盖不了改写与隐喻，上线前应接入语义模型；
   在那之前不要对外宣称达到了标注集上的召回率。
"""

import re
from dataclasses import dataclass
from typing import Optional, Protocol

# 严重度：high 触发立即中断，medium 同样中断但复核优先级低一档。
# 不设 low —— 拿不准的一律按 medium 处理，符合宁可误报的取向。
SEVERITY_HIGH = "high"
SEVERITY_MEDIUM = "medium"


@dataclass(frozen=True)
class CrisisSignal:
    """识别结果。刻意不携带用户原话，避免明文流入日志与事件表。"""

    hit: bool
    severity: str = ""
    confidence: float = 0.0
    detector: str = ""
    rule: str = ""

    @classmethod
    def miss(cls) -> "CrisisSignal":
        return cls(hit=False)


class SemanticDetector(Protocol):
    """语义通道接口。选型未定，先留插槽。"""

    def detect(self, text: str) -> Optional[CrisisSignal]: ...


# ── 规则表 ──
# 每条规则 (名称, 严重度, 置信度, 正则)。名称会落进 safety_events.detector，
# 便于事后统计各规则的命中与误报分布，所以要稳定、可读、不含用户内容。
_RULES: list[tuple[str, str, float, str]] = [
    # 直接表达
    # 「想死你了 / 想死我了」是想念；「笑死 / 累死」是程度副词，都要排除。
    # 但「了」「个」不是人称代词，不能放进排除集，否则「我想死了」会漏掉。
    (
        "direct.want_die",
        SEVERITY_HIGH,
        0.95,
        r"(?<![笑累困饿气热冷疼吓乐])想死(?![你妳我他她它])",
    ),
    ("direct.not_want_live", SEVERITY_HIGH, 0.95, r"不想(再)?(活|活着|活下去)"),
    ("direct.cannot_live", SEVERITY_HIGH, 0.95, r"活不下去"),
    ("direct.suicide_word", SEVERITY_HIGH, 0.95, r"自杀"),
    (
        "direct.end_life",
        SEVERITY_HIGH,
        0.9,
        r"(结束|了结|终结)(我|自己)?(的)?(生命|一生|这一切|这条命)",
    ),
    ("direct.better_dead", SEVERITY_HIGH, 0.9, r"死了算了"),
    ("direct.once_and_for_all", SEVERITY_HIGH, 0.85, r"一了百了"),
    ("direct.end_it_all", SEVERITY_HIGH, 0.8, r"就这么结束|要不要.{0,4}结束"),
    ("direct.go_die", SEVERITY_HIGH, 0.9, r"(我)?就(去)?死|去死了?$"),
    (
        "direct.painful_alive",
        SEVERITY_HIGH,
        0.85,
        r"活着(好|很|真)?(痛苦|难受).{0,6}(结束|解脱|死)",
    ),
    # 自伤
    ("self_harm.cut", SEVERITY_HIGH, 0.95, r"割腕|划(了)?(自己)?(的)?手腕"),
    ("self_harm.general", SEVERITY_HIGH, 0.9, r"自残|伤害自己"),
    # 计划与手段
    (
        "plan.pills_stockpile",
        SEVERITY_HIGH,
        0.9,
        r"(攒|囤|存)了.{0,6}药|一整瓶药|吞了.{0,6}药|吃了.{0,4}片.{0,10}(晕|难受)",
    ),
    (
        "plan.height",
        SEVERITY_HIGH,
        0.9,
        r"(站在|在).{0,4}(天台|楼顶|楼顶上)|(跳|跳下)(楼|去)",
    ),
    ("plan.hanging", SEVERITY_HIGH, 0.9, r"上吊|绳子.{0,6}准备好"),
    ("plan.charcoal", SEVERITY_HIGH, 0.9, r"烧炭|买了炭"),
    ("plan.planning_exit", SEVERITY_HIGH, 0.9, r"计划.{0,6}(离开这个世界|结束)"),
    # 被动意念
    ("passive.cannot_hold", SEVERITY_MEDIUM, 0.8, r"(撑|挺)不(下去|住)"),
    ("passive.meaningless", SEVERITY_MEDIUM, 0.8, r"活着(还)?有什么(意思|意义|盼头)"),
    (
        "passive.better_without_me",
        SEVERITY_HIGH,
        0.9,
        r"没有我.{0,8}(更好|解脱|干净|轻松)|我消失了?.{0,8}(都)?好|(走|离开)了?对(大家|你们|所有人).{0,4}好",
    ),
    (
        "passive.disappear",
        SEVERITY_MEDIUM,
        0.8,
        r"(彻底|永远|好想)?想?(彻底|永远)?消失|不想存在|能让我不存在",
    ),
    ("passive.permanent_relief", SEVERITY_MEDIUM, 0.8, r"(永远|彻底)解脱"),
    (
        "passive.never_wake",
        SEVERITY_HIGH,
        0.9,
        r"(再也)?不(用|想)?醒(来|过来)|睡着了?.{0,6}不(用|想)醒",
    ),
    (
        "passive.burden",
        SEVERITY_MEDIUM,
        0.8,
        r"我是(个)?累赘|不配活|不值得被留下|不想看见明天",
    ),
    (
        "passive.exit_world",
        SEVERITY_MEDIUM,
        0.8,
        r"(退出|离开)这个世界|从这个世界(退出|消失)",
    ),
    ("passive.dying_no_rescue", SEVERITY_HIGH, 0.85, r"(慢慢)?死掉.{0,8}不想(抢救|治)"),
    # 丧失语境特有：与逝去/离开的对象「团聚」的表达
    (
        "loss.join_them",
        SEVERITY_HIGH,
        0.9,
        r"去(陪|找)(你|他|她|ta|TA)(?!.{0,4}(吃|玩|逛|上班|谈|拿|聊))"
        r"|(下面|那边|天上).{0,4}(找|陪|见)(你|他|她|ta|TA)",
    ),
    (
        "loss.reunite_soon",
        SEVERITY_HIGH,
        0.9,
        r"等我.{0,8}(就来|来找)|很快就(能|来|去).{0,6}(找你|在一起|来了)"
        r"|到那边.{0,6}重逢|马上就来",
    ),
    (
        "loss.go_together",
        SEVERITY_HIGH,
        0.85,
        r"跟(你|他|她|ta)一起走(?!.{0,4}(路|去|吧))",
    ),
    # 告别与后事
    (
        "farewell.last_talk",
        SEVERITY_HIGH,
        0.85,
        r"最后一次(跟|和|同).{0,4}(说话|聊|见面)",
    ),
    ("farewell.no_return", SEVERITY_MEDIUM, 0.8, r"(以后)?不会再来了|再也不会来了"),
    ("farewell.will", SEVERITY_HIGH, 0.95, r"遗书|遗言|后事"),
    (
        "farewell.handover",
        SEVERITY_HIGH,
        0.85,
        r"(账号|密码|东西).{0,8}留给|托付给.{0,10}(没有|无)牵挂|没有牵挂了",
    ),
]

_COMPILED = [(name, sev, conf, re.compile(pat)) for name, sev, conf, pat in _RULES]


class KeywordDetector:
    """规则通道：本地正则，无网络依赖，满足 P95 < 200ms 的硬要求。"""

    name = "keyword"

    def detect(self, text: str) -> Optional[CrisisSignal]:
        if not text:
            return None
        # 命中多条时取严重度最高、其次置信度最高的一条，保证响应一致
        best: Optional[CrisisSignal] = None
        for rule_name, severity, confidence, pattern in _COMPILED:
            if not pattern.search(text):
                continue
            signal = CrisisSignal(
                hit=True,
                severity=severity,
                confidence=confidence,
                detector=self.name,
                rule=rule_name,
            )
            if best is None or _rank(signal) > _rank(best):
                best = signal
        return best


def _rank(signal: CrisisSignal) -> tuple[int, float]:
    return (1 if signal.severity == SEVERITY_HIGH else 0, signal.confidence)


_keyword_detector = KeywordDetector()
_semantic_detector: Optional[SemanticDetector] = None


def set_semantic_detector(detector: Optional[SemanticDetector]) -> None:
    """注入语义通道。选型确定后在应用启动时调用。"""
    global _semantic_detector
    _semantic_detector = detector


def detect_crisis(text: str) -> CrisisSignal:
    """检测危机意念。任一通道命中即返回命中，取更严重的一条。

    永远返回 CrisisSignal，不抛异常：这条路径在每条用户消息上执行，
    检测器自身故障绝不能让对话链路挂掉——但故障要留日志。
    """
    signals: list[CrisisSignal] = []
    for detector in (_keyword_detector, _semantic_detector):
        if detector is None:
            continue
        try:
            result = detector.detect(text)
        except Exception:  # noqa: BLE001 — 检测器故障不得中断主链路
            import logging

            logging.getLogger("ex-memory").exception("危机检测器异常，已跳过该通道")
            continue
        if result is not None and result.hit:
            signals.append(result)
    if not signals:
        return CrisisSignal.miss()
    return max(signals, key=_rank)
