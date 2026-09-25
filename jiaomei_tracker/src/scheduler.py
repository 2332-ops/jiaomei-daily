"""常驻调度模式（可选）。

Windows 上更推荐用"任务计划程序"（见 scripts/install_task.ps1），
因为它是"到点拉起进程、跑完退出"，不存在进程挂掉无人知晓的问题。

本模块适用于：云服务器 / Linux / 希望单进程常驻并热加载配置的场景。
依赖 APScheduler（pip install apscheduler）。
"""

from __future__ import annotations

import threading
import time
from datetime import datetime

from .config import load_config, parse_hhmm
from .logger import get_logger
from .notifier import Notifier
from .pipeline import execute
from .storage import Storage
from .config import abs_path


def _slot_signature(cfg: dict) -> str:
    slots = cfg.get("schedule", {}).get("slots", []) or []
    return "|".join(f"{s.get('id')}@{s.get('time')}" for s in slots)


def serve(config_path: str | None = None, reload_seconds: int = 60) -> None:
    try:
        from apscheduler.schedulers.blocking import BlockingScheduler
        from apscheduler.triggers.cron import CronTrigger
    except ImportError as exc:  # pragma: no cover
        raise SystemExit("常驻模式需要 APScheduler，请先执行: pip install apscheduler") from exc

    logger = get_logger()
    cfg = load_config(config_path)
    storage = Storage(abs_path(cfg, cfg["storage"].get("db_path", "data/tracker.db")))
    notifier = Notifier(cfg)

    scheduler = BlockingScheduler(timezone="Asia/Shanghai")
    state = {"signature": _slot_signature(cfg)}

    def register(slot: dict) -> None:
        hour, minute = parse_hhmm(slot.get("time"))
        job_id = f"slot_{slot.get('id')}"
        scheduler.add_job(
            _run_one,
            trigger=CronTrigger(day_of_week="mon-fri", hour=hour, minute=minute),
            args=(slot,),
            id=job_id,
            replace_existing=True,
            misfire_grace_time=600,
            coalesce=True,
            max_instances=1,
        )
        logger.info("已注册定时任务 %s 每周一至周五 %02d:%02d（%s）",
                    job_id, hour, minute, slot.get("name"))

    def _run_one(slot: dict) -> None:
        # 每次执行前重新读一次配置，保证改阈值/改文案即时生效
        try:
            fresh = load_config(config_path)
            execute(fresh, slot, storage=storage, notifier=notifier)
        except Exception as exc:  # noqa: BLE001
            logger.exception("时段 %s 执行异常: %s", slot.get("id"), exc)

    for slot in cfg.get("schedule", {}).get("slots", []) or []:
        register(slot)

    def reloader() -> None:
        while True:
            time.sleep(reload_seconds)
            try:
                fresh = load_config(config_path)
                sig = _slot_signature(fresh)
                if sig != state["signature"]:
                    logger.info("检测到发送时间配置变化：%s -> %s，重新注册任务",
                                state["signature"], sig)
                    scheduler.remove_all_jobs()
                    for slot in fresh.get("schedule", {}).get("slots", []) or []:
                        register(slot)
                    state["signature"] = sig
                    notifier.cfg = fresh
            except Exception as exc:  # noqa: BLE001
                logger.warning("配置热加载失败（继续沿用旧配置）: %s", exc)

    threading.Thread(target=reloader, daemon=True).start()
    logger.info("常驻调度已启动，当前时间 %s，按 Ctrl+C 退出",
                datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    try:
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        logger.info("常驻调度已停止")
