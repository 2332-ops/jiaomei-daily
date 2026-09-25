"""消息推送：多渠道 + 指数退避重试 + 失败明细回报。

通道清单（在 config.yaml 的 channels 里开关）：
  pushplus    微信推送，最省事
  serverchan  Server 酱 Turbo，微信推送
  wecom       企业微信群机器人
  dingtalk    钉钉群机器人（支持加签）
  feishu      飞书群机器人
  email       SMTP 邮件（手机邮箱 App 接收，最通用稳定）
  bark        iPhone 原生通知
  console     本机控制台（调试用）
  wxpush      转发给 wechat_pusher/ 的推送模块（推荐，见下）

关于 wxpush：本模块自带的通道是"够用"级别，重试逻辑不区分"可重试/不可重试"
（凭证错了也会重试满 3 次，白等退避）。wechat_pusher/ 那份实现把"该不该重试"
编码进了异常类型，还带字节级分片、token 缓存、多源限流。
用 `type: wxpush` 即可把消息转交给它，它支持的通道（含 QQ 机器人）在这里自动可用，
不必在两处重复实现。配置示例：

    - type: wxpush
      enabled: true
      module_path: "../wechat_pusher"       # 相对本项目根目录，或写绝对路径
      config: "config.yaml"                 # 相对 module_path
      # mode: "all"                         # all | failover | require_all
      # only: ["pushplus", "qq_bot"]        # 只发这些通道

约定：send_* 返回 (ok: bool, detail: str)，绝不抛异常给调用方——
单通道失败不应影响其他通道，也不应让整轮任务崩掉。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import random
import smtplib
import sys
import time
import urllib.parse
from email.header import Header
from email.mime.text import MIMEText
from pathlib import Path

import requests

from .logger import get_logger

_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")


def _backoff(attempt: int, base: float, cap: float) -> None:
    delay = min(cap, base * (2 ** attempt))
    time.sleep(delay * (0.7 + random.random() * 0.6))


class NonRetryableSend(Exception):
    """内部标记：这次失败重试没有意义，外层不要再退避重试。

    两种情况：
      1. 配置类错误（模块路径写错、配置文件不存在）—— 重试一百次也是一样的结果
      2. 下层已经自己重试过了（wxpush 内部有完整的重试与错误分类）
         —— 外面再套一层会让最坏情况的请求数相乘（3×4=12 次）
    """


def _non_retryable(message: str) -> NonRetryableSend:
    return NonRetryableSend(message)


class Notifier:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.retry = cfg.get("retry", {}) or {}
        self.attempts = int(self.retry.get("send_attempts", 3))
        self.base = float(self.retry.get("send_backoff_base", 2.0))
        self.cap = float(self.retry.get("send_backoff_cap", 20.0))
        self.timeout = (
            float(self.retry.get("timeout_connect", 6)),
            float(self.retry.get("timeout_read", 15)),
        )
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": _UA})

    # ------------------------------------------------------------ 对外入口

    def send(self, title: str, text: str, markdown: str | None = None) -> list[dict]:
        """向所有启用通道推送，返回每个通道的结果。"""
        logger = get_logger()
        results = []
        for channel in self.cfg.get("channels", []) or []:
            if not channel or not channel.get("enabled"):
                continue
            ch_type = str(channel.get("type", "")).lower()
            handler = getattr(self, f"_send_{ch_type}", None)
            if handler is None:
                logger.warning("未知推送通道类型，已跳过: %s", ch_type)
                results.append({"channel": ch_type, "ok": False, "detail": "通道类型不支持"})
                continue
            if not self._is_configured(ch_type, channel):
                logger.warning("通道 %s 未完成配置，已跳过", ch_type)
                results.append({"channel": ch_type, "ok": False, "detail": "缺少必要配置项"})
                continue

            ok, detail = self._with_retry(ch_type, handler, channel, title, text, markdown)
            results.append({"channel": ch_type, "ok": ok, "detail": detail})
            if ok:
                logger.info("推送成功 通道=%s", ch_type)
            else:
                logger.error("推送失败 通道=%s 详情=%s", ch_type, detail)
        return results

    # ------------------------------------------------------------ 重试包装

    def _with_retry(self, name, handler, channel, title, text, markdown):
        logger = get_logger()
        last = ""
        for i in range(self.attempts):
            try:
                ok, detail = handler(channel, title, text, markdown)
                if ok:
                    return True, detail
                last = detail
            except NonRetryableSend as exc:
                # 配置错 / 下层已重试过：立即返回，不浪费退避等待
                return False, str(exc)
            except Exception as exc:  # noqa: BLE001
                last = f"{type(exc).__name__}: {exc}"
            logger.warning("推送重试 %s/%s 通道=%s 详情=%s", i + 1, self.attempts, name, last)
            if i < self.attempts - 1:
                _backoff(i, self.base, self.cap)
        return False, last

    @staticmethod
    def _is_configured(ch_type: str, ch: dict) -> bool:
        need = {
            "pushplus": ["token"], "serverchan": ["sendkey"],
            "wecom": ["webhook"], "feishu": ["webhook"],
            "dingtalk": ["access_token"], "bark": ["key"],
            "email": ["user", "password", "mail_to"],
            "console": [], "wxpush": [],
        }.get(ch_type, [])
        return all(str(ch.get(k, "")).strip() for k in need)

    # ------------------------------------------------------------ wxpush 转发

    def _send_wxpush(self, ch, title, text, markdown):
        """转交给 wechat_pusher/ 的 wxpush 模块发送。

        刻意不做成"把 wxpush 的通道类型再实现一遍"：那份实现里
        「重试是否值得」是按异常类型分的、分片按字节算、token 带缓存，
        照抄一遍就等于维护两份会各自漂移的逻辑。
        """
        module_dir = Path(str(ch.get("module_path") or "../wechat_pusher"))
        if not module_dir.is_absolute():
            # 相对路径按**项目根目录**解析，而不是进程 cwd ——
            # 计划任务的工作目录往往不是项目根，否则会 ModuleNotFoundError
            module_dir = (Path(__file__).resolve().parent.parent / module_dir).resolve()
        if not module_dir.is_dir():
            raise _non_retryable(
                f"找不到 wxpush 模块目录: {module_dir}。检查 channel 配置里的 module_path")

        path_str = str(module_dir)
        if path_str not in sys.path:
            sys.path.insert(0, path_str)

        try:
            from wxpush import Pusher  # noqa: PLC0415 - 延迟导入：不用 wxpush 就不强依赖
        except Exception as exc:  # noqa: BLE001
            raise _non_retryable(
                f"导入 wxpush 失败: {type(exc).__name__}: {exc}。"
                "该模块需要 requests + PyYAML") from exc

        cfg_name = str(ch.get("config") or "config.yaml")
        cfg_path = Path(cfg_name)
        if not cfg_path.is_absolute():
            cfg_path = module_dir / cfg_path
        if not cfg_path.is_file():
            raise _non_retryable(f"找不到 wxpush 配置文件: {cfg_path}")

        only = ch.get("only") or None
        skip = ch.get("skip") or None
        if isinstance(only, str):
            only = [x.strip() for x in only.split(",") if x.strip()]
        if isinstance(skip, str):
            skip = [x.strip() for x in skip.split(",") if x.strip()]

        try:
            with Pusher.from_config_file(str(cfg_path)) as pusher:
                if not pusher.channels:
                    raise _non_retryable(
                        f"wxpush 里没有任何启用的通道 —— 检查 {cfg_path} 的 channels")
                report = pusher.send(title, text=text, markdown=markdown,
                                     only=only, skip=skip, mode=ch.get("mode"))
        except NonRetryableSend:
            raise
        except Exception as exc:  # noqa: BLE001
            # wxpush 内部的失败已经在它自己的重试里处理过了，这里不再退避重试，
            # 否则最坏情况请求数会相乘（3×4=12 次）
            raise _non_retryable(f"wxpush 异常: {type(exc).__name__}: {exc}") from exc

        if report.any_ok:
            return True, report.summary

        # 汇总里把"哪些通道失败了、为什么"带出来，方便一眼定位
        failed = [f"{r.channel}={r.detail}" for r in report.failed]
        why = "; ".join(failed) if failed else report.summary
        raise _non_retryable(f"{report.summary}｜失败明细: {why}")

    # ------------------------------------------------------------ 各通道实现

    def _send_console(self, ch, title, text, markdown):
        print("\n" + "=" * 56)
        print(title)
        print("-" * 56)
        print(text)
        print("=" * 56 + "\n")
        return True, "已输出到控制台"

    def _send_pushplus(self, ch, title, text, markdown):
        url = ch.get("url") or "https://www.pushplus.plus/send"
        payload = {
            "token": ch["token"],
            "title": title,
            "content": markdown or text,
            "template": "markdown",
        }
        resp = self.session.post(url, json=payload, timeout=self.timeout)
        data = resp.json()
        if data.get("code") == 200:
            return True, "ok"
        return False, f"pushplus 返回 code={data.get('code')} msg={data.get('msg')}"

    def _send_serverchan(self, ch, title, text, markdown):
        key = ch["sendkey"]
        url = ch.get("url") or f"https://sctapi.ftqq.com/{key}.send"
        resp = self.session.post(url, data={"title": title, "desp": markdown or text},
                                 timeout=self.timeout)
        data = resp.json()
        if str(data.get("code")) == "0":
            return True, "ok"
        return False, f"serverchan 返回 {json.dumps(data, ensure_ascii=False)[:200]}"

    def _send_wecom(self, ch, title, text, markdown):
        content = f"{title}\n{markdown or text}"
        payload = {"msgtype": "markdown", "markdown": {"content": content}}
        resp = self.session.post(ch["webhook"], json=payload, timeout=self.timeout)
        data = resp.json()
        if data.get("errcode") == 0:
            return True, "ok"
        return False, f"wecom errcode={data.get('errcode')} errmsg={data.get('errmsg')}"

    def _send_dingtalk(self, ch, title, text, markdown):
        url = "https://oapi.dingtalk.com/robot/send?access_token=" + str(ch["access_token"])
        secret = str(ch.get("secret") or "").strip()
        if secret:
            ts = str(round(time.time() * 1000))
            string_to_sign = f"{ts}\n{secret}"
            digest = hmac.new(secret.encode("utf-8"), string_to_sign.encode("utf-8"),
                              digestmod=hashlib.sha256).digest()
            sign = urllib.parse.quote_plus(base64.b64encode(digest))
            url += f"&timestamp={ts}&sign={sign}"
        payload = {"msgtype": "markdown",
                   "markdown": {"title": title, "text": f"### {title}\n\n{markdown or text}"}}
        resp = self.session.post(url, json=payload, timeout=self.timeout)
        data = resp.json()
        if data.get("errcode") == 0:
            return True, "ok"
        return False, f"dingtalk errcode={data.get('errcode')} errmsg={data.get('errmsg')}"

    def _send_feishu(self, ch, title, text, markdown):
        payload = {"msg_type": "text", "content": {"text": f"{title}\n{text}"}}
        resp = self.session.post(ch["webhook"], json=payload, timeout=self.timeout)
        data = resp.json()
        if data.get("code") == 0 or data.get("StatusCode") == 0:
            return True, "ok"
        return False, f"feishu 返回 {json.dumps(data, ensure_ascii=False)[:200]}"

    def _send_bark(self, ch, title, text, markdown):
        server = str(ch.get("server") or "https://api.day.app").rstrip("/")
        url = f"{server}/{ch['key']}"
        resp = self.session.post(url, json={
            "title": title, "body": text,
            "group": ch.get("group") or "山西焦煤行情",
            "isArchive": 1,
        }, timeout=self.timeout)
        data = resp.json()
        if data.get("code") == 200:
            return True, "ok"
        return False, f"bark 返回 {json.dumps(data, ensure_ascii=False)[:200]}"

    def _send_email(self, ch, title, text, markdown):
        host = str(ch.get("smtp_host") or "").strip()
        port = int(ch.get("smtp_port") or 465)
        user = str(ch.get("user")).strip()
        password = str(ch.get("password")).strip()
        mail_from = str(ch.get("mail_from") or user).strip()
        recipients = [x.strip() for x in str(ch.get("mail_to", "")).replace(";", ",").split(",")
                      if x.strip()]
        if not recipients:
            return False, "mail_to 为空"

        msg = MIMEText(text, "plain", "utf-8")
        msg["Subject"] = Header(f"{ch.get('subject_prefix', '')} {title}".strip(), "utf-8")
        msg["From"] = mail_from
        msg["To"] = ",".join(recipients)

        if bool(ch.get("use_ssl", True)):
            server = smtplib.SMTP_SSL(host, port, timeout=self.timeout[1])
        else:
            server = smtplib.SMTP(host, port, timeout=self.timeout[1])
            server.starttls()
        try:
            server.login(user, password)
            server.sendmail(mail_from, recipients, msg.as_string())
        finally:
            try:
                server.quit()
            except Exception:  # noqa: BLE001
                pass
        return True, "ok"
