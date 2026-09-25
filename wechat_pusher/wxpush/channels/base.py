"""通道基类。

所有通道共享同一套"编排逻辑"，子类只需实现 deliver() 那一次 HTTP 调用：

    compose()   把 title / text / markdown 合成最终内容
    split_by_bytes()  按字节上限切片（超长消息自动分片续发）
    call_with_retry() 分片级独立重试，某一片失败不会重发前面已成功的片
    on_retry()  钩子：token 失效就刷新、代理报错就切直连

这样做的直接好处：新增一个平台只需要写 30 行 deliver()，重试/分片/限流
这些容易写错的地方全部只有一份实现。
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any

import requests

from ..errors import ConfigError, DryRunStop, TokenExpiredError, is_retryable
from ..logging_setup import get_logger
from ..retry import RetryPolicy, call_with_retry
from ..text_utils import mask_secret, one_line, split_by_bytes, utf8_len


@dataclass
class SendResult:
    channel: str
    ok: bool = False
    detail: str = ""
    chunks: int = 0
    attempts: int = 0
    skipped: bool = False
    error_type: str = ""
    #: 失败是否属于"重试有意义"。上层据此决定是重跑整轮任务还是立刻喊人。
    retryable: bool = False
    elapsed: float = 0.0

    def to_dict(self) -> dict:
        return {
            "channel": self.channel, "ok": self.ok, "detail": self.detail,
            "chunks": self.chunks, "attempts": self.attempts,
            "skipped": self.skipped, "error_type": self.error_type,
            "retryable": self.retryable,
            "elapsed": round(self.elapsed, 2),
        }


@dataclass
class ChannelContext:
    """通道运行期共享的东西。"""
    session: requests.Session
    policy: RetryPolicy
    tokens: Any = None                 # TokenStore，只有需要 token 的通道用
    network: dict = field(default_factory=dict)
    dry_run: bool = False


class Channel:
    #: 配置里 type 字段的取值
    type_name: str = ""
    #: 兼容的别名（老配置里可能写的是别的名字）
    aliases: tuple[str, ...] = ()
    #: 人类可读的名称，仅用于日志与文档
    display_name: str = ""
    #: 是否支持 markdown 格式
    supports_markdown: bool = False
    #: 纯文本内容上限（**字节**）
    text_limit: int = 2000
    #: markdown 内容上限（**字节**）
    markdown_limit: int = 4000
    #: 是否把标题拼进正文。
    #: 企业微信这类"消息内容即全部"的通道要拼（否则正文没有抬头）；
    #: PushPlus / Server酱 / 公众号模板都有**独立的 title 字段**，再拼一次
    #: 就是同一句话显示两遍，所以它们设为 False。可在配置里用 embed_title 覆盖。
    embed_title: bool = True
    #: 两次调用之间的最小间隔（秒），用于规避平台限流
    min_interval: float = 0.0
    #: 必填字段
    required_fields: tuple[str, ...] = ()

    def __init__(self, raw: dict, ctx: ChannelContext):
        self.raw = dict(raw or {})
        self.ctx = ctx
        self.log = get_logger(self.type_name)
        self.session = ctx.session
        self.policy = ctx.policy
        self.tokens = ctx.tokens
        self.network = ctx.network or {}
        self.dry_run = bool(ctx.dry_run)
        #: check() 自检期间为 True —— 自检的意义就是"真去验一次凭证"，
        #: 所以即使整体是 dry-run，自检也必须放行真实网络请求。
        self.verifying = False
        self._last_call = 0.0
        self.validate()
        # 允许在配置里覆盖"标题是否拼进正文"
        if "embed_title" in self.raw:
            self.embed_title = bool(self.raw["embed_title"])

    # ------------------------------------------------------------------ 配置

    @property
    def name(self) -> str:
        """用户给这个通道起的名字，便于多通道同类型时区分。"""
        return str(self.raw.get("name") or self.type_name)

    def field(self, key: str, default=None, *, required: bool = False):
        val = self.raw.get(key, default)
        if isinstance(val, str):
            val = val.strip()
        if required and (val is None or val == ""):
            raise ConfigError(f"通道 {self.name} 缺少必要配置项: {key}")
        return val

    def validate(self) -> None:
        missing = [k for k in self.required_fields
                   if not str(self.raw.get(k, "") or "").strip()]
        if missing:
            raise ConfigError(f"通道 {self.raw.get('type')} 缺少必要配置项: {', '.join(missing)}")

    def describe(self) -> str:
        return f"{self.name}({self.type_name})"

    # ------------------------------------------------------------------ HTTP

    def request(self, method: str, url: str, **kw) -> requests.Response:
        """统一出网入口：超时、UA、限流间隔都在这里兜住。"""
        if self.min_interval > 0:
            wait = self.min_interval - (time.time() - self._last_call)
            if wait > 0:
                self.log.debug("等待 %.1fs 以规避 %s 限流", wait, self.type_name)
                time.sleep(wait)
        self._last_call = time.time()

        kw.setdefault("timeout", (float(self.network.get("timeout_connect", 6)),
                                  float(self.network.get("timeout_read", 15))))
        proxies = self.network.get("proxies")
        if proxies is not None:
            kw.setdefault("proxies", proxies)
        resp = self.session.request(method, url, **kw)
        return resp

    def _post_json(self, url: str, payload: dict,
                   headers: dict | None = None) -> dict:
        if self.dry_run and not self.verifying:
            size = len(json.dumps(payload, ensure_ascii=False).encode("utf-8"))
            self._dry_run_stop(url, size)
        resp = self.request("POST", url, json=payload, headers=headers)
        return self._json(resp)

    def _dry_run_stop(self, url: str, payload_bytes: int) -> None:
        """dry-run 的统一短路点：记一条日志，然后抛 DryRunStop。

        所有"发请求"的入口都应该走这里（JSON 用 _post_json；表单类通道
        如 Server 酱需要自己调用一次），这样 dry-run 的行为只有一处定义。
        """
        self.log.info("[dry-run] 不发起请求 %s（payload %d 字节）",
                      _short(url), payload_bytes)
        raise DryRunStop("dry-run：未发起真实请求")

    @staticmethod
    def _json(resp: requests.Response) -> dict:
        """解析响应体。

        关键：**绝不能用 HTTP 状态码判断业务成败**。实测企业微信在凭证错误、
        webhook 无效、token 失效时全部返回 HTTP 200，错误信息在 body 里。
        Server 酱则相反，失败时返回 HTTP 400 但 body 是结构化 JSON。
        所以两边都要看，且以 body 为准。
        """
        text = resp.text or ""
        try:
            data = resp.json()
        except ValueError:
            raise RuntimeError(
                f"响应不是 JSON (http={resp.status_code}): {one_line(text, 160)}")
        if not isinstance(data, dict):
            raise RuntimeError(f"响应格式异常 (http={resp.status_code}): {one_line(text, 160)}")
        data.setdefault("_http_status", resp.status_code)
        return data

    # ------------------------------------------------------------------ 重试钩子

    def on_retry(self, exc: Exception, attempt: int) -> None:
        """重试前的副作用准备。子类可覆盖（记得调 super()）。"""
        if isinstance(exc, TokenExpiredError):
            self.invalidate_token()
            self.log.warning("access_token 已失效，已作废缓存，重试时将重新获取")

        # 实测踩过：本机配了 HTTP 代理，代理连不上某些域名（EOF），但直连正常。
        # 遇到代理层错误就切直连重试一次，而不是傻等退避。
        if self.network.get("auto_disable_proxy", True) and \
                isinstance(exc, requests.exceptions.ProxyError) and self.session.trust_env:
            self.session.trust_env = False
            self.log.warning("检测到代理不可用，已切换为直连重试")

    def invalidate_token(self) -> None:
        """需要 token 的通道覆盖此方法。"""
        return None

    # ------------------------------------------------------------------ 主流程

    def compose(self, title: str, text: str | None, markdown: str | None) -> tuple[str, bool]:
        """合成最终内容，返回 (content, 是否为 markdown)。"""
        body = (markdown if (markdown and self.supports_markdown) else text) or ""
        if markdown and not self.supports_markdown and not text:
            from ..text_utils import markdown_to_plain
            body = markdown_to_plain(markdown)
        title = (title or "").strip()
        if title and body and self.embed_title:
            content = f"{title}\n{body}"
        else:
            content = body or title
        return content.strip(), bool(markdown and self.supports_markdown)

    def send(self, title: str, text: str | None = None,
             markdown: str | None = None) -> SendResult:
        started = time.time()
        content, is_md = self.compose(title, text, markdown)

        if not content.strip():
            return SendResult(channel=self.name, ok=False, skipped=True,
                              detail="消息内容为空，未发送")

        limit = self.markdown_limit if is_md else self.text_limit
        try:
            chunks = split_by_bytes(content, limit)
        except Exception as exc:  # noqa: BLE001
            return SendResult(channel=self.name, ok=False,
                              detail=f"内容分片失败: {exc}",
                              error_type=type(exc).__name__)

        total = len(chunks)
        if total > 1:
            self.log.info("内容 %d 字节超过 %s 上限 %d 字节，已分为 %d 片续发",
                          utf8_len(content), self.type_name, limit, total)

        attempts_used = 0
        dry_run_skipped = False
        for idx, chunk in enumerate(chunks):
            try:
                _, used = call_with_retry(
                    lambda c=chunk, i=idx: self.deliver(c, chunk_index=i, total_chunks=total),
                    self.policy, on_retry=self.on_retry)
                attempts_used += used
            except DryRunStop:
                # dry-run：这一片已在 _dry_run_stop() 里被拦下并记了日志，按成功处理。
                # 不计入 attempts（没有真实尝试），但要标记出来，避免把"没发"
                # 汇报成"发送成功"
                dry_run_skipped = True
                continue
            except Exception as exc:  # noqa: BLE001 - 单通道失败绝不能影响其他通道
                # call_with_retry 会把实际尝试次数挂到异常上；拿不到就按"至少试过 1 次"算，
                # 不能让汇总结果出现 attempts=0（会被误读成"根本没发出去"）
                attempts_used += int(getattr(exc, "attempts", 1) or 1)
                self.log.error("发送失败 通道=%s 第 %d/%d 片 已尝试 %d 次 错误=%s",
                               self.name, idx + 1, total, attempts_used, exc)
                return SendResult(
                    channel=self.name, ok=False, chunks=total, attempts=attempts_used,
                    detail=f"第 {idx + 1}/{total} 片失败: {exc}",
                    error_type=type(exc).__name__, retryable=is_retryable(exc),
                    elapsed=time.time() - started)

        if dry_run_skipped:
            detail = "dry-run（未真实发送）" if total == 1 else f"dry-run（{total} 片）"
            self.log.info("dry-run 完成 通道=%s %s", self.name, detail)
            return SendResult(channel=self.name, ok=True, chunks=total,
                              attempts=attempts_used, detail=detail,
                              elapsed=time.time() - started)

        detail = "ok" if total == 1 else f"ok（分 {total} 片续发）"
        self.log.info("发送成功 通道=%s %s", self.name, detail)
        return SendResult(channel=self.name, ok=True, chunks=total,
                          attempts=attempts_used, detail=detail,
                          elapsed=time.time() - started)

    # ------------------------------------------------------------------ 子类实现

    def deliver(self, content: str, *, chunk_index: int, total_chunks: int) -> None:
        """真正发一次。成功即正常返回，失败必须抛 PushError 子类。"""
        raise NotImplementedError

    # ------------------------------------------------------------------ 自检

    def check(self) -> str:
        """配置自检。默认只检查"配全了没有"，不发起网络请求。

        需要凭证的通道应覆盖此方法，真实去取一次 access_token —— 这能在
        计划任务真正开跑之前，把"corpid 抄错了"这类问题暴露出来。
        """
        return "配置完整（未校验凭证有效性）"

    def run_check(self) -> str:
        """check() 的外壳：打开 verifying 开关，让 dry-run 也放行真实请求。"""
        prev, self.verifying = self.verifying, True
        try:
            return self.check()
        finally:
            self.verifying = prev


def _short(url: str, keep: int = 64) -> str:
    """日志里的 URL 可能带 key，截断并遮掉敏感部分。"""
    if "key=" in url:
        head, _, tail = url.partition("key=")
        return head + "key=" + mask_secret(tail, 4, 4)
    if "access_token=" in url:
        head, _, tail = url.partition("access_token=")
        return head + "access_token=" + mask_secret(tail, 4, 4)
    return url if len(url) <= keep else url[:keep] + "..."
