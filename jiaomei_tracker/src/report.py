"""报告生成：把取到的原始数据渲染成适合手机屏幕阅读的短消息。

输出三种形态：
  title    推送标题（含关键数字，锁屏即可看到涨跌）
  text     纯文本正文（邮件 / Server酱 / 短信式阅读）
  markdown 带轻量标记的正文（企微 / 钉钉 / PushPlus 支持 markdown 时更好看）
"""

from __future__ import annotations

from datetime import datetime

UP = "▲"
DOWN = "▼"
FLAT = "＝"

DISCLAIMER = "仅供参考，不构成投资建议"


# ---------------------------------------------------------------- 格式化

def fmt_price(value, digits: int = 2) -> str:
    return "—" if value is None else f"{value:.{digits}f}"


def fmt_pct(value, digits: int = 2, signed: bool = True) -> str:
    if value is None:
        return "—"
    return f"{value:+.{digits}f}%" if signed else f"{value:.{digits}f}%"


def fmt_arrow(value) -> str:
    if value is None:
        return ""
    if value > 0:
        return UP
    if value < 0:
        return DOWN
    return FLAT


def fmt_money(yuan, signed: bool = True) -> str:
    """元 → 万/亿 自适应。signed=True 时强制带正负号。"""
    if yuan is None:
        return "—"
    wan = yuan / 10000.0
    if abs(wan) >= 10000:
        text = f"{wan / 10000.0:.2f}亿"
    else:
        text = f"{wan:.0f}万"
    if signed and wan > 0:
        text = "+" + text
    return text


def fmt_hand(hand) -> str:
    """手 → 万手。"""
    if hand is None:
        return "—"
    return f"{hand / 10000.0:.2f}万手"


def _moving_average(values: list[float], window: int):
    if len(values) < window:
        return None
    return sum(values[-window:]) / float(window)


# ---------------------------------------------------------------- 数据加工

def compute_metrics(bundle: dict, cfg: dict, flow_history: list[dict] | None = None) -> dict:
    """把原始数据整理成报告需要的中间指标（全部为客观计算，不含判断）。"""
    report_cfg = cfg.get("report", {}) or {}
    quote = bundle.get("quote") or {}
    kline = bundle.get("kline") or []
    flow = bundle.get("fundflow")

    window_n = int(report_cfg.get("five_day_count", 5) or 5)
    metrics: dict = {"window_n": window_n}

    # --- 近 N 日走势窗口 ---
    window = kline[-window_n:] if len(kline) >= window_n else kline
    prev_row = kline[-(window_n + 1)] if len(kline) >= window_n + 1 else None

    metrics["window"] = window
    if window and prev_row and prev_row.get("close"):
        metrics["window_change_pct"] = (window[-1]["close"] / prev_row["close"] - 1) * 100
    else:
        metrics["window_change_pct"] = quote.get("chg_5d") if window_n == 5 else None

    if window:
        highs = [r["high"] for r in window if r.get("high")]
        lows = [r["low"] for r in window if r.get("low")]
        vols = [r["volume_hand"] for r in window if r.get("volume_hand")]
        metrics["window_high"] = max(highs) if highs else None
        metrics["window_low"] = min(lows) if lows else None
        metrics["window_avg_volume"] = (sum(vols) / len(vols)) if vols else None
        if metrics["window_avg_volume"] and quote.get("volume_hand"):
            metrics["volume_vs_avg"] = quote["volume_hand"] / metrics["window_avg_volume"]

    # --- 均线（日K 最后一根若为当日，则天然是实时口径）---
    closes = [r["close"] for r in kline if r.get("close")]
    metrics["ma5"] = _moving_average(closes, 5)
    metrics["ma20"] = _moving_average(closes, 20)

    # --- 大资金 ---
    metrics["fundflow"] = flow
    # 优先用数据源自带的日线历史；没有（例如降级到分钟级接口）就用本地存档累积
    hist: list[dict] = []
    if flow and flow.get("history"):
        hist = [{"trade_date": r.get("date"), "main": r.get("main")} for r in flow["history"]]
        metrics["flow_hist_origin"] = "数据源"
    elif flow_history:
        hist = list(flow_history)
        metrics["flow_hist_origin"] = "本地存档"
    else:
        metrics["flow_hist_origin"] = ""

    if hist:
        recent = hist[-window_n:]
        vals = [r.get("main") for r in recent if r.get("main") is not None]
        metrics["fundflow_window_sum"] = sum(vals) if vals else None
        metrics["fundflow_window_days"] = len(recent)
    else:
        metrics["fundflow_window_sum"] = None
        metrics["fundflow_window_days"] = 0

    # --- 内外盘（仅腾讯源提供，作为辅助参考，不等同于主力资金）---
    outer, inner = quote.get("outer_hand"), quote.get("inner_hand")
    if outer and inner:
        metrics["net_active_hand"] = outer - inner

    return metrics


# ---------------------------------------------------------------- 渲染

def build_report(bundle: dict, cfg: dict, slot: dict,
                 now: datetime | None = None,
                 flow_history: list[dict] | None = None) -> dict:
    """生成报告 dict：{title, text, markdown, trade_date, is_intraday, ok}"""
    now = now or datetime.now()
    report_cfg = cfg.get("report", {}) or {}
    prefix = report_cfg.get("title_prefix", "山西焦煤 000983")
    slot_name = slot.get("name") or slot.get("id") or ""
    intraday = bool(slot.get("intraday"))
    quote = bundle.get("quote")

    if not quote:
        return _build_failure_report(bundle, cfg, slot, now)

    metrics = compute_metrics(bundle, cfg, flow_history)
    price_label = "现价" if intraday else "收盘"
    state_label = "盘中快照" if intraday else "当日定稿"

    m = metrics
    lines: list[str] = []
    lines.append(f"{prefix} ｜ {slot_name}{'快照' if intraday else '摘要'}")
    lines.append(f"{quote.get('quote_time') or now.strftime('%Y-%m-%d %H:%M')} {state_label}")
    lines.append("")

    # 1) 价格
    lines.append(f"{price_label} {fmt_price(quote.get('price'))} "
                 f"{fmt_arrow(quote.get('change_pct'))}{fmt_pct(quote.get('change_pct'))}"
                 f"（{fmt_price(quote.get('change'))}）")
    lines.append(f"今开 {fmt_price(quote.get('open'))} ｜ "
                 f"高 {fmt_price(quote.get('high'))} ｜ "
                 f"低 {fmt_price(quote.get('low'))}")
    extras = []
    for label, key in (("振幅", "amplitude_pct"), ("换手", "turnover_pct")):
        if quote.get(key) is not None:
            extras.append(f"{label} {fmt_pct(quote[key], signed=False)}")
    if quote.get("volume_ratio") is not None:
        extras.append(f"量比 {quote['volume_ratio']:.2f}")
    if extras:
        lines.append(" ｜ ".join(extras))

    # 2) 成交
    vol_text = fmt_hand(quote.get("volume_hand"))
    amt_text = fmt_money(quote.get("amount_yuan"), signed=False) if quote.get("amount_yuan") else "—"
    lines.append(f"成交 {vol_text} / {amt_text}")

    # 3) 大资金
    if report_cfg.get("include_fund_flow", True):
        flow = m.get("fundflow")
        lines.append("")
        lines.append("【大资金】")
        if flow and flow.get("main") is not None:
            main_pct = flow.get("main_pct")
            suffix = f"（占成交 {main_pct:+.2f}%）" if main_pct is not None else ""
            lines.append(f"主力净流入 {fmt_money(flow.get('main'))}{suffix}")
            parts = []
            if flow.get("xbig") is not None:
                parts.append(f"超大单 {fmt_money(flow['xbig'])}")
            if flow.get("big") is not None:
                parts.append(f"大单 {fmt_money(flow['big'])}")
            if parts:
                lines.append(" ｜ ".join(parts))
            if m.get("fundflow_window_sum") is not None:
                days = int(m.get("fundflow_window_days") or 0)
                need = int(m["window_n"])
                label = f"近{days}日主力合计" if days < need else f"近{need}日主力合计"
                note = ""
                if days < need:
                    note = f"（本地存档累计，满{need}日后完整）"
                elif m.get("flow_hist_origin") == "本地存档":
                    note = "（本地存档累计）"
                lines.append(f"{label} {fmt_money(m['fundflow_window_sum'])}{note}")
        else:
            lines.append("主力资金未取到（数据源不可用）")
            # 降级参考：内外盘差额与"主力净流入"是两套不同口径，必须显式区分，不能替代
            net_active = m.get("net_active_hand")
            if report_cfg.get("degraded_fund_proxy", True) and net_active is not None:
                lines.append(f"参考：主动买-主动卖 {net_active:+.0f} 手")
                lines.append("（内外盘口径，非主力净流入，仅供方向参考）")

    # 4) 近 N 日走势
    if report_cfg.get("include_five_day", True) and m.get("window"):
        lines.append("")
        lines.append(f"【近{m['window_n']}日走势】")
        all_k = bundle.get("kline") or []
        start = max(0, len(all_k) - len(m["window"]))
        for offset, row in enumerate(m["window"]):
            idx = start + offset
            chg = None
            if idx > 0 and all_k[idx - 1].get("close"):
                base = all_k[idx - 1]["close"]
                if base:
                    chg = (row["close"] / base - 1) * 100
            chg_text = fmt_pct(chg) if chg is not None else "—"
            lines.append(f"{row['date'][5:]}  {fmt_price(row['close'])} "
                         f"{fmt_arrow(chg)}{chg_text}")

        summary = [f"{m['window_n']}日累计 {fmt_pct(m.get('window_change_pct'))}"]
        if m.get("window_low") is not None and m.get("window_high") is not None:
            summary.append(f"区间 {fmt_price(m['window_low'])}~{fmt_price(m['window_high'])}")
        lines.append(" ｜ ".join(summary))

        if m.get("window_avg_volume"):
            ratio = m.get("volume_vs_avg")
            extra = f"（今日 {ratio:.2f}×）" if ratio else ""
            lines.append(f"{m['window_n']}日均量 {fmt_hand(m['window_avg_volume'])}{extra}")

    # 5) 均线
    if report_cfg.get("include_ma", True) and m.get("ma5"):
        ma_line = f"MA5 {fmt_price(m['ma5'], 3)}"
        if m.get("ma20"):
            ma_line += f" ｜ MA20 {fmt_price(m['ma20'], 3)}"
            if quote.get("price"):
                pos = "高于" if quote["price"] >= m["ma20"] else "低于"
                ma_line += f"（{price_label}{pos}MA20）"
        lines.append("")
        lines.append(ma_line)

    # 6) 收尾
    lines.append("")
    src = []
    if quote.get("source"):
        src.append(f"行情 {quote['source']}")
    if m.get("fundflow", {}) and (m.get("fundflow") or {}).get("source"):
        src.append(f"资金 {(m['fundflow'] or {}).get('source')}")
    if bundle.get("kline"):
        src.append("日K 前复权")
    if src:
        lines.append("数据源：" + " / ".join(src))
    if report_cfg.get("include_disclaimer", True):
        lines.append(DISCLAIMER)

    text = "\n".join(lines)
    change_pct = quote.get("change_pct")
    title = (f"{prefix} {slot_name}｜"
             f"{fmt_arrow(change_pct)}{fmt_pct(change_pct)}")
    if not intraday:
        title = f"{prefix} {slot_name}｜{fmt_price(quote.get('price'))} "
        title += f"{fmt_arrow(change_pct)}{fmt_pct(change_pct)}"

    return {
        "title": title,
        "text": text,
        "markdown": _to_markdown(text),
        "trade_date": _trade_date(quote, now),
        "is_intraday": intraday,
        "ok": True,
        "warnings": bundle.get("warnings", []),
    }


def _to_markdown(text: str) -> str:
    out = []
    for line in text.split("\n"):
        if line.startswith("【") and line.endswith("】"):
            out.append(f"**{line}**")
        else:
            out.append(line)
    return "\n".join(out)


def _trade_date(quote: dict, fallback: datetime) -> str:
    raw = str(quote.get("quote_time") or "").strip()
    if len(raw) >= 10 and raw[4] == "-":
        return raw[:10]
    return fallback.strftime("%Y-%m-%d")


def _build_failure_report(bundle: dict, cfg: dict, slot: dict, now: datetime) -> dict:
    """取数全部失败时发出的提醒（比沉默安全得多）。"""
    prefix = (cfg.get("report", {}) or {}).get("title_prefix", "山西焦煤 000983")
    slot_name = slot.get("name") or slot.get("id") or ""
    reasons = bundle.get("warnings") or ["未知原因"]
    lines = [
        f"{prefix} ｜ {slot_name}数据获取失败",
        now.strftime("%Y-%m-%d %H:%M"),
        "",
        "本轮未能取到行情数据，已自动重试全部数据源。",
        "可能原因：网络异常 / 数据源限流 / 非交易日 / 接口变更。",
        "",
        "失败详情：",
    ]
    lines.extend(f"· {r}" for r in reasons[:5])
    lines.append("")
    lines.append("程序会在下一个时段自动重试，无需手动干预。")
    text = "\n".join(lines)
    return {
        "title": f"{prefix} {slot_name}｜数据获取失败",
        "text": text,
        "markdown": text,
        "trade_date": now.strftime("%Y-%m-%d"),
        "is_intraday": bool(slot.get("intraday")),
        "ok": False,
        "warnings": reasons,
    }
