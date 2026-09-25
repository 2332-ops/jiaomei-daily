"""配置加载：默认值 + config.yaml 深合并 + ${ENV} 环境变量展开。"""

from __future__ import annotations

import os
import re
from copy import deepcopy
from pathlib import Path

import yaml

BASE_DIR = Path(__file__).resolve().parent.parent

_ENV_PATTERN = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")

DEFAULTS: dict = {
    "target": {"code": "000983", "name": "山西焦煤", "expect_name": ""},
    "schedule": {
        "slots": [
            {"id": "market_open", "name": "开盘", "time": "09:35", "intraday": True},
            {"id": "morning_ten", "name": "上午十点", "time": "10:00", "intraday": True},
            {"id": "market_close", "name": "下午三点", "time": "15:02", "intraday": False},
        ],
        "holidays": [],
    },
    "retry": {
        "fetch_attempts": 4,
        "fetch_backoff_base": 1.2,
        "fetch_backoff_cap": 12.0,
        "send_attempts": 3,
        "send_backoff_base": 2.0,
        "send_backoff_cap": 20.0,
        "timeout_connect": 6,
        "timeout_read": 15,
    },
    "report": {
        "include_fund_flow": True,
        "include_ma": True,
        "include_five_day": True,
        "five_day_count": 5,
        "include_disclaimer": True,
        "degraded_fund_proxy": True,
        "title_prefix": "山西焦煤 000983",
    },
    "sources": {
        "quote_order": ["tencent", "sina"],
        "kline_order": ["tencent", "sina"],
        "fundflow_order": ["eastmoney_day", "eastmoney_minute"],
        "eastmoney_hosts": [
            "push2his.eastmoney.com",
            "push2.eastmoney.com",
            "push2delay.eastmoney.com",
        ],
        "fundflow_backend": "auto",
    },
    "channels": [{"type": "console", "enabled": True}],
    "storage": {"db_path": "data/tracker.db", "keep_days": 400},
    "logging": {"level": "INFO", "dir": "logs", "max_bytes": 2097152, "backup_count": 5},
    "alert_on_failure": True,
    "dedup_by_slot": True,
}


def _expand_env(node):
    """递归把字符串里的 ${VAR} 替换为环境变量值；未定义则替换为空串。"""
    if isinstance(node, dict):
        return {k: _expand_env(v) for k, v in node.items()}
    if isinstance(node, list):
        return [_expand_env(v) for v in node]
    if isinstance(node, str):
        return _ENV_PATTERN.sub(lambda m: os.environ.get(m.group(1), ""), node)
    return node


def _deep_merge(base: dict, override: dict) -> dict:
    """override 覆盖 base；dict 递归合并，list 整体替换。"""
    out = deepcopy(base)
    for key, val in (override or {}).items():
        if key in out and isinstance(out[key], dict) and isinstance(val, dict):
            out[key] = _deep_merge(out[key], val)
        else:
            out[key] = deepcopy(val)
    return out


def load_config(path: str | Path | None = None) -> dict:
    cfg_path = Path(path) if path else BASE_DIR / "config.yaml"
    if not cfg_path.exists():
        cfg_path = BASE_DIR / "config.example.yaml"
    if not cfg_path.exists():
        raise FileNotFoundError(f"找不到配置文件: {cfg_path}")

    with open(cfg_path, "r", encoding="utf-8") as fh:
        raw = yaml.safe_load(fh) or {}

    cfg = _deep_merge(DEFAULTS, raw)
    cfg = _expand_env(cfg)
    cfg["_base_dir"] = str(BASE_DIR)
    cfg["_config_path"] = str(cfg_path)
    return cfg


def abs_path(cfg: dict, rel: str) -> Path:
    p = Path(rel)
    return p if p.is_absolute() else Path(cfg["_base_dir"]) / p


def get_slot(cfg: dict, slot_id: str) -> dict | None:
    for slot in cfg.get("schedule", {}).get("slots", []) or []:
        if str(slot.get("id")) == str(slot_id):
            return slot
    return None


def enabled_channels(cfg: dict) -> list[dict]:
    out = []
    for ch in cfg.get("channels", []) or []:
        if not ch or not ch.get("enabled"):
            continue
        ch_type = str(ch.get("type", "")).lower()
        if ch_type in ("pushplus",) and not ch.get("token"):
            continue
        if ch_type in ("serverchan",) and not ch.get("sendkey"):
            continue
        if ch_type in ("wecom", "feishu") and not ch.get("webhook"):
            continue
        if ch_type in ("dingtalk",) and not ch.get("access_token"):
            continue
        if ch_type in ("email",) and not (ch.get("user") and ch.get("password") and ch.get("mail_to")):
            continue
        if ch_type in ("bark",) and not ch.get("key"):
            continue
        out.append(ch)
    return out


def parse_hhmm(text: str) -> tuple[int, int]:
    parts = str(text).strip().split(":")
    if len(parts) != 2:
        raise ValueError(f"时间格式应为 HH:MM，收到: {text!r}")
    hour, minute = int(parts[0]), int(parts[1])
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise ValueError(f"时间超出范围: {text!r}")
    return hour, minute
