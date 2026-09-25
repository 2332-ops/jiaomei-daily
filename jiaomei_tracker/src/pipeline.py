"""单轮任务的完整执行流程：守卫 → 取数 → 渲染 → 落库 → 推送 → 记录。

这一层把"要不要跑 / 跑什么 / 结果如何"全部集中，run.py 与常驻调度器都调用它，
保证手动执行与定时执行走的是同一条代码路径（避免"手动能跑、定时不行"的经典问题）。
"""

from __future__ import annotations

import time
from datetime import datetime

from .config import abs_path
from .fetcher import fetch_all
from .logger import get_logger
from .notifier import Notifier
from .report import build_report
from .storage import Storage

STATUS_OK = "ok"
STATUS_SKIP_DEDUP = "skip_dedup"
STATUS_SKIP_HOLIDAY = "skip_holiday"
STATUS_SKIP_WEEKEND = "skip_weekend"
STATUS_SKIP_STALE = "skip_stale"
STATUS_FETCH_FAILED = "fetch_failed"
STATUS_SEND_FAILED = "send_failed"

# 交易日守卫：行情日期不是今天时的额外等待重试（次，秒）
STALE_RETRY_ATTEMPTS = 3
STALE_RETRY_WAIT = 20


def _quote_date(quote: dict) -> str:
    raw = str((quote or {}).get("quote_time") or "").strip()
    return raw[:10] if len(raw) >= 10 else ""


def execute(cfg: dict, slot: dict, *, dry_run: bool = False, force: bool = False,
            no_send: bool = False, storage: Storage | None = None,
            notifier: Notifier | None = None) -> dict:
    """执行一个时段的任务，返回结果字典（含 status / detail / report）。

    dry_run 与 no_send 都是"不经过本程序通道"的运行方式，但目的不同：
      dry_run —— 预览：不落库、不发送，纯粹看一眼文案长什么样。
      no_send —— 托管：守卫/取数/落库全部照常，只是把最终文案打印出来，交给
                 外层（WorkBuddy 自动化、CI 日志、人工转发）去送达。
                 这样本程序不必持有任何推送凭证，通道怎么换都不影响取数链路。
    """
    logger = get_logger()
    now = datetime.now()
    today = now.strftime("%Y-%m-%d")
    slot_id = str(slot.get("id"))
    slot_name = slot.get("name") or slot_id
    result: dict = {"slot": slot_id, "slot_name": slot_name, "status": "",
                    "detail": "", "report": None, "send_results": []}

    st = storage or Storage(abs_path(cfg, cfg["storage"].get("db_path", "data/tracker.db")))

    logger.info("===== 开始执行 时段=%s(%s) 日期=%s dry_run=%s force=%s =====",
                slot_name, slot_id, today, dry_run, force)

    # ---------- 守卫 1：周末 ----------
    if now.weekday() >= 5 and not force:
        result.update(status=STATUS_SKIP_WEEKEND, detail="周末非交易日，跳过")
        logger.info(result["detail"])
        st.log_run(today, slot_id, STATUS_SKIP_WEEKEND, result["detail"])
        return result

    # ---------- 守卫 2：配置的节假日 ----------
    holidays = [str(x) for x in (cfg.get("schedule", {}).get("holidays") or [])]
    if today in holidays and not force:
        result.update(status=STATUS_SKIP_HOLIDAY, detail=f"{today} 在休市列表中，跳过")
        logger.info(result["detail"])
        st.log_run(today, slot_id, STATUS_SKIP_HOLIDAY, result["detail"])
        return result

    # ---------- 守卫 3：同交易日同时段去重 ----------
    # 去重键用"运行日"而不是"行情日"：两者在休市、跨零点等场景会不一致，
    # 用运行日才能和下面写入 send_log 的键保持同一套口径，否则去重会失效。
    if cfg.get("dedup_by_slot", True) and not dry_run and not force \
            and st.already_sent(today, slot_id):
        result.update(status=STATUS_SKIP_DEDUP, detail=f"{today} {slot_name} 已成功推送过，跳过重复发送")
        logger.info(result["detail"])
        return result

    # ---------- 取数 ----------
    bundle = fetch_all(cfg)
    quote = bundle.get("quote")

    # ---------- 守卫 4：交易日校验（用行情自身的时间戳，不依赖外部日历）----------
    if quote:
        q_date = _quote_date(quote)
        attempt = 0
        while q_date and q_date != today and attempt < STALE_RETRY_ATTEMPTS and not force:
            attempt += 1
            logger.warning("行情日期(%s)不是今天(%s)，%s 秒后重试 %s/%s",
                           q_date, today, STALE_RETRY_WAIT, attempt, STALE_RETRY_ATTEMPTS)
            time.sleep(STALE_RETRY_WAIT)
            bundle = fetch_all(cfg)
            quote = bundle.get("quote")
            q_date = _quote_date(quote) if quote else ""

        if quote and q_date and q_date != today and not force:
            result.update(
                status=STATUS_SKIP_STALE,
                detail=f"行情最新日期为 {q_date}（非今日 {today}），判定为休市或数据源未更新，本次不推送",
            )
            logger.warning(result["detail"])
            st.log_run(today, slot_id, STATUS_SKIP_STALE, result["detail"])
            return result

    # ---------- 当日交易日（以行情自身时间戳为准）----------
    trade_date = _quote_date(quote) if quote else today
    window_n = int((cfg.get("report") or {}).get("five_day_count", 5) or 5)

    # ---------- 先落库，再渲染 ----------
    # 顺序很重要：先存档，后面算"近 N 日主力合计"才能包含当日。
    # 存档失败只记警告，绝不能因此不发消息。
    if not dry_run and quote:
        try:
            st.save_quote(trade_date, cfg["target"]["code"], quote)
            if bundle.get("fundflow"):
                st.save_fundflow(trade_date, cfg["target"]["code"], bundle["fundflow"])
        except Exception as exc:  # noqa: BLE001
            logger.warning("落库异常（不影响推送）: %s", exc)

    # ---------- 本地资金流历史（东财日线接口间歇可用，靠本地累积更稳）----------
    flow_history = st.recent_fundflow(cfg["target"]["code"], limit=window_n + 3)
    today_flow = bundle.get("fundflow")
    if today_flow and today_flow.get("main") is not None:
        if not flow_history or str(flow_history[-1].get("trade_date")) != trade_date:
            flow_history = flow_history + [{"trade_date": trade_date, "main": today_flow.get("main")}]

    # ---------- 渲染报告 ----------
    report = build_report(bundle, cfg, slot, now, flow_history=flow_history)
    result["report"] = report
    trade_date = report.get("trade_date") or trade_date

    if not report.get("ok") and not cfg.get("alert_on_failure", True):
        result.update(status=STATUS_FETCH_FAILED,
                      detail="取数失败，且已关闭失败提醒")
        logger.error(result["detail"])
        st.log_run(today, slot_id, STATUS_FETCH_FAILED, result["detail"])
        return result

    # ---------- 推送 ----------
    if dry_run or no_send:
        print("\n" + "=" * 60)
        print(("[仅输出] " if no_send else "[DRY-RUN] ") + "标题：" + report["title"])
        print("-" * 60)
        print(report["text"])
        print("=" * 60)
        if no_send:
            # 记一条 send_log，让去重守卫照常生效：本时段已经"交付"过了。
            # 渠道写成 external，是为了不冒充某个真实通道 —— 真出问题时，
            # 看日志的人应该立刻明白送达动作发生在程序之外。
            st.log_send(today, slot_id, "external", "ok",
                        f"仅输出未发送，由外部通道送达（行情日 {trade_date}）")
            result.update(status=STATUS_OK,
                          detail=f"仅输出（已落库，由外部通道送达，行情日 {trade_date}）")
            st.log_run(today, slot_id, STATUS_OK, result["detail"])
            print("(仅输出模式：已落库，本程序未发送)\n")
        else:
            print("(dry-run 模式：未对外发送，也未写入发送日志)\n")
            result.update(status=STATUS_OK, detail="dry-run 完成，未发送")
        logger.info("===== 完成 %s -> %s =====", slot_name, result["detail"])
        return result

    nf = notifier or Notifier(cfg)
    send_results = nf.send(report["title"], report["text"], report["markdown"])
    result["send_results"] = send_results

    any_ok = any(r.get("ok") for r in send_results)
    for item in send_results:
        if item.get("ok"):
            # send_log 用运行日，与去重查询口径保持一致
            st.log_send(today, slot_id, item.get("channel", ""), "ok", item.get("detail", ""))

    if any_ok:
        # 带上失败原因，而不是只报通道名 —— 只报名字的话，出问题时还得再翻一遍
        # 日志才知道是凭证错了还是被限流了
        failed = []
        for r in send_results:
            if r.get("ok"):
                continue
            why = " ".join(str(r.get("detail", "")).split())[:140]
            failed.append(f"{r['channel']}({why})" if why else str(r["channel"]))
        detail = "推送成功" + (f"；未成功通道: {', '.join(failed)}" if failed else "")
        detail += f"（行情日 {trade_date}）"
        result.update(status=STATUS_OK, detail=detail)
        st.log_run(today, slot_id, STATUS_OK, detail)
        logger.info("===== 完成 %s -> %s =====", slot_name, detail)
    else:
        detail = "全部通道推送失败: " + "; ".join(
            f"{r.get('channel')}={r.get('detail')}" for r in send_results) or "无可用通道"
        result.update(status=STATUS_SEND_FAILED, detail=detail)
        for item in send_results:
            st.log_send(today, slot_id, item.get("channel", ""), "fail", item.get("detail", ""))
        st.log_run(today, slot_id, STATUS_SEND_FAILED, detail)
        logger.error("===== 失败 %s -> %s =====", slot_name, detail)

    return result


def execute_all(cfg: dict, *, dry_run: bool = False, force: bool = False,
                no_send: bool = False) -> list[dict]:
    st = Storage(abs_path(cfg, cfg["storage"].get("db_path", "data/tracker.db")))
    nf = Notifier(cfg)
    out = []
    for slot in cfg.get("schedule", {}).get("slots", []) or []:
        out.append(execute(cfg, slot, dry_run=dry_run, force=force, no_send=no_send,
                           storage=st, notifier=nf))
    return out


def send_test_message(cfg: dict) -> list[dict]:
    """只发一条测试消息，用于验证通道配置是否正确。"""
    logger = get_logger()
    nf = Notifier(cfg)
    now = datetime.now()
    title = f"{(cfg.get('report') or {}).get('title_prefix', '山西焦煤 000983')} ｜ 推送测试"
    text = (
        f"{title}\n{now.strftime('%Y-%m-%d %H:%M:%S')}\n\n"
        "这是一条测试消息，收到即表示推送通道配置正确。\n"
        "程序会在配置的时段自动发送当日行情摘要。\n\n"
        "仅供参考，不构成投资建议"
    )
    logger.info("开始发送测试消息")
    results = nf.send(title, text, text)
    for item in results:
        logger.info("测试通道 %s -> %s (%s)", item.get("channel"), item.get("ok"), item.get("detail"))
    return results
