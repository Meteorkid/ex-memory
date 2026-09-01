"""结构化日志系统。"""

import json
import logging
import os
import sys
from typing import Optional
from pathlib import Path
from logging.handlers import RotatingFileHandler

_logger = None


class _JsonFormatter(logging.Formatter):
    """结构化输出。日志系统按字段检索，文本行做不到。"""

    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S"),
            "level": record.levelname,
            "logger": record.name,
            "line": f"{record.module}:{record.lineno}",
            "message": record.getMessage(),
        }
        for key in ("trace_id", "request_id", "user_id", "slug"):
            value = getattr(record, key, "")
            if value:
                payload[key] = value
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False)


def setup_logging(
    log_dir: Optional[Path] = None, level: str = "INFO"
) -> logging.Logger:
    global _logger
    if _logger is not None:
        return _logger

    logger = logging.getLogger("ex-memory")
    logger.setLevel(getattr(logging, level.upper(), logging.INFO))

    if os.getenv("LOG_FORMAT", "text").lower() == "json":
        fmt: logging.Formatter = _JsonFormatter()
    else:
        fmt = logging.Formatter(
            "%(asctime)s [%(levelname)s] %(name)s:%(lineno)d "
            "trace=%(trace_id)s user=%(user_id)s: %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )

    # 上下文过滤器：trace_id / user_id 等由 contextvars 自动注入，
    # 业务代码不必层层传参
    from core.observability import ContextFilter

    logger.addFilter(ContextFilter())

    # 控制台 handler
    console = logging.StreamHandler(sys.stderr)
    console.setLevel(logging.WARNING)
    console.setFormatter(fmt)
    logger.addHandler(console)

    # 文件 handler
    if log_dir:
        log_dir = Path(log_dir)
        log_dir.mkdir(parents=True, exist_ok=True)
        file_handler = RotatingFileHandler(
            log_dir / "app.log",
            maxBytes=10 * 1024 * 1024,
            backupCount=5,
            encoding="utf-8",
        )
        file_handler.setLevel(logging.DEBUG)
        file_handler.setFormatter(fmt)
        logger.addHandler(file_handler)

    _logger = logger
    return logger


def get_logger() -> logging.Logger:
    return _logger or logging.getLogger("ex-memory")


def get_audit_logger(log_dir: Optional[Path] = None) -> logging.Logger:
    """审计日志：记录登录、注册、删除等关键操作，独立于应用日志。"""
    audit = logging.getLogger("ex-memory.audit")
    if audit.handlers:
        return audit

    audit.setLevel(logging.INFO)
    audit.propagate = False

    fmt = logging.Formatter(
        '{"timestamp": "%(asctime)s", "level": "%(levelname)s", "event": %(message)s}',
        datefmt="%Y-%m-%dT%H:%M:%S",
    )

    target_dir = Path(log_dir) if log_dir else Path("data")
    target_dir.mkdir(parents=True, exist_ok=True)
    fh = logging.handlers.RotatingFileHandler(
        target_dir / "audit.log",
        maxBytes=10 * 1024 * 1024,
        backupCount=5,
        encoding="utf-8",
    )
    fh.setFormatter(fmt)
    audit.addHandler(fh)
    return audit
