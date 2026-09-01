"""可观测性：结构化日志上下文、指标、链路。

改造前只有一个 request_id 打进文本日志：出事之后既不知道是谁触发的，
也不知道慢在哪一段，更没有「错误率涨了」这种能报警的信号。

三件事：
1. **上下文**：用 contextvars 携带 trace_id / request_id / user_id / slug，
   日志过滤器自动注入，业务代码不必层层传参。
2. **指标**：Prometheus。埋点集中在这里，避免散落各处。
3. **链路**：OpenTelemetry。未配置 OTLP 端点时不导出，只在本地生成
   trace_id 供日志关联——不强制运维先搭一套采集器才能用。
"""

import logging
import time
import uuid
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any, Iterator, Optional

logger = logging.getLogger("ex-memory")

_trace_id: ContextVar[str] = ContextVar("trace_id", default="")
_request_id: ContextVar[str] = ContextVar("request_id", default="")
_user_id: ContextVar[str] = ContextVar("user_id", default="")
_slug: ContextVar[str] = ContextVar("slug", default="")


def new_trace_id() -> str:
    return uuid.uuid4().hex


def set_context(
    trace_id: Optional[str] = None,
    request_id: Optional[str] = None,
    user_id: Optional[Any] = None,
    slug: Optional[str] = None,
) -> None:
    if trace_id is not None:
        _trace_id.set(trace_id)
    if request_id is not None:
        _request_id.set(request_id)
    if user_id is not None:
        _user_id.set(str(user_id))
    if slug is not None:
        _slug.set(slug)


def current_context() -> dict:
    return {
        "trace_id": _trace_id.get(),
        "request_id": _request_id.get(),
        "user_id": _user_id.get(),
        "slug": _slug.get(),
    }


class ContextFilter(logging.Filter):
    """把上下文注入每条日志。业务代码不必层层传 trace_id。"""

    def filter(self, record: logging.LogRecord) -> bool:
        for key, value in current_context().items():
            setattr(record, key, value)
        return True


# ── 指标 ──

_metrics: dict[str, Any] = {}


def _registry():
    from prometheus_client import REGISTRY

    return REGISTRY


def init_metrics() -> None:
    """注册指标。重复调用安全——测试里会多次建 app。"""
    if _metrics:
        return
    from prometheus_client import Counter, Gauge, Histogram

    _metrics["http_requests"] = Counter(
        "exmemory_http_requests_total",
        "HTTP 请求数",
        ["method", "path", "status"],
    )
    _metrics["http_latency"] = Histogram(
        "exmemory_http_request_seconds",
        "HTTP 请求耗时",
        ["method", "path"],
        buckets=(0.05, 0.1, 0.25, 0.5, 1, 2, 5, 10, 30),
    )
    _metrics["llm_calls"] = Counter(
        "exmemory_llm_calls_total",
        "LLM 调用数",
        ["provider", "outcome"],
    )
    _metrics["llm_latency"] = Histogram(
        "exmemory_llm_seconds",
        "LLM 调用耗时",
        ["provider"],
        buckets=(0.5, 1, 2, 5, 10, 20, 60),
    )
    _metrics["llm_tokens"] = Counter(
        "exmemory_llm_tokens_total",
        "LLM token 消耗",
        ["provider", "kind"],
    )
    _metrics["rag_hits"] = Histogram(
        "exmemory_rag_injected_chunks",
        "单次对话注入的 RAG 切片数",
        buckets=(0, 1, 2, 3, 5, 10),
    )
    _metrics["safety_events"] = Counter(
        "exmemory_safety_events_total",
        "安全事件数",
        ["type", "action"],
    )
    _metrics["tasks"] = Counter(
        "exmemory_tasks_total",
        "异步任务数",
        ["type", "status"],
    )
    _metrics["task_queue_depth"] = Gauge(
        "exmemory_task_queue_depth",
        "排队中的任务数",
    )
    _metrics["kv_degraded"] = Gauge(
        "exmemory_kv_degraded",
        "共享状态后端是否降级（1=降级）",
    )


def metric(name: str):
    """取指标。未初始化时返回 None，调用方不必到处判空。"""
    return _metrics.get(name)


def observe_http(method: str, path: str, status: int, seconds: float) -> None:
    counter = metric("http_requests")
    if counter is not None:
        counter.labels(method=method, path=path, status=str(status)).inc()
    hist = metric("http_latency")
    if hist is not None:
        hist.labels(method=method, path=path).observe(seconds)


def observe_llm(
    provider: str,
    outcome: str,
    seconds: float,
    prompt_tokens: int = 0,
    completion_tokens: int = 0,
) -> None:
    calls = metric("llm_calls")
    if calls is not None:
        calls.labels(provider=provider, outcome=outcome).inc()
    hist = metric("llm_latency")
    if hist is not None:
        hist.labels(provider=provider).observe(seconds)
    tokens = metric("llm_tokens")
    if tokens is not None and (prompt_tokens or completion_tokens):
        tokens.labels(provider=provider, kind="prompt").inc(prompt_tokens)
        tokens.labels(provider=provider, kind="completion").inc(completion_tokens)


def observe_safety_event(event_type: str, action: str) -> None:
    counter = metric("safety_events")
    if counter is not None:
        counter.labels(type=event_type, action=action).inc()


def observe_task(task_type: str, status: str) -> None:
    counter = metric("tasks")
    if counter is not None:
        counter.labels(type=task_type, status=status).inc()


# ── 链路 ──

_tracer: Any = None


def init_tracing(service_name: str = "ex-memory", otlp_endpoint: str = "") -> Any:
    """初始化 tracer。

    未配置 OTLP 端点时不导出，只在本地生成 span 与 trace_id 供日志关联——
    不该强制运维先搭一套采集器才能用上结构化日志里的 trace_id。
    """
    global _tracer
    if _tracer is not None:
        return _tracer
    try:
        from opentelemetry import trace
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider

        provider = TracerProvider(
            resource=Resource.create({"service.name": service_name})
        )
        if otlp_endpoint:
            from opentelemetry.exporter.otlp.proto.http.trace_exporter import (
                OTLPSpanExporter,
            )
            from opentelemetry.sdk.trace.export import BatchSpanProcessor

            provider.add_span_processor(
                BatchSpanProcessor(OTLPSpanExporter(endpoint=otlp_endpoint))
            )
            logger.info("链路导出已启用: %s", otlp_endpoint)
        else:
            logger.info("未配置 OTLP 端点，链路只在本地生成不导出")
        trace.set_tracer_provider(provider)
        _tracer = trace.get_tracer(service_name)
    except Exception as e:  # noqa: BLE001 — 观测组件不该拖垮服务
        logger.error("链路初始化失败（%s），继续以无链路模式运行", e)
        _tracer = False
    return _tracer


@contextmanager
def span(name: str, **attributes: Any) -> Iterator[None]:
    """开一个 span。tracer 不可用时退化为纯计时，不影响业务。"""
    started = time.perf_counter()
    tracer = _tracer
    if not tracer:
        yield
        return
    with tracer.start_as_current_span(name) as current:
        for key, value in attributes.items():
            if value is not None:
                current.set_attribute(key, value)
        try:
            yield
        finally:
            current.set_attribute(
                "duration_ms", int((time.perf_counter() - started) * 1000)
            )


def reset_for_tests() -> None:
    global _tracer
    _tracer = None
