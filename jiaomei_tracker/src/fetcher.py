"""行情取数：多源容错 + 自动降级 + 指数退避重试。

数据源与字段口径（均已实测核对，2026-09-24 收盘数据）：
  腾讯行情   https://qt.gtimg.cn/q={sym}            GBK，字段以 ~ 分隔
  新浪行情   https://hq.sinajs.cn/list={sym}        GBK，需 Referer
  腾讯日K    https://web.ifzq.gtimg.cn/appstock/app/fqkline/get   前复权
  新浪日K    https://money.finance.sina.com.cn/quotes_service/... 不复权
  东财资金流 daykline（日线，含主力/超大单/大单/中单/小单）
  东财资金流 kline klt=1（分钟级，用于盘中取当日累计）

设计原则：
  1. 任一数据源不可用不得让整轮任务失败，能拿到多少算多少；
  2. 拿不到的字段一律返回 None，由报告层显式写"未取到"，绝不用其他口径冒充。
"""

from __future__ import annotations

import json
import random
import re
import shutil
import subprocess
import time
from datetime import datetime

import requests

from .logger import get_logger

# ---------------------------------------------------------------- 基础工具

_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")


class FetchError(Exception):
    """取数失败（重试耗尽或数据格式不符合预期）。"""


def market_symbol(code: str) -> str:
    """6 位代码 → 带市场前缀的代码，如 000983 → sz000983。"""
    code = str(code).strip().lower()
    if code[:2] in ("sh", "sz", "bj"):
        return code
    digits = re.sub(r"\D", "", code)
    if not digits:
        raise FetchError(f"非法股票代码: {code!r}")
    if digits[0] in ("6", "9"):
        return "sh" + digits
    if digits[0] in ("4", "8"):
        return "bj" + digits
    return "sz" + digits


def em_secid(symbol: str) -> str:
    """东财 secid：沪市 1.xxxxxx，深市/北交所 0.xxxxxx。"""
    symbol = symbol.lower()
    return ("1." if symbol.startswith("sh") else "0.") + symbol[2:]


def _backoff(attempt: int, base: float, cap: float) -> None:
    delay = min(cap, base * (2 ** attempt))
    time.sleep(delay * (0.7 + random.random() * 0.6))


def _brief(value, limit: int = 140) -> str:
    """把冗长的网络异常/URL 压成一行，避免日志被单条错误刷屏。"""
    text = str(value).replace("\n", " ").strip()
    return text if len(text) <= limit else text[:limit] + "…"


def _short_url(url: str, keep: int = 70) -> str:
    return url if len(url) <= keep else url[:keep] + "…"


_CURL_BIN: str | None = None
_CURL_PROBED = False


def _find_curl() -> str | None:
    """查找系统 curl。Windows 10/11 自带 C:\\Windows\\System32\\curl.exe。"""
    global _CURL_BIN, _CURL_PROBED
    if not _CURL_PROBED:
        _CURL_BIN = shutil.which("curl")
        _CURL_PROBED = True
    return _CURL_BIN


def curl_get(url: str, *, referer: str | None = None, timeout: int = 15,
             use_env_proxy: bool = True) -> str:
    """用系统 curl 取数据。

    为什么需要它：某些网络环境下（企业代理、特定 TLS 策略）Python 的
    HTTP 栈会与目标站点握手失败，而系统 curl 能正常访问。实测遇到过
    requests 报 RemoteDisconnected、curl 直连 200 的情况。
    这是资金流取数的最后一道兜底，失败时由调用方决定如何降级。
    """
    curl = _find_curl()
    if not curl:
        raise FetchError("系统未找到 curl，无法使用 curl 兜底")

    cmd = [curl, "-sS", "--max-time", str(int(timeout)), "-H", f"User-Agent: {_UA}"]
    if referer:
        cmd += ["-H", f"Referer: {referer}"]
    if not use_env_proxy:
        cmd += ["--noproxy", "*"]
    cmd.append(url)

    proc = subprocess.run(cmd, capture_output=True, timeout=int(timeout) + 5)
    if proc.returncode != 0:
        raise FetchError(f"curl 退出码 {proc.returncode}: "
                         f"{_brief(proc.stderr.decode('utf-8', 'replace'), 120)}")
    text = proc.stdout.decode("utf-8", "replace").strip()
    if not text:
        raise FetchError("curl 返回空内容")
    return text


def _to_float(value, default=None):
    try:
        if value is None:
            return default
        text = str(value).strip()
        if text in ("", "-", "--", "null", "None"):
            return default
        return float(text)
    except (TypeError, ValueError):
        return default


class HttpClient:
    """带重试与退避的极简 HTTP 客户端。"""

    def __init__(self, retry_cfg: dict):
        self.retry = retry_cfg or {}
        self.timeout = (
            float(self.retry.get("timeout_connect", 6)),
            float(self.retry.get("timeout_read", 15)),
        )
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": _UA, "Accept": "*/*"})

    def get_text(self, url: str, *, referer: str | None = None,
                 encoding: str | None = None, attempts: int | None = None) -> str:
        attempts = int(attempts or self.retry.get("fetch_attempts", 4))
        base = float(self.retry.get("fetch_backoff_base", 1.2))
        cap = float(self.retry.get("fetch_backoff_cap", 12.0))
        logger = get_logger()
        last_err: Exception | None = None

        for i in range(attempts):
            try:
                headers = {"Referer": referer} if referer else None
                resp = self.session.get(url, headers=headers, timeout=self.timeout)
                if resp.status_code != 200:
                    raise FetchError(f"HTTP {resp.status_code}")
                if encoding:
                    return resp.content.decode(encoding, errors="replace")
                return resp.text
            except Exception as exc:  # noqa: BLE001 - 网络异常类型繁多，统一兜住
                last_err = exc
                logger.warning("请求失败 %s/%s %s -> %s",
                               i + 1, attempts, _short_url(url), _brief(exc, 110))
                if i < attempts - 1:
                    _backoff(i, base, cap)

        raise FetchError(f"请求最终失败: {_short_url(url)} ({_brief(last_err, 110)})")


# ---------------------------------------------------------------- 行情快照

# 腾讯行情字段位（已核对 2026-09-24 收盘：6.53 / 昨收 6.49 / 最高 6.58 / 最低 6.44 全部吻合）
_TC_INDEX = {
    "name": 1, "code": 2, "price": 3, "prev_close": 4, "open": 5,
    "volume_hand": 6, "outer_hand": 7, "inner_hand": 8,
    "time": 30, "change": 31, "change_pct": 32, "high": 33, "low": 34,
    "amount_wan": 37, "turnover_pct": 38, "pe_ttm": 39,
    "amplitude_pct": 43, "float_mcap_yi": 44, "total_mcap_yi": 45, "pb": 46,
    "limit_up": 47, "limit_down": 48, "volume_ratio": 49,
    "avg_price": 51, "amount_wan_exact": 57,
    "chg_ytd": 62, "chg_5d": 63, "dividend_ttm": 64,
    "high_52w": 67, "low_52w": 68, "chg_10d": 69, "chg_20d": 70,
    "chg_60d": 71, "chg_180d": 75,
}


def _parse_quote_time(raw: str) -> str:
    """腾讯时间戳 20260924161454 -> 2026-09-24 16:14:54。"""
    raw = str(raw or "").strip()
    if len(raw) >= 14 and raw.isdigit():
        try:
            return datetime.strptime(raw[:14], "%Y%m%d%H%M%S").strftime("%Y-%m-%d %H:%M:%S")
        except ValueError:
            pass
    return raw


def _parse_tencent_quote(text: str) -> dict:
    match = re.search(r'="([^"]*)"', text)
    if not match or not match.group(1).strip():
        raise FetchError("腾讯行情返回内容为空或格式异常")
    fields = match.group(1).split("~")
    if len(fields) < 50:
        raise FetchError(f"腾讯行情字段数异常: {len(fields)}")

    def pick(key, cast=float):
        idx = _TC_INDEX.get(key)
        if idx is None or idx >= len(fields):
            return None
        return cast(fields[idx]) if cast is not float else _to_float(fields[idx])

    quote = {
        "name": fields[_TC_INDEX["name"]].strip(),
        "code": fields[_TC_INDEX["code"]].strip(),
        "price": _to_float(fields[3]),
        "prev_close": _to_float(fields[4]),
        "open": _to_float(fields[5]),
        "volume_hand": _to_float(fields[6]),
        "outer_hand": _to_float(fields[7]),
        "inner_hand": _to_float(fields[8]),
        "quote_time": _parse_quote_time(fields[30]),
        "change": _to_float(fields[31]),
        "change_pct": _to_float(fields[32]),
        "high": _to_float(fields[33]),
        "low": _to_float(fields[34]),
        "amount_wan": _to_float(fields[37]),
        "turnover_pct": _to_float(fields[38]),
        "amplitude_pct": _to_float(fields[43]),
        "float_mcap_yi": _to_float(fields[44]),
        "total_mcap_yi": _to_float(fields[45]),
        "pb": _to_float(fields[46]),
        "limit_up": _to_float(fields[47]),
        "limit_down": _to_float(fields[48]),
        "volume_ratio": _to_float(fields[49]),
        "avg_price": pick("avg_price"),
        "chg_5d": pick("chg_5d"),
        "chg_10d": pick("chg_10d"),
        "chg_20d": pick("chg_20d"),
        "chg_60d": pick("chg_60d"),
        "chg_ytd": pick("chg_ytd"),
        "high_52w": pick("high_52w"),
        "low_52w": pick("low_52w"),
        "source": "tencent",
    }
    # 成交额优先用更精确的第 57 位（单位：万元）
    exact = pick("amount_wan_exact")
    if exact:
        quote["amount_wan"] = exact
    quote["amount_yuan"] = (quote["amount_wan"] * 10000) if quote["amount_wan"] else None
    return _sanity_check(quote)


def _parse_sina_quote(text: str) -> dict:
    match = re.search(r'="([^"]*)"', text)
    if not match or not match.group(1).strip():
        raise FetchError("新浪行情返回内容为空")
    f = match.group(1).split(",")
    if len(f) < 32:
        raise FetchError(f"新浪行情字段数异常: {len(f)}")

    price = _to_float(f[3]) or 0.0
    prev_close = _to_float(f[2]) or 0.0
    # 收盘后新浪的"现价"可能为 0，用昨收兜底，避免算出 -100% 涨跌幅
    if price <= 0:
        price = prev_close
    change = round(price - prev_close, 4) if prev_close else None
    change_pct = round(change / prev_close * 100, 4) if (prev_close and change is not None) else None
    high = _to_float(f[4])
    low = _to_float(f[5])
    amplitude = round((high - low) / prev_close * 100, 4) if (prev_close and high and low) else None

    quote = {
        "name": f[0].strip(),
        "code": "",
        "price": price,
        "prev_close": prev_close,
        "open": _to_float(f[1]),
        "high": high,
        "low": low,
        "volume_hand": (_to_float(f[8]) or 0.0) / 100.0,   # 股 → 手
        "amount_yuan": _to_float(f[9]),
        "amount_wan": (_to_float(f[9]) or 0.0) / 10000.0,
        "change": change,
        "change_pct": change_pct,
        "amplitude_pct": amplitude,
        "turnover_pct": None,
        "volume_ratio": None,
        "limit_up": None,
        "limit_down": None,
        "outer_hand": None,
        "inner_hand": None,
        "quote_time": f"{f[30].strip()} {f[31].strip()}".strip(),
        "source": "sina",
    }
    return _sanity_check(quote)


def _sanity_check(quote: dict) -> dict:
    """基本合理性校验：价格必须为正，否则视为脏数据。"""
    if not quote.get("price") or quote["price"] <= 0:
        raise FetchError("行情价格无效（<=0），疑似脏数据或停牌无数据")
    return quote


def fetch_quote(client: HttpClient, cfg: dict) -> dict:
    """按配置顺序尝试各行情源，返回统一结构的快照。"""
    logger = get_logger()
    symbol = market_symbol(cfg["target"]["code"])
    order = cfg.get("sources", {}).get("quote_order", ["tencent", "sina"])
    errors = []

    for source in order:
        try:
            if source == "tencent":
                text = client.get_text(f"https://qt.gtimg.cn/q={symbol}", encoding="gbk")
                quote = _parse_tencent_quote(text)
            elif source == "sina":
                text = client.get_text(
                    f"https://hq.sinajs.cn/list={symbol}",
                    referer="https://finance.sina.com.cn", encoding="gbk",
                )
                quote = _parse_sina_quote(text)
            else:
                continue

            quote["symbol"] = symbol
            expect = (cfg["target"].get("expect_name") or "").strip()
            got = (quote.get("name") or "").strip()
            if expect and got and expect not in got:
                logger.warning("标的名称校验未通过：期望 %s，实际 %s，跳过该源", expect, got)
                errors.append(f"{source}: 名称校验未通过({got})")
                continue

            logger.info("行情取数成功，来源=%s 现价=%s 涨跌幅=%s 时间=%s",
                        source, quote["price"], quote["change_pct"], quote["quote_time"])
            return quote
        except Exception as exc:  # noqa: BLE001
            logger.warning("行情源 %s 不可用: %s", source, _brief(exc, 110))
            errors.append(f"{source}: {_brief(exc, 110)}")

    raise FetchError("全部行情源均失败 -> " + " | ".join(errors))


# ---------------------------------------------------------------- 日 K 线

def _parse_tencent_kline(text: str, symbol: str) -> list[dict]:
    payload = json.loads(text)
    node = (payload.get("data") or {}).get(symbol) or {}
    rows = node.get("qfqday") or node.get("day")
    if not rows:
        raise FetchError("腾讯日K返回为空")
    out = []
    for row in rows:
        if len(row) < 6:
            continue
        out.append({
            "date": str(row[0]),
            "open": _to_float(row[1]),
            "close": _to_float(row[2]),
            "high": _to_float(row[3]),
            "low": _to_float(row[4]),
            "volume_hand": _to_float(row[5]),
        })
    if not out:
        raise FetchError("腾讯日K解析后为空")
    return out


def _parse_sina_kline(text: str) -> list[dict]:
    rows = json.loads(text)
    if not isinstance(rows, list) or not rows:
        raise FetchError("新浪日K返回为空")
    out = []
    for row in rows:
        out.append({
            "date": str(row.get("day")),
            "open": _to_float(row.get("open")),
            "close": _to_float(row.get("close")),
            "high": _to_float(row.get("high")),
            "low": _to_float(row.get("low")),
            "volume_hand": (_to_float(row.get("volume")) or 0.0) / 100.0,  # 股 → 手
        })
    if not out:
        raise FetchError("新浪日K解析后为空")
    return out


def fetch_kline(client: HttpClient, cfg: dict, count: int = 30) -> list[dict]:
    """取最近 count 个交易日的日 K（按日期升序）。"""
    logger = get_logger()
    symbol = market_symbol(cfg["target"]["code"])
    order = cfg.get("sources", {}).get("kline_order", ["tencent", "sina"])
    errors = []

    for source in order:
        try:
            if source == "tencent":
                url = ("https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"
                       f"?param={symbol},day,,,{count},qfq")
                rows = _parse_tencent_kline(client.get_text(url), symbol)
            elif source == "sina":
                url = ("https://money.finance.sina.com.cn/quotes_service/api/json_v2.php/"
                       f"CN_MarketData.getKLineData?symbol={symbol}&scale=240&ma=no&datalen={count}")
                rows = _parse_sina_kline(client.get_text(url, referer="https://finance.sina.com.cn"))
            else:
                continue

            rows.sort(key=lambda r: r["date"])
            logger.info("日K取数成功，来源=%s 条数=%s 区间=%s~%s",
                        source, len(rows), rows[0]["date"], rows[-1]["date"])
            return rows
        except Exception as exc:  # noqa: BLE001
            logger.warning("日K源 %s 不可用: %s", source, _brief(exc, 110))
            errors.append(f"{source}: {_brief(exc, 110)}")

    logger.error("全部日K源均失败: %s", " | ".join(errors))
    return []


# ---------------------------------------------------------------- 资金流

# 东财日线资金流字段顺序（已用 2026-09-24 数据交叉验证：
# 主力 13474965 = 大单 -6454264 + 超大单 19929229；主力占比 4.19% × 成交额 3.218亿 ≈ 1348万）
_FF_DAY_FIELDS = ["date", "main", "small", "mid", "big", "xbig",
                  "main_pct", "small_pct", "mid_pct", "big_pct", "xbig_pct",
                  "close", "change_pct"]
# 分钟级资金流字段顺序
_FF_MIN_FIELDS = ["time", "main", "small", "mid", "big", "xbig"]


def _parse_ff_rows(klines: list[str], fields: list[str]) -> list[dict]:
    out = []
    for line in klines or []:
        parts = str(line).split(",")
        if len(parts) < len(fields):
            continue
        row = {}
        for key, raw in zip(fields, parts):
            row[key] = raw.strip() if key in ("date", "time") else _to_float(raw)
        out.append(row)
    return out


def fetch_fundflow(client: HttpClient, cfg: dict) -> dict | None:
    """取资金流：优先日线（含全部分档），失败降级到分钟级（当日累计）。

    返回 {date, main, small, mid, big, xbig, main_pct, trade_date, history:[...], source}
    全部源失败返回 None —— 由报告层写明"未取到"，绝不用别的指标冒充主力资金。
    """
    logger = get_logger()
    symbol = market_symbol(cfg["target"]["code"])
    secid = em_secid(symbol)
    hosts = cfg.get("sources", {}).get("eastmoney_hosts") or ["push2his.eastmoney.com"]
    order = cfg.get("sources", {}).get("fundflow_order", ["eastmoney_day", "eastmoney_minute"])
    fields2 = "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61,f62,f63"
    fields1 = "f1,f2,f3,f7"
    errors: list[str] = []
    backend = str(cfg.get("sources", {}).get("fundflow_backend", "auto")).lower()
    referer = f"https://quote.eastmoney.com/{symbol}.html"
    timeout = int(client.timeout[1])

    def load_rows(kind: str, host: str):
        """按 backend 配置依次尝试 requests / curl（带代理）/ curl（直连）。"""
        if kind == "eastmoney_day":
            url = (f"https://{host}/api/qt/stock/fflow/daykline/get"
                   f"?lmt=30&klt=101&secid={secid}&fields1={fields1}&fields2={fields2}")
            fields = _FF_DAY_FIELDS
        elif kind == "eastmoney_minute":
            url = (f"https://{host}/api/qt/stock/fflow/kline/get"
                   f"?lmt=0&klt=1&secid={secid}&fields1={fields1}&fields2=f51,f52,f53,f54,f55,f56")
            fields = _FF_MIN_FIELDS
        else:
            return [], None

        attempts: list[tuple[str, object]] = []
        if backend in ("auto", "requests"):
            # 这里只试 1 次：外层已经对 3 个域名各试一轮，重复重试只是浪费时间
            attempts.append(("requests",
                             lambda: client.get_text(url, referer=referer, attempts=1)))
        if backend in ("auto", "curl") and _find_curl():
            attempts.append(("curl", lambda: curl_get(url, referer=referer,
                                                      timeout=timeout, use_env_proxy=True)))
            attempts.append(("curl-direct", lambda: curl_get(url, referer=referer,
                                                             timeout=timeout, use_env_proxy=False)))

        for name, loader in attempts:
            try:
                payload = json.loads(loader())
                klines = ((payload.get("data") or {}).get("klines")) or []
                rows = _parse_ff_rows(klines, fields)
                if rows:
                    return rows, f"{kind}@{host}[{name}]"
                errors.append(f"{kind}@{host}[{name}]: 返回空数据")
            except Exception as exc:  # noqa: BLE001
                errors.append(f"{kind}@{host}[{name}]: {_brief(exc, 100)}")
        return [], None

    for kind in order:
        for host in hosts:
            rows, tag = load_rows(kind, host)
            if not rows or not tag:
                continue

            latest = rows[-1]
            trade_date = str(latest.get("date") or latest.get("time") or "")[:10]
            result = {
                "source": tag,
                "trade_date": trade_date,
                "main": latest.get("main"),
                "small": latest.get("small"),
                "mid": latest.get("mid"),
                "big": latest.get("big"),
                "xbig": latest.get("xbig"),
                "main_pct": latest.get("main_pct"),
                "history": rows if kind == "eastmoney_day" else [],
            }
            logger.info("资金流取数成功，来源=%s 日期=%s 主力净流入=%s",
                        tag, trade_date, f"{result['main']:.0f}" if result["main"] else "—")
            return result

    logger.warning("全部资金流源均失败（报告将标注未取到）: %s", " | ".join(errors[:8]))
    return None


def fetch_all(cfg: dict) -> dict:
    """一次性取齐本轮报告所需数据，返回 {quote, kline, fundflow, warnings}。"""
    logger = get_logger()
    client = HttpClient(cfg.get("retry", {}))
    bundle: dict = {"quote": None, "kline": [], "fundflow": None, "warnings": []}

    try:
        bundle["quote"] = fetch_quote(client, cfg)
    except Exception as exc:  # noqa: BLE001
        bundle["warnings"].append(f"行情获取失败：{exc}")
        logger.error("行情获取失败: %s", exc)

    need_kline = int(cfg.get("report", {}).get("five_day_count", 5)) + 20
    bundle["kline"] = fetch_kline(client, cfg, count=max(need_kline, 30))
    if not bundle["kline"]:
        bundle["warnings"].append("日K走势数据获取失败，无法生成近 5 日走势")

    if cfg.get("report", {}).get("include_fund_flow", True):
        bundle["fundflow"] = fetch_fundflow(client, cfg)
        if bundle["fundflow"] is None:
            bundle["warnings"].append("大资金数据获取失败（数据源不可用，未使用其他口径替代）")

    return bundle
