"""结构化日志系统。"""

import logging
import sys
from typing import Optional
from pathlib import Path
from logging.handlers import RotatingFileHandler

_logger = None


def setup_logging(log_dir: Optional[Path] = None, level: str = "INFO") -> logging.Logger:
    global _logger
    if _logger is not None:
        return _logger

    logger = logging.getLogger("ex-memory")
    logger.setLevel(getattr(logging, level.upper(), logging.INFO))

    fmt = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s:%(lineno)d: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

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
