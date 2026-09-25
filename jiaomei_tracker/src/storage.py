"""本地存档（SQLite）。

三张表各司其职：
  quotes      每个交易日一行的行情快照
  fundflow    每个交易日一行的资金流
  send_log    发送记录 —— 同时是"同一时段不重复发送"的幂等依据
  run_log     每次运行的执行记录，用于事后排查"某天为什么没收到消息"

并发说明：日终脚本与常驻进程可能同时写，已开启 WAL 并把事务窗口压到最小。
"""

from __future__ import annotations

import sqlite3
from datetime import datetime
from pathlib import Path

from .logger import get_logger

_SCHEMA = """
CREATE TABLE IF NOT EXISTS quotes (
    trade_date   TEXT NOT NULL,
    code         TEXT NOT NULL,
    name         TEXT,
    price        REAL,
    prev_close   REAL,
    open         REAL,
    high         REAL,
    low          REAL,
    change_pct   REAL,
    volume_hand  REAL,
    amount_yuan  REAL,
    turnover_pct REAL,
    volume_ratio REAL,
    source       TEXT,
    quote_time   TEXT,
    fetched_at   TEXT,
    PRIMARY KEY (trade_date, code)
);

CREATE TABLE IF NOT EXISTS fundflow (
    trade_date  TEXT NOT NULL,
    code        TEXT NOT NULL,
    main        REAL,
    small       REAL,
    mid         REAL,
    big         REAL,
    xbig        REAL,
    main_pct    REAL,
    source      TEXT,
    fetched_at  TEXT,
    PRIMARY KEY (trade_date, code)
);

CREATE TABLE IF NOT EXISTS send_log (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    trade_date TEXT NOT NULL,
    slot_id    TEXT NOT NULL,
    channel    TEXT,
    status     TEXT,
    detail     TEXT,
    created_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_send_slot ON send_log(trade_date, slot_id, status);

CREATE TABLE IF NOT EXISTS run_log (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    trade_date TEXT,
    slot_id    TEXT,
    status     TEXT,
    message    TEXT,
    created_at TEXT
);
"""


class Storage:
    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path), timeout=15)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=15000")
        return conn

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.executescript(_SCHEMA)

    # ------------------------------------------------------------ 写入

    def save_quote(self, trade_date: str, code: str, quote: dict) -> None:
        try:
            with self._connect() as conn:
                conn.execute(
                    """INSERT INTO quotes (trade_date, code, name, price, prev_close, open, high, low,
                                           change_pct, volume_hand, amount_yuan, turnover_pct,
                                           volume_ratio, source, quote_time, fetched_at)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                       ON CONFLICT(trade_date, code) DO UPDATE SET
                         price=excluded.price, prev_close=excluded.prev_close, open=excluded.open,
                         high=excluded.high, low=excluded.low, change_pct=excluded.change_pct,
                         volume_hand=excluded.volume_hand, amount_yuan=excluded.amount_yuan,
                         turnover_pct=excluded.turnover_pct, volume_ratio=excluded.volume_ratio,
                         source=excluded.source, quote_time=excluded.quote_time,
                         fetched_at=excluded.fetched_at""",
                    (trade_date, code, quote.get("name"), quote.get("price"), quote.get("prev_close"),
                     quote.get("open"), quote.get("high"), quote.get("low"), quote.get("change_pct"),
                     quote.get("volume_hand"), quote.get("amount_yuan"), quote.get("turnover_pct"),
                     quote.get("volume_ratio"), quote.get("source"), quote.get("quote_time"),
                     datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
                )
        except Exception as exc:  # noqa: BLE001 - 存档失败不影响推送
            get_logger().warning("行情落库失败: %s", exc)

    def save_fundflow(self, trade_date: str, code: str, flow: dict) -> None:
        if not flow:
            return
        try:
            with self._connect() as conn:
                conn.execute(
                    """INSERT INTO fundflow (trade_date, code, main, small, mid, big, xbig,
                                             main_pct, source, fetched_at)
                       VALUES (?,?,?,?,?,?,?,?,?,?)
                       ON CONFLICT(trade_date, code) DO UPDATE SET
                         main=excluded.main, small=excluded.small, mid=excluded.mid,
                         big=excluded.big, xbig=excluded.xbig, main_pct=excluded.main_pct,
                         source=excluded.source, fetched_at=excluded.fetched_at""",
                    (trade_date, code, flow.get("main"), flow.get("small"), flow.get("mid"),
                     flow.get("big"), flow.get("xbig"), flow.get("main_pct"),
                     flow.get("source"), datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
                )
        except Exception as exc:  # noqa: BLE001
            get_logger().warning("资金流落库失败: %s", exc)

    def log_send(self, trade_date: str, slot_id: str, channel: str,
                 status: str, detail: str = "") -> None:
        try:
            with self._connect() as conn:
                conn.execute(
                    "INSERT INTO send_log (trade_date, slot_id, channel, status, detail, created_at)"
                    " VALUES (?,?,?,?,?,?)",
                    (trade_date, slot_id, channel, status, (detail or "")[:500],
                     datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
                )
        except Exception as exc:  # noqa: BLE001
            get_logger().warning("发送日志写入失败: %s", exc)

    def log_run(self, trade_date: str, slot_id: str, status: str, message: str = "") -> None:
        try:
            with self._connect() as conn:
                conn.execute(
                    "INSERT INTO run_log (trade_date, slot_id, status, message, created_at)"
                    " VALUES (?,?,?,?,?)",
                    (trade_date, slot_id, status, (message or "")[:800],
                     datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
                )
        except Exception as exc:  # noqa: BLE001
            get_logger().warning("运行日志写入失败: %s", exc)

    # ------------------------------------------------------------ 查询

    def already_sent(self, trade_date: str, slot_id: str) -> bool:
        """该交易日该时段是否已成功推送过（用于去重，防止计划任务重试造成重复）。"""
        try:
            with self._connect() as conn:
                row = conn.execute(
                    "SELECT COUNT(1) AS n FROM send_log"
                    " WHERE trade_date=? AND slot_id=? AND status='ok'",
                    (trade_date, slot_id),
                ).fetchone()
            return bool(row and row["n"] > 0)
        except Exception:  # noqa: BLE001
            return False

    def recent_fundflow(self, code: str, limit: int = 6) -> list[dict]:
        """取最近 limit 个交易日的资金流（按日期升序）。

        用途：东财日线接口是间歇可用的，与其依赖它，不如用本地存档自己累积
        "近 5 日主力合计"——程序跑满 5 个交易日后该指标即完整，且不依赖外部接口。
        """
        try:
            with self._connect() as conn:
                rows = conn.execute(
                    "SELECT trade_date, main, xbig, big, mid, small, source FROM fundflow"
                    " WHERE code=? ORDER BY trade_date DESC LIMIT ?",
                    (code, int(limit)),
                ).fetchall()
            return [dict(r) for r in reversed(rows)]
        except Exception as exc:  # noqa: BLE001
            get_logger().warning("读取历史资金流失败: %s", exc)
            return []

    def recent_quotes(self, code: str, limit: int = 10) -> list[dict]:
        try:
            with self._connect() as conn:
                rows = conn.execute(
                    "SELECT trade_date, price, change_pct, volume_hand, amount_yuan,"
                    " high, low, open, prev_close FROM quotes"
                    " WHERE code=? ORDER BY trade_date DESC LIMIT ?",
                    (code, int(limit)),
                ).fetchall()
            return [dict(r) for r in reversed(rows)]
        except Exception:  # noqa: BLE001
            return []

    def recent_runs(self, limit: int = 20) -> list[dict]:
        try:
            with self._connect() as conn:
                rows = conn.execute(
                    "SELECT * FROM run_log ORDER BY id DESC LIMIT ?", (limit,)
                ).fetchall()
            return [dict(r) for r in rows]
        except Exception:  # noqa: BLE001
            return []

    def purge(self, keep_days: int) -> int:
        """清理超过保留期的运行/发送日志（行情与资金流数据永久保留）。"""
        if not keep_days or keep_days <= 0:
            return 0
        try:
            with self._connect() as conn:
                cur = conn.execute(
                    "DELETE FROM run_log WHERE created_at < datetime('now','localtime', ?)",
                    (f"-{int(keep_days)} days",),
                )
                n = cur.rowcount or 0
                cur = conn.execute(
                    "DELETE FROM send_log WHERE created_at < datetime('now','localtime', ?)",
                    (f"-{int(keep_days)} days",),
                )
                n += cur.rowcount or 0
            return n
        except Exception as exc:  # noqa: BLE001
            get_logger().warning("日志清理失败: %s", exc)
            return 0
