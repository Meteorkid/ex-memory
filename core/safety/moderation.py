"""内容审核：输入与输出双向。

分层设计：
- **主通道**是第三方内容安全服务（阿里云内容安全 / 网易易盾等）。选型未定，
  这里只定义接口，不绑定任何一家 SDK。
- **兜底通道**是本地词表，永远可用、无网络依赖。第三方不可用时自动降级，
  不裸奔；降级必须留日志，否则「审核形同虚设」这件事无人察觉。

🔴 危机识别优先级高于本模块。一条消息既命中危机又命中违规词时走危机流程，
不能因为「违规」把正在求救的人挡回去。调用方负责保证这个顺序。
"""

import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Protocol

logger = logging.getLogger("ex-memory")

WORDLIST_PATH = (
    Path(__file__).resolve().parent.parent.parent
    / "content"
    / "moderation_wordlist.zh-CN.json"
)

DIRECTION_INPUT = "input"
DIRECTION_OUTPUT = "output"

SEVERITY_BLOCK = "block"
SEVERITY_FLAG = "flag"


@dataclass(frozen=True)
class ModerationResult:
    """审核结论。不携带用户原话，避免明文流进日志。"""

    allowed: bool
    category: str = ""
    severity: str = ""
    detector: str = ""

    @classmethod
    def ok(cls, detector: str = "") -> "ModerationResult":
        return cls(allowed=True, detector=detector)


class ModerationProvider(Protocol):
    """第三方内容安全服务接口。选型确定后实现它并注入。"""

    name: str

    def check(self, text: str, direction: str) -> ModerationResult: ...


_wordlist_cache: Optional[list[dict]] = None


def reset_wordlist_cache() -> None:
    global _wordlist_cache
    _wordlist_cache = None


def _load_wordlist() -> list[dict]:
    global _wordlist_cache
    if _wordlist_cache is not None:
        return _wordlist_cache
    try:
        data = json.loads(WORDLIST_PATH.read_text(encoding="utf-8"))
        _wordlist_cache = data.get("categories", [])
    except (OSError, json.JSONDecodeError) as e:
        # 词表不可用时返回空表：兜底通道失效必须显式告警，
        # 但不能因此把整条对话链路弄挂
        logger.error("本地审核词表不可用（%s），兜底通道当前为空", e)
        _wordlist_cache = []
    return _wordlist_cache


class LocalWordlistProvider:
    """兜底通道：本地词表，永远可用。"""

    name = "local_wordlist"

    def check(self, text: str, direction: str) -> ModerationResult:
        if not text:
            return ModerationResult.ok(self.name)
        for category in _load_wordlist():
            severity = category.get("severity", SEVERITY_BLOCK)
            for term in category.get("terms", []):
                if term and term in text:
                    return ModerationResult(
                        allowed=severity != SEVERITY_BLOCK,
                        category=category.get("name", "unknown"),
                        severity=severity,
                        detector=self.name,
                    )
        return ModerationResult.ok(self.name)


class NoopProvider:
    """开发用空实现：一律放行。生产环境绝不能用它当主通道。"""

    name = "noop"

    def check(self, text: str, direction: str) -> ModerationResult:
        return ModerationResult.ok(self.name)


_local = LocalWordlistProvider()
_provider: Optional[ModerationProvider] = None


def set_provider(provider: Optional[ModerationProvider]) -> None:
    """注入第三方主通道。选型确定后在应用启动时调用。"""
    global _provider
    _provider = provider


def has_provider() -> bool:
    return _provider is not None


def moderate(text: str, direction: str) -> ModerationResult:
    """审核文本。主通道故障时降级到本地词表，绝不静默放行。

    两条通道都跑：主通道判定优先，但本地词表命中 block 时同样拦截——
    第三方漏判的情况下本地表还能兜一层。
    """
    local_result = _local.check(text, direction)

    if _provider is not None:
        try:
            remote_result = _provider.check(text, direction)
            if not remote_result.allowed:
                return remote_result
        except Exception as e:  # noqa: BLE001 — 主通道故障必须降级而非放行
            logger.error(
                "内容安全主通道不可用（%s），已降级到本地词表 direction=%s",
                e,
                direction,
            )
    else:
        logger.debug("内容安全主通道未配置，仅使用本地兜底词表")

    return local_result


def moderate_input(text: str) -> ModerationResult:
    return moderate(text, DIRECTION_INPUT)


def moderate_output(text: str) -> ModerationResult:
    return moderate(text, DIRECTION_OUTPUT)


def moderate_output_local(text: str) -> ModerationResult:
    """仅走本地词表，供流式热路径逐块调用。

    流式下发前要对累计全文复查，若每次都打主通道，就是每个分块一次网络
    请求——延迟与成本都不可接受。所以热路径只用本地表（够挡住已知词），
    完整的主通道检查放在流结束后做一次。
    """
    return _local.check(text, DIRECTION_OUTPUT)
