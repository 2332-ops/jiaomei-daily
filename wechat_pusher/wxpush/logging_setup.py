"""日志配置：让本模块可以独立使用，不依赖调用方的 logging 配置。

这里刻意写得比一般库"多余"一点，因为踩过坑：

  库的常规做法是"只加 NullHandler，配置交给使用者"。但这套代码是给
  计划任务/CI 跑的 —— 如果使用者漏了 setup_logging()，程序会完全静默，
  推送失败了也没人知道。

  所以策略是：**首次取 logger 时自动装一个控制台 handler**（默认 INFO）。
  使用者随后调 setup_logging() 依然能改级别、加文件输出。

  同时 setup_logging 支持"增量生效"而不是"第二次调用就整个忽略"：
  级别每次都应用；控制台 handler 只装一次（避免日志翻倍）；
  文件 handler 换了路径才重装。
"""

from __future__ import annotations

import logging
import os
import sys
from logging.handlers import RotatingFileHandler

LOGGER_NAME = "wxpush"
_FMT = "%(asctime)s %(levelname)-7s [%(name)s] %(message)s"
_DATEFMT = "%Y-%m-%d %H:%M:%S"

_state: dict = {"configured": False, "console": False, "file": None, "quiet": False}


def _norm_level(level: str | int) -> int:
    if isinstance(level, int):
        return level
    return getattr(logging, str(level).upper(), logging.INFO)


def _console_handler() -> logging.Handler:
    h = logging.StreamHandler(sys.stdout)
    h.setFormatter(logging.Formatter(_FMT, _DATEFMT))
    h.set_name("wxpush-console")
    return h


def _file_handler(log_file: str) -> logging.Handler:
    d = os.path.dirname(os.path.abspath(log_file))
    if d:
        os.makedirs(d, exist_ok=True)
    h = RotatingFileHandler(log_file, maxBytes=2 * 1024 * 1024,
                            backupCount=5, encoding="utf-8")
    h.setFormatter(logging.Formatter(_FMT, _DATEFMT))
    h.set_name("wxpush-file")
    return h


def setup_logging(level: str | int = "INFO", *, log_file: str | None = None,
                  quiet: bool = False, force: bool = False) -> logging.Logger:
    """配置 wxpush 的日志。

    level    级别，每次都生效
    log_file 附带一个轮转文件输出（单文件 2MB × 5）
    quiet    不在控制台输出
    force    清掉之前装的 handler 重新来（一般不需要）
    """
    logger = logging.getLogger(LOGGER_NAME)
    logger.propagate = False          # 不往 root 冒泡，避免与宿主程序重复打印
    logger.setLevel(_norm_level(level))

    if force:
        for h in list(logger.handlers):
            logger.removeHandler(h)
        logger.propagate = False
        _state.update(configured=False, console=False, file=None, quiet=False)

    # ---- 控制台 ----
    if quiet:
        if _state["console"]:
            for h in list(logger.handlers):
                if h.get_name() == "wxpush-console":
                    logger.removeHandler(h)
            _state["console"] = False
    elif not _state["console"]:
        logger.addHandler(_console_handler())
        _state["console"] = True

    # ---- 文件 ----
    if log_file:
        target = os.path.abspath(log_file)
        if _state["file"] != target:
            for h in list(logger.handlers):
                if h.get_name() == "wxpush-file":
                    logger.removeHandler(h)
            logger.addHandler(_file_handler(target))
            _state["file"] = target

    if not logger.handlers:
        logger.addHandler(logging.NullHandler())

    _state["quiet"] = bool(quiet)
    _state["configured"] = True
    return logger


def get_logger(suffix: str | None = None) -> logging.Logger:
    # 首次使用时自动装好控制台输出，避免"程序在跑但什么都不打印"
    if not _state["configured"]:
        setup_logging()
    return logging.getLogger(f"{LOGGER_NAME}.{suffix}" if suffix else LOGGER_NAME)
