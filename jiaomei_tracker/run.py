#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""山西焦煤（000983）每日行情追踪与手机推送 —— 命令行入口。

常用命令
  python run.py --slot market_close       # 执行单个时段（计划任务调用这个）
  python run.py --all --dry-run           # 跑全部时段但不发送，先看文案
  python run.py --test-notify             # 只发一条测试消息，验证通道配置
  python run.py --serve                   # 常驻调度（云服务器/Linux 用）
  python run.py --print-schedule          # 输出时段清单，供安装脚本注册计划任务
  python run.py --status                  # 查看最近运行记录
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from src.config import abs_path, get_slot, load_config, parse_hhmm  # noqa: E402
from src.logger import setup_logger  # noqa: E402
from src.notifier import Notifier  # noqa: E402
from src.pipeline import execute, execute_all, send_test_message  # noqa: E402
from src.storage import Storage  # noqa: E402


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="山西焦煤（000983）每日行情追踪与手机推送",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--config", default=None, help="指定配置文件路径（默认项目根目录 config.yaml）")
    p.add_argument("--slot", default=None, help="执行指定时段，取值见 config.yaml 的 slots[].id")
    p.add_argument("--all", action="store_true", help="依次执行全部时段")
    p.add_argument("--dry-run", action="store_true", help="只取数并打印，不对外发送、不写发送日志")
    p.add_argument("--no-send", dest="no_send", action="store_true",
                   help="守卫/取数/落库照常，只打印报告交给外部通道送达（本程序不发送、不需凭证）")
    p.add_argument("--force", action="store_true", help="忽略周末/休市/去重等守卫，强制执行")
    p.add_argument("--test-notify", action="store_true", help="向所有启用通道发送一条测试消息")
    p.add_argument("--serve", action="store_true", help="常驻调度模式（需安装 APScheduler）")
    p.add_argument("--print-schedule", action="store_true", help="打印时段清单：id|HH:MM|名称")
    p.add_argument("--status", action="store_true", help="显示最近运行记录")
    p.add_argument("--purge", action="store_true", help="按 keep_days 清理历史运行日志")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    # --print-schedule 需要在日志目录之外尽量干净地输出，供 PowerShell 解析
    cfg = load_config(args.config)
    log_cfg = cfg.get("logging", {}) or {}
    setup_logger(abs_path(cfg, log_cfg.get("dir", "logs")),
                 level=log_cfg.get("level", "INFO"),
                 max_bytes=int(log_cfg.get("max_bytes", 2097152)),
                 backup_count=int(log_cfg.get("backup_count", 5)))

    if args.print_schedule:
        for slot in cfg.get("schedule", {}).get("slots", []) or []:
            hour, minute = parse_hhmm(slot.get("time"))
            print(f"{slot.get('id')}|{hour:02d}:{minute:02d}|{slot.get('name') or slot.get('id')}")
        return 0

    if args.test_notify:
        results = send_test_message(cfg)
        ok = [r for r in results if r.get("ok")]
        print("\n--- 测试结果 ---")
        for item in results:
            print(f"{'[OK]  ' if item.get('ok') else '[FAIL]'} {item.get('channel')}: {item.get('detail')}")
        if not ok:
            print("\n没有任何通道发送成功。请检查："
                  "\n1) config.yaml 里对应通道的 enabled 是否为 true"
                  "\n2) 密钥是否已通过环境变量注入（在系统里 setx 后需重开终端）"
                  "\n3) 网络是否可访问该服务")
            return 1
        return 0

    if args.status:
        st = Storage(abs_path(cfg, cfg["storage"].get("db_path", "data/tracker.db")))
        rows = st.recent_runs(limit=25)
        if not rows:
            print("暂无运行记录。")
            return 0
        print(f"{'时间':<20} {'日期':<12} {'时段':<14} {'状态':<14} 说明")
        print("-" * 100)
        for r in rows:
            print(f"{str(r.get('created_at')):<20} {str(r.get('trade_date')):<12} "
                  f"{str(r.get('slot_id')):<14} {str(r.get('status')):<14} "
                  f"{str(r.get('message'))[:44]}")
        return 0

    if args.purge:
        st = Storage(abs_path(cfg, cfg["storage"].get("db_path", "data/tracker.db")))
        n = st.purge(int((cfg.get("storage") or {}).get("keep_days", 400)))
        print(f"已清理 {n} 条历史日志。")
        return 0

    if args.serve:
        from src.scheduler import serve  # 延迟导入，避免未装 APScheduler 时报错
        serve(args.config)
        return 0

    if args.slot:
        slot = get_slot(cfg, args.slot)
        if not slot:
            available = ", ".join(str(s.get("id")) for s in
                                  cfg.get("schedule", {}).get("slots", []) or [])
            print(f"找不到时段 id={args.slot}。可用时段：{available}", file=sys.stderr)
            return 2
        result = execute(cfg, slot, dry_run=args.dry_run, force=args.force,
                         no_send=args.no_send)
        print(f"\n[{result['status']}] {result['detail']}")
        return 0 if result["status"] == "ok" or result["status"].startswith("skip") else 1

    if args.all:
        results = execute_all(cfg, dry_run=args.dry_run, force=args.force,
                              no_send=args.no_send)
        print("\n--- 执行汇总 ---")
        for r in results:
            print(f"{r['slot_name']:<10} {r['status']:<14} {r['detail']}")
        return 0

    build_parser().print_help()
    print("\n提示：先用 `python run.py --all --dry-run` 看文案，"
          "再用 `python run.py --test-notify` 验证推送通道。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
