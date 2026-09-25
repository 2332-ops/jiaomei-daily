#!/usr/bin/env python
"""命令行入口 —— 可以直接被计划任务 / crontab / CI 调用。

用法示例：

    # 最简单：直接发一条
    python send.py --title "打卡" --text "程序正在运行"

    # 从文件读正文（适合发报告）
    python send.py --title "山西焦煤 收盘摘要" --text-file report.txt

    # 从管道读（把别的程序的输出直接推过来）
    python other_task.sh | python send.py --title "任务结果" --stdin

    # 只发给某几个通道 / 调换分发模式
    python send.py --title "告警" --text "..." --only wecom_app --mode failover

    # 上线前先自检（会真的去取 access_token 验证凭证）
    python send.py --check

    # 看有哪些通道类型可用
    python send.py --list

退出码：0 至少一个通道成功；1 全部失败；2 配置或用法错误。
计划任务里可以直接靠退出码判断成败。
"""

from __future__ import annotations

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from wxpush import Pusher, __version__, list_channels, load_config, setup_logging  # noqa: E402
from wxpush.errors import PushError  # noqa: E402
from wxpush.logging_setup import get_logger  # noqa: E402
from wxpush.text_utils import mask_secret  # noqa: E402

_SECRET_KEYS = {"token", "secret", "corpsecret", "sendkey", "password", "webhook",
                "access_token", "openid", "key"}


def _mask_config(cfg: dict) -> dict:
    """打印配置时把凭证遮掉。"""
    def walk(node):
        if isinstance(node, dict):
            return {k: (mask_secret(v) if k.lower() in _SECRET_KEYS and isinstance(v, str)
                        else walk(v)) for k, v in node.items()}
        if isinstance(node, list):
            return [walk(x) for x in node]
        return node
    return walk(cfg)


def _read_text(args) -> str | None:
    if args.text is not None:
        return args.text
    if args.text_file:
        with open(args.text_file, "r", encoding="utf-8") as f:
            return f.read()
    if args.stdin:
        return sys.stdin.read()
    return None


def _read_markdown(args) -> str | None:
    if args.markdown:
        return args.markdown
    if args.markdown_file:
        with open(args.markdown_file, "r", encoding="utf-8") as f:
            return f.read()
    return None


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="send.py",
        description="把文本消息推送到微信（企业微信 / 公众号 / PushPlus / Server酱）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    p.add_argument("--config", "-c", default=None, help="配置文件路径，默认找 ./config.yaml")
    p.add_argument("--title", "-t", default="", help="消息标题")
    p.add_argument("--text", default=None, help="消息正文")
    p.add_argument("--text-file", default=None, help="从文件读取正文")
    p.add_argument("--markdown", default=None, help="Markdown 正文（支持 markdown 的通道优先用它）")
    p.add_argument("--markdown-file", default=None, help="从文件读取 Markdown 正文")
    p.add_argument("--stdin", action="store_true", help="从标准输入读取正文")

    p.add_argument("--only", default=None, help="只发给这些通道（name 或 type，逗号分隔）")
    p.add_argument("--skip", default=None, help="排除这些通道")
    p.add_argument("--mode", choices=("all", "failover", "require_all"), default=None,
                   help="分发模式，覆盖配置里的 mode")
    p.add_argument("--dry-run", action="store_true", help="只走流程不发请求")
    p.add_argument("--dedup-key", default=None, help="发送去重键（需在配置里开启 dedup）")
    p.add_argument("--dedup-ttl", type=float, default=None, help="去重有效期（秒）")

    p.add_argument("--list", action="store_true", help="列出支持的通道类型后退出")
    p.add_argument("--check", action="store_true", help="自检配置与凭证，不发送消息")
    p.add_argument("--print-config", action="store_true", help="打印生效配置（凭证已打码）")
    p.add_argument("--json", action="store_true", help="以 JSON 输出结果，便于被程序调用")

    p.add_argument("--log-level", default="INFO",
                   choices=("DEBUG", "INFO", "WARNING", "ERROR"))
    p.add_argument("--log-file", default=None, help="同时把日志写入文件")
    p.add_argument("--quiet", action="store_true", help="不输出到控制台（只写 --log-file）")
    p.add_argument("--version", action="version", version=f"wxpush {__version__}")
    return p


def cmd_list(as_json: bool) -> int:
    rows = list_channels()
    if as_json:
        print(json.dumps(rows, ensure_ascii=False, indent=2))
        return 0
    print(f"wxpush {__version__} 支持 {len(rows)} 种通道：\n")
    for r in rows:
        print(f"  {r['type']:<12} {r['name']}")
        print(f"  {'':<12} 必填: {', '.join(r['required'])}")
        print(f"  {'':<12} markdown支持: {'是' if r['markdown'] else '否'}"
              f" | 纯文本上限: {r['text_limit_bytes']} 字节"
              f"（约 {r['text_limit_bytes'] // 3} 个汉字）")
        if r["aliases"]:
            print(f"  {'':<12} 别名: {', '.join(r['aliases'])}")
        print()
    return 0


def cmd_check(pusher: Pusher, as_json: bool) -> int:
    report = pusher.check()
    ok = all(r["ok"] for r in report)
    if as_json:
        print(json.dumps({"ok": ok, "channels": report}, ensure_ascii=False, indent=2))
    else:
        print(f"配置自检：{'全部通过' if ok else '存在问题'}\n")
        for r in report:
            mark = "[OK]  " if r["ok"] else "[FAIL]"
            print(f"  {mark} {r['channel']:<16} {r['detail']}")
        if not pusher.channels:
            print("\n  提示：配置里没有任何 enabled: true 的通道。")
            print("        先照着 config.example.yaml 打开一个通道，或看 README 的"
                  "「5 分钟接入」章节。")
    return 0 if ok else 1


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    if args.list:
        return cmd_list(args.json)

    log = setup_logging(args.log_level, log_file=args.log_file, quiet=args.quiet)
    plog = get_logger("pusher")
    if not args.quiet:
        log.debug("argv=%s", argv if argv is not None else sys.argv[1:])

    # ---------------------------------------------------------- 加载配置
    try:
        cfg = load_config(args.config)
    except Exception as exc:  # noqa: BLE001
        print(f"配置加载失败: {exc}", file=sys.stderr)
        return 2

    if args.print_config:
        print(json.dumps(_mask_config(cfg), ensure_ascii=False, indent=2))
        return 0

    # ---------------------------------------------------------- 构造 Pusher
    try:
        pusher = Pusher(config=cfg, logger=plog, dry_run=args.dry_run)
    except PushError as exc:
        print(f"初始化失败: {exc}", file=sys.stderr)
        return 2

    try:
        if args.check:
            return cmd_check(pusher, args.json)

        title = args.title
        text = _read_text(args)
        markdown = _read_markdown(args)

        if not title and not text and not markdown:
            print("没有可发送的内容。用 --title / --text / --text-file / --stdin 提供，"
                  "或用 --check / --list。", file=sys.stderr)
            return 2

        if args.dry_run:
            log.info("dry-run 模式：只走流程，不会真的发出请求")

        report = pusher.send(title, text, markdown,
                             only=args.only, skip=args.skip, mode=args.mode,
                             dedup_key=args.dedup_key, dedup_ttl=args.dedup_ttl)

        if args.json:
            print(json.dumps(report.to_dict(), ensure_ascii=False, indent=2))
        else:
            print(f"\n{report.summary}")

        if report.deduped:
            return 0
        if not report.results:
            return 1
        if args.mode == "require_all" or cfg.get("mode") == "require_all":
            return 0 if report.all_ok else 1
        return 0 if report.any_ok else 1
    finally:
        pusher.close()


if __name__ == "__main__":
    sys.exit(main())
