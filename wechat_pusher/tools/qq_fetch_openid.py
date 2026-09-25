"""抓取 QQ 机器人的 group_openid / user_openid —— 一次性使用的小工具。

## 为什么需要它

QQ 的 `group_openid` / `user_openid` **不是 QQ 号，也无法从任何接口反查**，
只能由平台在消息事件里下发。也就是说：

    机器人必须先在目标群（或单聊）里收到一条消息，你才能拿到 openid 去发推送。

这是走 QQ 通道绕不过去的一步，和"配好 AppID 就能发"的直觉不一样。

## 用法

    # 1. 到 QQ 开放平台拿到 AppID / AppSecret，设成环境变量
    setx QQ_BOT_APPID "102xxxxxx"
    setx QQ_BOT_SECRET "你的 AppSecret"        # 设完重开终端

    # 2. 把机器人加进目标群（或用 QQ 加它为好友），然后运行本工具
    python tools/qq_fetch_openid.py

    # 3. 在群里 @ 机器人 发一句话（单聊直接发消息）
    #    终端会打印出 group_openid / user_openid，复制进 config.yaml
    #    拿到后可加 --once 让它自动退出

开发期建议先加 `--sandbox`：沙箱环境不校验 IP 白名单，也不需要机器人已上线。

## 两个坑（都是本工具首版踩过的，已修）

1. **必须发心跳**。QQ 网关要求客户端每 `heartbeat_interval`（约 41 秒）发一次
   `{"op":1,"d":<last_s>`。只 IDENTIFY 不发心跳，服务端很快以
   `4009 Session timed out` 断开 —— 表现为"连上了、机器人也上线了，但一直抓不到东西"。
   （另：心跳 `d` 传最近一次事件的 `s`；没有事件时传 `null`。）
2. **超时不用非 daemon 的 Timer**。`threading.Timer` 默认非 daemon，主逻辑跑完
   进程还会挂到超时才退出，看起来像"卡住了"。改用 deadline 变量 + 循环内判断。

另外断线（4009 / op=7 / op=9）会自动重取网关并重连，最多等到 `--timeout` 为止，
所以让用户慢慢操作即可，不必掐着秒表。

## 依赖

    pip install websocket-client

不需要公网 IP、不需要开端口 —— 走的是**出站** WebSocket 长连接。
（另一条路是配 Webhook 回调，但那就得有一个公网可访问的地址，本机场景不划算。）
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import time

try:
    import requests
except ImportError:  # pragma: no cover
    sys.exit("缺少依赖 requests：pip install requests")

try:
    import websocket  # type: ignore
except ImportError:  # pragma: no cover
    sys.exit("缺少依赖 websocket-client：pip install websocket-client")

TOKEN_URL = "https://bots.qq.com/app/getAppAccessToken"
BASE_PROD = "https://api.sgroup.qq.com"
BASE_SANDBOX = "https://sandbox.api.sgroup.qq.com"

# 群聊 + 单聊消息事件
INTENT_GROUP_AND_C2C = 1 << 25

OP_DISPATCH = 0
OP_HEARTBEAT = 1
OP_IDENTIFY = 2
OP_RECONNECT = 7
OP_INVALID_SESSION = 9
OP_HELLO = 10
OP_HEARTBEAT_ACK = 11


def get_token(appid: str, secret: str, timeout: float = 15.0) -> str:
    """取 AppAccessToken。

    注意：失败时也返回 HTTP 200，错误在 body 的 code 里（实测 10004 = 机器人不存在）。
    """
    resp = requests.post(TOKEN_URL, json={"appId": appid, "clientSecret": secret},
                         timeout=timeout)
    try:
        data = resp.json()
    except ValueError:
        sys.exit(f"取 token 失败：响应不是 JSON (http={resp.status_code}) {resp.text[:200]}")

    if data.get("code") not in (None, 0):
        hints = {
            10004: "appId 对应的机器人不存在，检查 AppID 是否抄错",
            100007: "AppID 无效，或机器人状态不正常（被封禁/已删除）",
            100016: "AppID 或 AppSecret 不正确，到开放平台管理端重新复制",
            100001: "请求过于频繁，稍等再试",
        }
        code = data.get("code")
        sys.exit(f"取 token 失败 code={code} "
                 f"{data.get('message') or ''}\n提示：{hints.get(code, '请核对开放平台配置')}")

    token = data.get("access_token")
    if not token:
        sys.exit(f"取 token 返回里没有 access_token：{data}")
    return str(token)


def get_gateway(token: str, appid: str, sandbox: bool, timeout: float = 15.0) -> str:
    base = BASE_SANDBOX if sandbox else BASE_PROD
    resp = requests.get(f"{base}/gateway",
                        headers={"Authorization": f"QQBot {token}",
                                 "X-Union-Appid": appid},
                        timeout=timeout)
    try:
        data = resp.json()
    except ValueError:
        sys.exit(f"取网关地址失败：响应不是 JSON (http={resp.status_code}) {resp.text[:200]}")
    url = data.get("url")
    if not url:
        sys.exit(f"取网关地址失败 http={resp.status_code}：{data}")
    return str(url)


class OpenIdCollector:
    def __init__(self, appid: str, token: str, once: bool = False):
        self.appid = appid
        self.token = token
        self.once = once
        self.seq: int | None = None
        self.found: dict[str, str] = {}
        self.ws: websocket.WebSocketApp | None = None
        # 停止标志：拿到目标 openid（--once）或人为中断后置位
        self.stopped = False
        self._hb_thread: threading.Thread | None = None
        self._hb_stop = threading.Event()

    # ---------------------------------------------------------------- 心跳

    def _stop_heartbeat(self) -> None:
        self._hb_stop.set()
        if self._hb_thread is not None:
            self._hb_thread.join(timeout=2)
            self._hb_thread = None

    def _heartbeat_loop(self, ws, interval: float) -> None:
        """定期发 op=1 心跳。

        不发心跳的后果是**静默失败**：连接建立、机器人也显示上线，但约一个心跳周期后
        服务端以 `4009 Session timed out` 断开，看起来像"什么都没发生"。
        心跳的 d 传最近一次事件的 s（没有事件时传 None）。
        """
        while not self._hb_stop.wait(interval):
            try:
                ws.send(json.dumps({"op": OP_HEARTBEAT, "d": self.seq}))
            except Exception as exc:      # noqa: BLE001
                print(f"心跳发送失败：{exc}")
                return

    # ---------------------------------------------------------------- 输出

    def _record(self, kind: str, openid: str) -> bool:
        if not openid or self.found.get(kind) == openid:
            return False
        self.found[kind] = openid
        label = {"group": "group_openid（发到群）",
                 "user": "user_openid（发到单聊）",
                 "sender": "author.user_openid（发消息的人）"}[kind]
        print("\n" + "=" * 64)
        print(f"  拿到 {label}")
        print(f"  {openid}")
        print("=" * 64)
        return True

    def _maybe_stop(self) -> None:
        if self.once and self.ws is not None and (
                "group" in self.found or "user" in self.found):
            print("\n已拿到所需 openid，按 --once 退出。把它填进 config.yaml 的对应字段即可。")
            self.stopped = True
            self._stop_heartbeat()
            self.ws.close()

    # ---------------------------------------------------------------- 事件

    def on_open(self, ws) -> None:
        print("WebSocket 已连接，等待 HELLO…")

    def on_message(self, ws, raw: str) -> None:
        try:
            payload = json.loads(raw)
        except ValueError:
            return
        op = payload.get("op")
        t = payload.get("t")
        d = payload.get("d") or {}
        if payload.get("s") is not None:
            self.seq = payload["s"]

        if op == OP_HELLO:
            interval = float((d.get("heartbeat_interval") or 30000)) / 1000.0
            ws.send(json.dumps({
                "op": OP_IDENTIFY,
                "d": {"token": f"QQBot {self.token}",
                      "intents": INTENT_GROUP_AND_C2C,
                      "shard": [0, 1],
                      "properties": {}},
            }))
            print(f"已发送 IDENTIFY（心跳间隔 {interval:.0f}s，已启动心跳线程）。")
            # 关键：起心跳，否则 ~1 个周期后被服务端 4009 断开
            self._stop_heartbeat()
            self._hb_stop.clear()
            self._hb_thread = threading.Thread(
                target=self._heartbeat_loop, args=(ws, interval), daemon=True)
            self._hb_thread.start()
            return

        if op == OP_HEARTBEAT_ACK:
            return
        if op in (OP_RECONNECT, OP_INVALID_SESSION):
            print(f"服务端要求重连 (op={op})，将自动重连…")
            ws.close()
            return
        if op != OP_DISPATCH:
            return

        if t == "READY":
            user = d.get("user") or {}
            print(f"机器人已上线：{user.get('username')}（会话已建立）")
            print("现在去群里 @ 它 发一句话，或给它发一条单聊消息。\n")
            return
        if t == "GROUP_AT_MESSAGE_CREATE":
            print("收到群消息事件 ✅")
            self._record("group", str(d.get("group_openid") or ""))
            author = d.get("author") or {}
            self._record("sender", str(author.get("user_openid") or author.get("member_openid") or ""))
            self._maybe_stop()
            return

        if t in ("GROUP_MESSAGE_CREATE", "C2C_MESSAGE_CREATE"):
            print(f"收到消息事件 ✅ ({t})")
            author = d.get("author") or {}
            sender = str(author.get("user_openid") or author.get("member_openid") or "")
            if t == "C2C_MESSAGE_CREATE":
                self._record("user", sender)
                self._record("sender", sender)
            else:
                self._record("group", str(d.get("group_openid") or ""))
                self._record("sender", sender)
            self._maybe_stop()
            return

        if t:
            print(f"（其他事件 {t}，已忽略）")

    def on_error(self, ws, error) -> None:
        msg = str(error)
        print(f"\n连接出错：{msg}")
        if "401" in msg or "403" in msg:
            print("提示：401/403 一般是凭证问题 —— AppID/AppSecret 抄错，"
                  "或用了沙箱凭证连正式环境（反之亦然）。")

    def on_close(self, ws, code, msg) -> None:
        self._stop_heartbeat()
        print(f"连接已关闭 (code={code} {msg})")

    def run(self, sandbox: bool, deadline: float | None = None) -> None:
        """连网关等事件；断开就重取网关重连，直到抓到 openid 或到 deadline。

        重连是必要的：网关卡会定期让客户端重连（op=7），长时间空跑也会超时。
        用户是在真人操作（建群、扫码、发消息），不能要求他配合秒级窗口。
        """
        attempt = 0
        while not self.found and not self.stopped:
            if deadline is not None and time.monotonic() >= deadline:
                print("\n已到等待上限，退出。")
                return
            attempt += 1
            if attempt > 1:
                delay = min(2 ** min(attempt - 2, 3), 8)
                print(f"\n第 {attempt} 次连接尝试（{delay}s 后）…")
                time.sleep(delay)
            try:
                url = get_gateway(self.token, self.appid, sandbox)
            except SystemExit as exc:
                print(f"取网关地址失败：{exc}")
                return
            print(f"网关：{url}")
            self.ws = websocket.WebSocketApp(
                url,
                header={"Authorization": f"QQBot {self.token}",
                        "X-Union-Appid": self.appid},
                on_open=self.on_open,
                on_message=self.on_message,
                on_error=self.on_error,
                on_close=self.on_close,
            )
            self.ws.run_forever(ping_interval=30, ping_timeout=10)
            if not self.found and not self.stopped:
                print("连接已断开、openid 还没抓到，自动重连…")


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        description="抓取 QQ 机器人的 group_openid / user_openid（一次性工具）")
    p.add_argument("--appid", default=os.environ.get("QQ_BOT_APPID", ""),
                   help="机器人 AppID，默认读环境变量 QQ_BOT_APPID")
    p.add_argument("--secret", default=os.environ.get("QQ_BOT_SECRET", ""),
                   help="机器人 AppSecret，默认读环境变量 QQ_BOT_SECRET")
    p.add_argument("--sandbox", action="store_true",
                   help="连沙箱环境（开发期推荐：不校验 IP 白名单、无需机器人已上线）")
    p.add_argument("--once", action="store_true", help="拿到 openid 后自动退出")
    p.add_argument("--timeout", type=float, default=0.0,
                   help="超时秒数，0 表示一直等待（默认）")
    args = p.parse_args(argv)

    if not args.appid or not args.secret:
        print("缺少 AppID / AppSecret。请先设置环境变量：\n"
              '    setx QQ_BOT_APPID "102xxxxxx"\n'
              '    setx QQ_BOT_SECRET "你的 AppSecret"\n'
              "或直接用参数：--appid xxx --secret yyy", file=sys.stderr)
        return 2

    env = "沙箱" if args.sandbox else "正式"
    print(f"环境：{env}")
    print("1/2 取 access_token …")
    token = get_token(args.appid, args.secret)
    print(f"    OK（{token[:6]}…{token[-4:]}）")

    print("2/2 连接网关，等待消息事件（断线自动重连）…")
    if args.timeout and args.timeout > 0:
        print(f"    最长等待 {args.timeout:.0f} 秒")
    print("    Ctrl+C 退出")
    collector = OpenIdCollector(args.appid, token, once=args.once)
    deadline = (time.monotonic() + args.timeout) if (args.timeout and args.timeout > 0) else None

    try:
        collector.run(args.sandbox, deadline=deadline)
    except KeyboardInterrupt:
        print("\n已中断。")
    finally:
        collector._stop_heartbeat()

    if collector.found:
        print("\n本次抓到的 openid：")
        for k, v in collector.found.items():
            print(f"  {k:<7} {v}")
        return 0
    print("\n没有抓到任何 openid —— 确认机器人已在群里被 @ 过，或用 QQ 给它发过消息。")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
