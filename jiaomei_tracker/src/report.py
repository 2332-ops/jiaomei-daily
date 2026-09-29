"""报告生成：把取到的原始数据渲染成适合手机屏幕阅读的短消息。

输出三种形态：
  title    推送标题（含关键数字，锁屏即可看到涨跌）
  text     纯文本正文（邮件 / Server酱 / 短信式阅读）
  markdown 带轻量标记的正文（企微 / 钉钉 / PushPlus 支持 markdown 时更好看）

【正文排序原则】
按「对短线股价的影响大小 + 变化频率」由高到低排，而不是按数据获取顺序：
  ① 期货定方向    —— 焦煤/焦炭主力，变化最快、对股价方向影响最大
  ② 股价定买卖点  —— 与人工设定的关键位比较，给出位置判断
  ③ 板块资金看情绪—— 中证煤炭/煤炭ETF + 主力资金
  ④ 公告估值定中期—— 公司公告、PE/PB/市值
  ⑤ 技术位置      —— MA、近 N 日走势（辅助，优先级最低）
一句话：期货定方向，股价定买卖点，板块资金定情绪，公告估值定中期弹性。
"""

from __future__ import annotations

import re
from datetime import datetime

UP = "▲"
DOWN = "▼"
FLAT = "＝"

DISCLAIMER = "仅供参考，不构成投资建议"


# ---------------------------------------------------------------- 格式化

def fmt_price(value, digits: int = 2) -> str:
    return "—" if value is None else f"{value:.{digits}f}"


def fmt_auto(value) -> str:
    """按量级自动选小数位：ETF（约 1.2）需要 3 位，指数（约 2355）用 2 位即可。"""
    if value is None:
        return "—"
    return fmt_price(value, 3 if abs(value) < 3 else 2)


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


# ---------------------------------------------------------------- 关键位判断
#
# 关键位是**人工设定**在 config 的 watch 段（会随行情失效，因此带设定日期，
# 报告里会印出来提醒该更新了）。这里只做客观比对，不生成任何预测。

def _level_band(repair_above, repair_zone_high, digits: int = 2) -> str:
    """修复区 → '1460-1465' / '6.38-6.40' / '6.38'。"""
    if repair_above is None:
        return ""
    if repair_zone_high and repair_zone_high != repair_above:
        return f"{fmt_price(repair_above, digits)}-{fmt_price(repair_zone_high, digits)}"
    return fmt_price(repair_above, digits)


def _level_line(price, repair_above, repair_zone_high, break_below,
                digits: int = 2) -> str | None:
    """三态位置判断：跌破破位线 / 站上修复区 / 介于两者之间。

    中性地带刻意用「破位线~修复线」表述（而不是 ~修复区上沿），
    因为决定三态归属的两个边界就是这两条线，说清楚边界比给区间更好用。
    """
    if price is None or (repair_above is None and break_below is None):
        return None
    band = _level_band(repair_above, repair_zone_high, digits)

    if break_below is not None and price < break_below:
        return f"已跌破 {fmt_price(break_below, digits)} → 风险加大"
    if repair_above is not None and price >= repair_above:
        return f"站上 {band} → 偏修复"
    if break_below is not None and repair_above is not None:
        lo, hi = fmt_price(break_below, digits), fmt_price(repair_above, digits)
        # 紧贴临界位时值得单独提示：几何上就是"正在测试支撑/压力"，
        # 这是客观描述，不含任何预测。
        if abs(price / break_below - 1) <= 0.005:
            return f"紧贴 {lo} 支撑 → 待变，跌破则风险加大"
        if abs(price / repair_above - 1) <= 0.005:
            return f"紧贴 {hi} 修复线 → 待变，站上则转强"
        return f"处于 {lo}~{hi} 之间 → 中性待变"
    if repair_above is not None:
        return f"仍在 {band} 下方"
    return f"仍在 {fmt_price(break_below, digits)} 上方"


def _asof_note(asof: str) -> str:
    """把 '2026-09-29' 渲染成 '（关键位设定于 09-29）'，日期太旧一眼能看出。"""
    asof = str(asof or "").strip()
    return f"（关键位设定于 {asof[5:]}）" if len(asof) >= 10 else ""


# ---------------------------------------------------------------- 各章节

def _sec_futures(lines: list[str], bundle: dict, watch_cfg: dict) -> None:
    """① 期货·定方向。变化频率最高的变量，排第一。"""
    futures = bundle.get("futures") or []
    if not futures:
        return

    lines.append("① 期货·定方向")
    for idx, fut in enumerate(futures):
        name = fut.get("name") or fut.get("symbol")
        if fut.get("price") is None:
            lines.append(f"{name} 未取到（{fut.get('error') or '数据源不可用'}）")
            continue

        lines.append(f"{name} {fmt_price(fut['price'], 1)} "
                     f"{fmt_arrow(fut.get('change_pct'))}{fmt_pct(fut.get('change_pct'))}"
                     f"（前收 {fmt_price(fut.get('prev_close'), 1)}）")

        # 只有第一项（焦煤主力）展开细节，其余保持一行，避免报告过长
        if idx != 0:
            continue

        lines.append(f"开 {fmt_price(fut.get('open'), 1)} ｜ 高 {fmt_price(fut.get('high'), 1)}"
                     f" ｜ 低 {fmt_price(fut.get('low'), 1)}")
        lv = _level_line(fut["price"], fut.get("repair_above"),
                         fut.get("repair_zone_high"), fut.get("break_below"), digits=1)
        if lv:
            lines.append(f"位置：{lv}")
        kline = fut.get("kline") or []
        if len(kline) >= 2:
            seq = " → ".join(fmt_price(r.get("close"), 1) for r in kline)
            lines.append(f"近{len(kline)}日：{seq}")

    judge = next((f.get("judge") for f in futures if f.get("judge")), "")
    if judge:
        asof = (watch_cfg.get("futures") or {}).get("levels_asof")
        lines.append(f"判断依据{_asof_note(asof)}：{judge}")


def _sec_stock(lines: list[str], bundle: dict, m: dict, watch_cfg: dict,
               quote: dict, price_label: str) -> None:
    """② 股价·定买卖点。"""
    lines.append("")
    lines.append("② 股价·定买卖点")
    lines.append(f"{price_label} {fmt_price(quote.get('price'))} "
                 f"{fmt_arrow(quote.get('change_pct'))}{fmt_pct(quote.get('change_pct'))}"
                 f"（{fmt_price(quote.get('change'))}）")
    lines.append(f"今开 {fmt_price(quote.get('open'))} ｜ 高 {fmt_price(quote.get('high'))}"
                 f" ｜ 低 {fmt_price(quote.get('low'))}")

    st = watch_cfg.get("stock") or {}
    lv = _level_line(quote.get("price"), st.get("repair_above"),
                     st.get("repair_zone_high"), st.get("break_below"))
    if lv:
        lines.append(f"位置：{lv}")

    extras = []
    for label, key in (("振幅", "amplitude_pct"), ("换手", "turnover_pct")):
        if quote.get(key) is not None:
            extras.append(f"{label} {fmt_pct(quote[key], signed=False)}")
    if quote.get("volume_ratio") is not None:
        extras.append(f"量比 {quote['volume_ratio']:.2f}")
    amount = fmt_money(quote.get("amount_yuan"), signed=False) if quote.get("amount_yuan") else "—"
    deal = f"成交 {fmt_hand(quote.get('volume_hand'))} / {amount}"
    if extras:
        deal += " ｜ " + " ｜ ".join(extras)
    lines.append(deal)

    if st.get("judge"):
        lines.append(f"判断依据{_asof_note(st.get('levels_asof'))}：{st['judge']}")


def _sec_manual(lines: list[str], watch_cfg: dict) -> None:
    """③④⑤ 现货基差 / 钢厂铁水 / 安监复产：暂无稳定的免费自动数据源。

    刻意留在优先级序列的中间位置（而不是挪到报告末尾）——这样全文从头到尾
    就是"按影响大小排序"的，同时一眼能看出哪几项还没被自动覆盖。
    """
    if not watch_cfg.get("manual_watch_note", True):
        return
    lines.append("")
    lines.append(watch_cfg.get("manual_watch_text")
                 or "③④⑤ 现货·铁水·安监：暂无自动数据源，需人工关注")


def _sec_sector(lines: list[str], bundle: dict, m: dict, report_cfg: dict) -> None:
    """③ 板块·情绪与资金。"""
    sector = bundle.get("sector") or []
    has_flow = bool(m.get("fundflow") and m["fundflow"].get("main") is not None)
    if not sector and not has_flow and not report_cfg.get("include_fund_flow", True):
        return

    lines.append("")
    lines.append("⑥ 板块·情绪与资金")

    seg = []
    for item in sector:
        if item.get("price") is None:
            continue
        seg.append(f"{item.get('name')} {fmt_auto(item['price'])} "
                   f"{fmt_arrow(item.get('change_pct'))}{fmt_pct(item.get('change_pct'))}")
    if seg:
        lines.append(" ｜ ".join(seg))
    elif sector:
        lines.append("板块指数未取到（数据源不可用）")

    if not report_cfg.get("include_fund_flow", True):
        return

    flow = m.get("fundflow")
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


def _sec_company(lines: list[str], bundle: dict, quote: dict) -> None:
    """④ 公司·公告与估值（影响中期弹性）。"""
    anns = bundle.get("announcements") or []
    pe, pb, mcap = quote.get("pe_ttm"), quote.get("pb"), quote.get("total_mcap_yi")
    if not anns and pe is None and pb is None and mcap is None:
        return

    lines.append("")
    lines.append("⑦ 公司·公告与估值")
    if anns:
        for item in anns:
            lines.append(f"公告 {str(item.get('date'))[5:]} {item.get('title')}")
    else:
        lines.append("近期无新公告")

    est = []
    if pe is not None:
        est.append(f"PE(TTM) {pe:.2f}")
    if pb is not None:
        est.append(f"PB {pb:.2f}")
    if mcap is not None:
        est.append(f"总市值 {mcap:.1f}亿")
    if est:
        lines.append(" ｜ ".join(est))


def _sec_technicals(lines: list[str], bundle: dict, m: dict, report_cfg: dict,
                    quote: dict, price_label: str) -> None:
    """⑤ 技术位置（辅助参考，优先级最低）。"""
    has_ma = bool(report_cfg.get("include_ma", True) and m.get("ma5"))
    has_window = bool(report_cfg.get("include_five_day", True) and m.get("window"))
    if not has_ma and not has_window:
        return

    lines.append("")
    lines.append("附：技术位置（辅助参考）")

    if has_ma:
        ma_line = f"MA5 {fmt_price(m['ma5'], 3)}"
        if m.get("ma20"):
            ma_line += f" ｜ MA20 {fmt_price(m['ma20'], 3)}"
            if quote.get("price"):
                pos = "高于" if quote["price"] >= m["ma20"] else "低于"
                ma_line += f"（{price_label}{pos}MA20）"
        lines.append(ma_line)

    if has_window:
        kline = bundle.get("kline") or []
        window = m["window"]
        start = max(0, len(kline) - len(window))
        for offset, row in enumerate(window):
            idx = start + offset
            chg = None
            if idx > 0 and kline[idx - 1].get("close"):
                base = kline[idx - 1]["close"]
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


# ---------------------------------------------------------------- 渲染

def build_report(bundle: dict, cfg: dict, slot: dict,
                 now: datetime | None = None,
                 flow_history: list[dict] | None = None) -> dict:
    """生成报告 dict：{title, text, markdown, trade_date, is_intraday, ok}"""
    now = now or datetime.now()
    report_cfg = cfg.get("report", {}) or {}
    watch_cfg = cfg.get("watch", {}) or {}
    prefix = report_cfg.get("title_prefix", "山西焦煤 000983")
    slot_name = slot.get("name") or slot.get("id") or ""
    intraday = bool(slot.get("intraday"))
    quote = bundle.get("quote")

    if not quote:
        return _build_failure_report(bundle, cfg, slot, now)

    metrics = compute_metrics(bundle, cfg, flow_history)
    price_label = "现价" if intraday else "收盘"
    state_label = "盘中快照" if intraday else "当日定稿"

    lines: list[str] = []
    lines.append(f"{prefix} ｜ {slot_name}{'快照' if intraday else '摘要'}")
    lines.append(f"{quote.get('quote_time') or now.strftime('%Y-%m-%d %H:%M')} {state_label}")
    lines.append("")

    _sec_futures(lines, bundle, watch_cfg)
    _sec_stock(lines, bundle, metrics, watch_cfg, quote, price_label)
    _sec_manual(lines, watch_cfg)
    _sec_sector(lines, bundle, metrics, report_cfg)
    _sec_company(lines, bundle, quote)
    _sec_technicals(lines, bundle, metrics, report_cfg, quote, price_label)

    # 收尾
    lines.append("")
    src = []
    if quote.get("source"):
        src.append(f"行情 {quote['source']}")
    if (metrics.get("fundflow") or {}).get("source"):
        src.append("资金 eastmoney")
    if bundle.get("kline"):
        src.append("日K 前复权")
    if bundle.get("futures"):
        src.append("期货 sina")
    if src:
        lines.append("数据源：" + " / ".join(src))
    if report_cfg.get("include_disclaimer", True):
        lines.append(DISCLAIMER)

    text = "\n".join(lines)
    change_pct = quote.get("change_pct")
    if intraday:
        title = f"{prefix} {slot_name}｜{fmt_arrow(change_pct)}{fmt_pct(change_pct)}"
    else:
        title = (f"{prefix} {slot_name}｜{fmt_price(quote.get('price'))} "
                 f"{fmt_arrow(change_pct)}{fmt_pct(change_pct)}")

    return {
        "title": title,
        "text": text,
        "markdown": _to_markdown(text),
        "trade_date": _trade_date(quote, now),
        "is_intraday": intraday,
        "ok": True,
        "warnings": bundle.get("warnings", []),
    }


_HEAD_RE = re.compile(r"^([①②③④⑤⑥⑦]|【)")


def _to_markdown(text: str) -> str:
    out = []
    for line in text.split("\n"):
        if line.startswith("【") and line.endswith("】"):
            out.append(f"**{line}**")
        elif _HEAD_RE.match(line) and len(line) <= 20:
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
