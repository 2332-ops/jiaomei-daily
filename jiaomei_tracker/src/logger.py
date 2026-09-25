"""日志：同时输出到控制台与按大小轮转的文件。

计划任务场景下没有可见控制台，文件日志是唯一的排查依据，所以这里必须落文件。
"""

from __future__ import annotations

import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

_LOGGER_NAME = "jiaomei"

_FORMAT = "%(asctime)s [%(levelname)s] %(message)s"
_DATEFMT = "%Y-%m-%d %H:%M:%S"


def setup_logger(log_dir: str | Path, level: str = "INFO",
                 max_bytes: int = 2 * 1024 * 1024,
                 backup_count: int = 5) -> logging.Logger:
    """初始化并返回全局 logger（重复调用不会叠加 handler）。"""
    logger = logging.getLogger(_LOGGER_NAME)
    logger.setLevel(getattr(logging, str(level).upper(), logging.INFO))
    logger.propagate = False
    if logger.handlers:
        return logger

    fmt = logging.Formatter(_FORMAT, datefmt=_DATEFMT)

    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(fmt)
    logger.addHandler(console)

    try:
        d = Path(log_dir)
        d.mkdir(parents=True, exist_ok=True)
        fh = RotatingFileHandler(
            d / "tracker.log",
            maxBytes=max_bytes,
            backupCount=backup_count,
            encoding="utf-8",
        )
        fh.setFormatter(fmt)
        logger.addHandler(fh)
    except Exception as exc:  # 日志文件建不起来也不能让程序挂掉
        logger.warning("文件日志初始化失败，仅输出到控制台: %s", exc)

    return logger


def get_logger() -> logging.Logger:
    return logging.getLogger(_LOGGER_NAME)
