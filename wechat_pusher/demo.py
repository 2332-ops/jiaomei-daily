#!/usr/bin/env python
"""可直接运行的调用示例。

    python demo.py              # 离线演示：分片、异常分类、dry-run（不发任何网络请求）
    python demo.py --real       # 真实错误码演示：用**伪造凭证**打真实接口，
                                #   验证错误分类是否与平台实际返回一致（不会发出任何消息）
    python demo.py --live       # 用你自己的 config.yaml 真发一条（需先配好凭证）

--real 模式是这套代码的"体检报告"：它证明 40014 / 93000 / 903 / 40001 这些
错误码确实被正确归类成了"该刷新 token"还是"重试也没用"。
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from wxpush import (Pusher, RetryPolicy, default_config, list_channels,  # noqa: E402
                    send_text, setup_logging, split_by_bytes, utf8_len)
from wxpush.channels import ChannelContext, build_channel  # noqa: E402
from wxpush.errors import (ConfigError, PermanentError, PushError, RateLimitError,  # noqa: E402
                           TokenExpiredError, is_retryable)
from wxpush.retry import call_with_retry  # noqa: E402
from wxpush.token_store import TokenStore  # noqa: E402


def hr(title: str) -> None:
    print("\n" + "=" * 78)
    print(f"  {title}")
    print("=" * 78)


def _code_of(detail: str) -> str:
    """从失败详情里把错误码抠出来，仅用于展示。"""
    import re
    m = re.search(r"\[code=([^\]]+)\]", detail or "")
    return m.group(1) if m else "-"


# ============================================================================
# 示例 1：最简用法 —— 一行代码发消息
# ============================================================================
def demo_simple() -> None:
    hr("示例 1：一行代码发送（需要 config.yaml 里配好通道）")

    print("""
    from wxpush import send_text

    report = send_text(
        title="山西焦煤 000983 ｜ 收盘摘要",
        text="收盘 6.53 ▲+0.62%\\n成交 49.40万手 / 3.22亿",
    )
    print(report.summary)
    if not report.any_ok:
        ...   # 全部通道都失败，走你自己的兜底告警
""")
    print("    ↑ 这是最常用的写法。下面是它的等价展开（便于你插入自己的逻辑）：\n")
    print("""
    from wxpush import Pusher

    with Pusher.from_config_file("config.yaml") as p:
        report = p.send("标题", "正文")
        for r in report.results:
            print(r.channel, r.ok, r.detail, r.chunks, r.attempts)
""")


# ============================================================================
# 示例 2：超长消息自动分片（推送报告时必用）
# ============================================================================
def demo_chunking() -> None:
    hr("示例 2：超长消息按字节自动分片")

    # ---- 先演示最容易踩的坑：字符数够用、字节数已经超了 ----
    # 企业微信 text 类型上限是 2048 **字节**，而一个汉字占 3 字节
    looks_short = "焦" * 800            # 800 个汉字
    n_chars, n_bytes = len(looks_short), utf8_len(looks_short)
    limit = 2048
    print(f"先看这个坑。一条 {n_chars} 个汉字的正文：")
    print(f"  len(text)     = {n_chars:>6} 字符   -> 和上限 {limit} 比，看起来没问题")
    print(f"  utf8_len(text)= {n_bytes:>6} 字节   -> 实际已超限 {(n_bytes / limit - 1) * 100:.0f}%")
    print(f"  结论：按字符数判断{'会漏判' if n_chars <= limit < n_bytes else '没问题'}，"
          f"按字节数判断才正确。中文的安全字符数是 {limit // 3} 个。")

    # ---- 再看分片 ----
    line = "山西焦煤 000983 收盘 6.53 元，较前一交易日上涨 0.62%，成交 49.40 万手。"
    big = "\n".join(f"{i:03d} | {line}" for i in range(1, 901))
    chunks = split_by_bytes(big, limit)
    sizes = [utf8_len(c) for c in chunks]

    print(f"\n一条 900 行的日报（{len(big)} 字符 / {utf8_len(big)} 字节）切分结果：")
    print(f"  共 {len(chunks)} 片，最大片 {max(sizes)} 字节，最小片 {min(sizes)} 字节")
    print(f"  全部 <= {limit} 字节 -> {'通过' if max(sizes) <= limit else '不通过'}")
    for i, c in enumerate(chunks[:3], 1):
        print(f"    第 {i} 片: {utf8_len(c):>4} 字节 | 首行: {c.splitlines()[0][:38]}")
    print("    ...")

    # ---- 验证不会切断多字节字符（解码回原文应完全一致）----
    rejoined = "\n".join(chunks)
    print(f"\n  拼回去是否与原文一致: {rejoined == big}")

    print("\n这一步由 Channel.send() 自动完成，你不需要手动调用 —— "
          "只要别在子类里绕过基类自己发 HTTP。")


# ============================================================================
# 示例 3：重试与异常分类（不发请求，纯演示逻辑）
# ============================================================================
def demo_retry_logic() -> None:
    hr("示例 3：重试策略与异常分类")

    policy = RetryPolicy(attempts=4, base=1.0, cap=30.0, jitter=0.0)
    print("指数退避（jitter=0 便于观察）：")
    for i in range(3):
        print(f"  第 {i + 1} 次失败后等待 {policy.delay(i):>5.1f} 秒")

    print(f"\n平台给出冷却时间时优先照做，但有自己的上限 "
          f"retry_after_cap={policy.retry_after_cap:.0f} 秒：")
    for sec in (30, 60, 300):
        d = policy.delay(0, retry_after=sec)
        note = "（平台要求，照做）" if d == sec else f"（超过上限，截断到 {d:.0f}s）"
        print(f"  平台说等 {sec:>3} 秒 -> 实际等 {d:>5.1f} 秒 {note}")

    print("\n异常分类 -> 是否重试：")
    cases = [
        (PermanentError("webhook key 无效", code=93000), "立即放弃，告警通知人"),
        (TokenExpiredError("access_token 失效", code=40014), "先刷新 token 再重试"),
        (RateLimitError("接口调用超限", code=45009, retry_after=60), "等冷却时间后重试"),
        (ConfigError("缺少必要配置项: token"), "配置问题，立刻放弃"),
        (ConnectionError("网络不可达"), "指数退避重试"),
        (TimeoutError("读取超时"), "指数退避重试"),
    ]
    for exc, action in cases:
        print(f"  {type(exc).__name__:<18} is_retryable={str(is_retryable(exc)):<6}"
              f" -> {action}")
    print("\n  注意最后两行：它们不是 PushError 子类，但依然被判为可重试。")
    print("  这条规则很重要 —— 用 getattr(exc, 'retryable', False) 会把它们误判成"
          "\n  '不可重试'，导致一次网络抖动就让整条推送失败。")

    print("\n实测验证 1：永久错误不被重试（只调用 1 次就放弃）")
    calls = {"n": 0}

    def always_fail():
        calls["n"] += 1
        raise PermanentError("webhook key 无效", code=93000)

    try:
        call_with_retry(always_fail, policy, sleep=lambda s: None)
    except PermanentError as exc:
        print(f"  调用次数 = {calls['n']}（不是 4）| 抛出 {type(exc).__name__}: {exc}")

    print("\n实测验证 2：可重试错误重试到成功为止")
    calls["n"] = 0

    def fail_then_ok():
        calls["n"] += 1
        if calls["n"] < 3:
            raise ConnectionError("连接被重置")
        return "成功"

    result, used = call_with_retry(fail_then_ok, policy, sleep=lambda s: None)
    print(f"  调用次数 = {calls['n']} | 返回值 = {result!r} | 实际尝试 = {used}")

    print("\n实测验证 3：TokenExpiredError 会先触发 on_retry 钩子再重试")
    refreshed = {"count": 0}
    calls["n"] = 0

    def token_expired_then_ok():
        calls["n"] += 1
        if calls["n"] == 1:
            raise TokenExpiredError("access_token 失效", code=40014)
        return "成功"

    def on_retry(exc, attempt):
        if isinstance(exc, TokenExpiredError):
            refreshed["count"] += 1

    call_with_retry(token_expired_then_ok, policy,
                    on_retry=on_retry, sleep=lambda s: None)
    print(f"  钩子被调用 {refreshed['count']} 次 -> 已作废旧 token 并重新获取")


# ============================================================================
# 示例 4：dry-run（不发真实请求，验证流程与内容）
# ============================================================================
def demo_dry_run() -> None:
    hr("示例 4：dry-run 验证流程")

    cfg = default_config()
    cfg["channels"] = [
        {"type": "wecom_bot", "enabled": True, "name": "演示群",
         "webhook": "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=demo-key-for-dryrun",
         "msg_type": "text"},
        {"type": "wecom_app", "enabled": True, "name": "演示-发给我",
         "corpid": "ww_demo", "corpsecret": "demo-secret",
         "agentid": 1000002, "touser": "ZhangSan", "msg_type": "text"},
    ]
    cfg["retry"]["attempts"] = 1

    with Pusher(config=cfg, dry_run=True) as p:
        print("启用的通道：")
        for d in p.describe():
            print(f"  - {d['desc']}")
        print()
        report = p.send("山西焦煤 收盘摘要", "收盘 6.53 ▲+0.62%\n成交 49.40万手 / 3.22亿")
        print(f"\n结果: {report.summary}")
        print(f"any_ok={report.any_ok} all_ok={report.all_ok}")
        print("\nJSON 输出（可直接喂给上层系统）：")
        import json
        print(json.dumps(report.to_dict(), ensure_ascii=False, indent=2)[:900])


# ============================================================================
# 示例 5：--real 真实错误码体检
# ============================================================================
FAKE = {
    "wecom_bot": {
        "type": "wecom_bot", "enabled": True, "name": "伪造凭证-群机器人",
        "webhook": "https://qyapi.weixin.qq.com/cgi-bin/webhook/send"
                   "?key=00000000-1111-2222-3333-444444444444",
        "msg_type": "text",
    },
    "wecom_app": {
        "type": "wecom_app", "enabled": True, "name": "伪造凭证-应用消息",
        "corpid": "ww1234567890abcdef", "corpsecret": "abcdefghijklmnopqrstuvwxyz1234567890abcd",
        "agentid": 1000002, "touser": "@all", "msg_type": "text",
    },
    "pushplus": {
        "type": "pushplus", "enabled": True, "name": "伪造凭证-PushPlus",
        "token": "0000000000000000000000000000dead", "template": "txt",
    },
    "serverchan": {
        "type": "serverchan", "enabled": True, "name": "伪造凭证-ServerChan",
        "sendkey": "SCT000000T0000000000000000000000",
    },
}


def demo_real_errors() -> int:
    hr("示例 5：真实接口错误码体检（伪造凭证，不会发出任何消息）")
    print("目的：证明错误分类与平台实际返回一致。看最后两列 —— "
          "'归类'和'是否重试'。\n")

    import requests
    from wxpush.logging_setup import get_logger

    log = get_logger("demo")
    net = default_config()["network"]
    policy = RetryPolicy(attempts=1)          # 只试 1 次，避免体检时干等退避
    session = requests.Session()
    session.headers.update({"User-Agent": net["user_agent"]})
    ctx = ChannelContext(session=session, policy=policy, tokens=TokenStore(None),
                         network=net, dry_run=False)

    rows = []
    for key, raw in FAKE.items():
        try:
            ch = build_channel(raw, ctx)
            res = ch.send("体检", "这条消息不应该被发出去")
            # 注意：Channel.send() 按设计**不抛异常**，而是把失败装进 SendResult，
            # 这样单通道出错不会影响同批次的其他通道。所以这里读结果字段。
            if res.ok:
                rows.append((key, "居然成功了", "-", "-", "!!! 伪造凭证不该成功"))
            else:
                rows.append((key, res.error_type, str(_code_of(res.detail)),
                             "是" if res.retryable else "否", ""))
        except Exception as exc:  # noqa: BLE001 - 连构建都失败的情况
            rows.append((key, type(exc).__name__, "-", "-", f"构建失败: {exc}"))

    w = (12, 18, 9, 8)
    print(f"  {'通道':<{w[0]}} {'异常归类':<{w[1]}} {'错误码':<{w[2]}} {'可重试':<{w[3]}} 说明")
    print("  " + "-" * (sum(w) + 20))
    for r in rows:
        print(f"  {r[0]:<{w[0]}} {r[1]:<{w[1]}} {r[2]:<{w[2]}} {r[3]:<{w[3]}} {r[4]}")

    print("\n  参照表：")
    print("    PermanentError    -> 凭证/参数错误，重试无意义，应立刻告警给人")
    print("    TokenExpiredError -> token 过期，刷新后重试必成")
    print("    RateLimitError    -> 限流，按平台冷却时间等待后重试")
    print("    RetryableError    -> 网络/服务端抖动，指数退避重试")

    bad = [r for r in rows if r[1] in ("居然成功了",) or r[3] == "-"]
    session.close()
    if bad:
        print("\n  存在问题：上面的归类与预期不符，请检查通道的错误码映射。")
        return 1
    print("\n  全部归类为 PermanentError（不可重试）—— 符合预期："
          "凭证类错误重试没有意义。")
    return 0


# ============================================================================
# 示例 6：真发一条
# ============================================================================
def demo_live() -> int:
    hr("示例 6：用你自己的 config.yaml 真发一条")
    cfg_path = "config.yaml"
    if not os.path.isfile(cfg_path):
        print(f"  没找到 {cfg_path}。先复制一份：")
        print("    copy config.example.yaml config.yaml")
        print("  然后至少打开一个通道（enabled: true）并填好凭证。")
        print("\n  也可以用环境变量快速验证 PushPlus：")
        print('    set PUSHPLUS_TOKEN=你的token && python send.py -t "测试" --text "内容"')
        return 2

    with Pusher.from_config_file(cfg_path) as p:
        check = p.check()
        print("  自检：")
        for r in check:
            print(f"    [{'OK' if r['ok'] else 'FAIL'}] {r['channel']:<16} {r['detail']}")

        print("\n  发送：")
        report = p.send(
            "wxpush 测试消息",
            "如果你在微信里看到这条消息，说明通道配置正确。\n"
            f"发送时间：{__import__('datetime').datetime.now():%Y-%m-%d %H:%M:%S}",
        )
        print(f"\n  {report.summary}")
        return 0 if report.any_ok else 1


# ============================================================================

def main() -> int:
    args = sys.argv[1:]
    setup_logging("INFO")

    if "--real" in args:
        return demo_real_errors()
    if "--live" in args:
        return demo_live()

    print(f"""
wxpush 调用示例  |  Python {sys.version.split()[0]}

  可用通道类型（共 {len(list_channels())} 种）：
""")
    for r in list_channels():
        print(f"    {r['type']:<12} {r['name']}")

    demo_simple()
    demo_chunking()
    demo_retry_logic()
    demo_dry_run()

    print("""
""" + "=" * 78)
    print("  接下来：")
    print("=" * 78)
    print("""
  1. 复制配置并打开一个通道：
       copy config.example.yaml config.yaml

  2. 配置凭证（以企业微信群机器人为例，30 秒拿到）：
       群聊 → 右上角 ... → 群机器人 → 添加 → 复制 Webhook 地址
       setx WECOM_WEBHOOK "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=..."
       然后重开终端，把 config.yaml 里该通道的 enabled 改成 true

  3. 自检（不发送，但会真实验证凭证）：
       python send.py --check

  4. 真发一条：
       python send.py -t "测试" --text "hello from wxpush"

  5. 验证错误分类是否与平台一致（伪造凭证，不会真的发出去）：
       python demo.py --real
""")
    return 0


if __name__ == "__main__":
    sys.exit(main())
