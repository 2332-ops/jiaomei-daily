"""wxpush —— 把文本消息推送到微信的通用模块。

四条可用路径（详见 README.md）：
    wecom_bot    企业微信群机器人 Webhook  → 推到**群聊**
    wecom_app    企业微信自建应用消息      → 推到**指定微信账号（成员）**  ← 唯一官方"发给人"的方式
    wechat_mp    微信公众号模板消息         → 推到**指定微信用户（openid）**
    pushplus     推送加                     → 推到**个人微信**（借道公众号，最省事）
    serverchan   Server 酱 Turbo            → 推到**个人微信**（同上）

最快上手：
    from wxpush import send_text
    send_text("标题", "正文内容")

需要精细控制：
    from wxpush import Pusher
    with Pusher.from_config_file("config.yaml") as p:
        report = p.send("标题", text="正文", mode="failover")
        if not report.any_ok:
            ...  # 所有通道都失败了，走你自己的告警兜底
"""

from __future__ import annotations

from .channels import list_channels
from .channels.base import Channel, SendResult
from .errors import (ConfigError, DryRunStop, PermanentError, PushError,
                     RateLimitError, RetryableError, TokenExpiredError,
                     is_retryable)
from .logging_setup import get_logger, setup_logging
from .pusher import PushReport, Pusher, send_text
from .retry import RetryPolicy, call_with_retry
from .settings import default_config, env_hint, expand_env, load_config, missing_env
from .text_utils import split_by_bytes, truncate_bytes, utf8_len

__version__ = "1.0.0"

__all__ = [
    # 入口
    "Pusher", "send_text", "PushReport",
    # 数据结构
    "Channel", "SendResult", "RetryPolicy",
    # 异常
    "PushError", "ConfigError", "PermanentError", "RetryableError",
    "RateLimitError", "TokenExpiredError", "DryRunStop",
    # 工具
    "load_config", "default_config", "expand_env", "list_channels",
    "missing_env", "env_hint",
    "setup_logging", "get_logger", "call_with_retry",
    "split_by_bytes", "truncate_bytes", "utf8_len",
]
