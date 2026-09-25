"""Pusher —— 统一发送入口。

职责边界：Pusher 负责"发给谁、发几个、失败了怎么办"，
通道负责"这一次 HTTP 怎么发"。两者互不知道对方的实现细节。

三种分发模式（配置里的 mode，也可以在调用时临时指定）：

  all         （默认）所有启用的通道都发一遍。任何一个成功即整体成功。
              优点：多通道冗余，微信通道挂了邮件还能到。
              代价：额度消耗快。

  failover    按配置顺序发，第一个成功就停。
              优点：省额度、快。适合"主通道通常可靠，备通道只是保险"。

  require_all 必须全部成功，否则整体判定失败并告警。
              适合"通知必须留痕"的场景（审计、告警上报）。
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from datetime import datetime

import requests

from .channels import Channel, ChannelContext, SendResult, build_channel, list_channels
from .errors import ConfigError, PushError
from .logging_setup import get_logger
from .retry import RetryPolicy
from .settings import env_hint, load_config
from .text_utils import one_line
from .token_store import TokenStore

MODES = ("all", "failover", "require_all")


@dataclass
class PushReport:
    """一次推送的完整结果。调用方看 any_ok 就够了，排查问题看 results。"""
    title: str
    results: list[SendResult] = field(default_factory=list)
    deduped: bool = False
    mode: str = "all"
    started_at: str = ""
    elapsed: float = 0.0

    @property
    def any_ok(self) -> bool:
        return any(r.ok for r in self.results)

    @property
    def all_ok(self) -> bool:
        return bool(self.results) and all(r.ok for r in self.results)

    @property
    def failed(self) -> list[SendResult]:
        return [r for r in self.results if not r.ok]

    @property
    def should_retry(self) -> bool:
        """是否值得把整轮任务重跑一次（存在可重试的失败，且没有通道成功）。

        用途：外层调度器据此决定"过 5 分钟再跑一次"还是"直接发告警找人"。
        永久性错误（凭证错、参数错）重跑一万次也一样，不该浪费重试。
        """
        return bool(self.results) and not self.any_ok and \
            all(r.retryable for r in self.results if not r.skipped)

    @property
    def needs_human(self) -> bool:
        """是否需要人工介入：一个通道都没成功，且失败不是"等等就好"的类型。"""
        return bool(self.results) and not self.any_ok and not self.should_retry

    @property
    def summary(self) -> str:
        if self.deduped:
            return "已跳过（命中发送去重）"
        if not self.results:
            return "没有启用任何通道"
        ok = [r.channel for r in self.results if r.ok]
        bad = [f"{r.channel}({r.detail})" for r in self.results if not r.ok]
        parts = []
        if ok:
            parts.append("成功: " + ", ".join(ok))
        if bad:
            parts.append("失败: " + "; ".join(bad))
        return f"（{self.mode}）" + " | ".join(parts)

    def to_dict(self) -> dict:
        return {
            "title": self.title,
            "mode": self.mode,
            "any_ok": self.any_ok,
            "all_ok": self.all_ok,
            "deduped": self.deduped,
            "summary": self.summary,
            "started_at": self.started_at,
            "elapsed": round(self.elapsed, 2),
            "results": [r.to_dict() for r in self.results],
        }

    def __str__(self) -> str:
        return self.summary


class _DedupStore:
    """发送去重：防止"外层任务被重跑"导致同一条消息推了两遍。

    场景很常见：计划任务因为上次超时被重试、或者调度器崩溃重启补跑，
    消息本身没错，但重复推送会让人怀疑数据出问题。
    """

    def __init__(self, path: str, ttl: float):
        self.path = path
        self.ttl = float(ttl)
        self._cache: dict[str, float] = {}
        self._loaded = False

    def _load(self) -> None:
        if self._loaded:
            return
        self._loaded = True
        if not self.path:
            return
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                self._cache = {str(k): float(v) for k, v in data.items()}
        except Exception:  # noqa: BLE001
            self._cache = {}

    def _persist(self) -> None:
        if not self.path:
            return
        try:
            d = os.path.dirname(os.path.abspath(self.path))
            if d:
                os.makedirs(d, exist_ok=True)
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self._cache, f, ensure_ascii=False)
            os.replace(tmp, self.path)
        except Exception:  # noqa: BLE001
            pass

    def seen(self, key: str) -> bool:
        self._load()
        now = time.time()
        # 顺手清理过期项，避免文件无限膨胀
        expired = [k for k, v in self._cache.items() if now - v > self.ttl]
        for k in expired:
            self._cache.pop(k, None)
        if expired:
            self._persist()
        return key in self._cache

    def mark(self, key: str) -> None:
        self._load()
        self._cache[key] = time.time()
        self._persist()


class Pusher:
    """微信推送器。"""

    def __init__(self, config: dict | None = None, *, config_path: str | None = None,
                 logger=None, dry_run: bool = False):
        self.cfg = config if config is not None else load_config(config_path)
        self.log = logger or get_logger("pusher")
        self.dry_run = bool(dry_run)

        net = self.cfg.get("network", {}) or {}
        retry = self.cfg.get("retry", {}) or {}

        # 相对路径一律相对**配置文件所在目录**解析。
        # 不这样做的话，同一个配置从不同 cwd 调用（计划任务、CI、双击运行）
        # 会把 token 缓存和去重文件写到不同地方，表现为"token 反复失效""去重忽然不生效"。
        cfg_path = self.cfg.get("_config_path")
        self.base_dir = (os.path.dirname(os.path.abspath(cfg_path))
                         if cfg_path else os.getcwd())

        self.policy = RetryPolicy(
            attempts=int(retry.get("attempts", 4)),
            base=float(retry.get("base", 1.0)),
            cap=float(retry.get("cap", 30.0)),
            jitter=float(retry.get("jitter", 0.35)),
            retry_after_cap=float(retry.get("retry_after_cap", 120.0)),
        )

        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": net.get("user_agent") or "wxpush/1.0",
            "Accept": "application/json, text/plain, */*",
        })

        cache = self.cfg.get("token_cache", {}) or {}
        self.tokens = TokenStore(
            _resolve_path(cache.get("path"), self.base_dir),
            safety_margin=float(cache.get("safety_margin", 300)),
        )

        dedup = self.cfg.get("dedup", {}) or {}
        self._dedup_enabled = bool(dedup.get("enabled"))
        self._dedup_ttl = float(dedup.get("ttl_seconds", 1800))
        self._dedup_path = _resolve_path(dedup.get("path"), self.base_dir)

        self._ctx = ChannelContext(
            session=self.session, policy=self.policy, tokens=self.tokens,
            network=net, dry_run=self.dry_run,
        )
        # 引用了 ${VAR} 但当前进程读不到时，把变量名回显到所有相关报错里。
        # 只统计**已启用**通道路径下的占位符：未启用通道引用到的变量不该让人去配。
        enabled_prefixes = tuple(
            f"channels[{i}]"
            for i, raw in enumerate(self.cfg.get("channels") or [])
            if isinstance(raw, dict) and raw.get("enabled"))
        self._env_hint = env_hint(
            missing_paths=self.cfg.get("_missing_env_paths") or [],
            only_paths_prefixes=enabled_prefixes)
        self.channels: list[Channel] = []
        self._build_channels()

    # ------------------------------------------------------------------ 构建

    def _build_channels(self) -> None:
        raw_list = self.cfg.get("channels") or []
        if not isinstance(raw_list, list):
            raise ConfigError("channels 必须是列表")

        for raw in raw_list:
            if not raw or not raw.get("enabled"):
                continue
            try:
                self.channels.append(build_channel(raw, self._ctx))
            except PushError as exc:
                # 单个通道配错不应让整个程序起不来，记下来，发送时如实报告
                hint = f" {self._env_hint}" if self._env_hint else ""
                self.log.error("通道构建失败（已跳过）: %s%s", exc, hint)
                self.channels.append(
                    _BrokenChannel(raw, self._ctx, exc, hint=self._env_hint))

    @classmethod
    def from_config_file(cls, path: str | None = None, **kw) -> "Pusher":
        return cls(config=load_config(path), **kw)

    def channel_names(self) -> list[str]:
        return [c.name for c in self.channels]

    def describe(self) -> list[dict]:
        out = []
        for c in self.channels:
            try:
                desc = c.describe()
            except Exception:  # noqa: BLE001
                desc = c.name
            out.append({"name": c.name, "type": c.type_name, "desc": desc})
        return out

    # ------------------------------------------------------------------ 自检

    def check(self) -> list[dict]:
        """不发送消息，只校验每个通道的配置（能校验凭证的就去真实校验凭证）。"""
        report = []
        for ch in self.channels:
            try:
                detail = ch.run_check()
                report.append({"channel": ch.name, "type": ch.type_name,
                               "ok": True, "detail": detail})
            except Exception as exc:  # noqa: BLE001
                detail = f"{type(exc).__name__}: {exc}"
                if self._env_hint:
                    detail = f"{detail}。{self._env_hint}"
                report.append({"channel": ch.name, "type": ch.type_name,
                               "ok": False, "detail": detail})
        if not self.channels:
            detail = "没有任何启用的通道（channels 里 enabled 全是 false？）"
            if self._env_hint:
                detail = f"{detail}。{self._env_hint}"
            report.append({"channel": "-", "type": "-", "ok": False,
                           "detail": detail})
        return report

    # ------------------------------------------------------------------ 发送

    def send(self, title: str, text: str | None = None, markdown: str | None = None,
             *, only: list[str] | str | None = None,
             skip: list[str] | str | None = None,
             mode: str | None = None,
             dedup_key: str | None = None,
             dedup_ttl: float | None = None) -> PushReport:
        """发一条消息。

        only   只发给这些通道（按 name 或 type 匹配），其他忽略
        skip   排除这些通道
        mode   覆盖配置里的分发模式
        dedup_key  去重键。传了它且开启去重时，ttl 内重复调用会被跳过
        """
        mode = (mode or self.cfg.get("mode") or "all").lower()
        if mode not in MODES:
            raise ConfigError(f"mode 只能是 {', '.join(MODES)}，收到: {mode}")

        started = time.time()
        report = PushReport(title=title or "", mode=mode,
                            started_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"))

        if dedup_key and self._dedup_enabled:
            store = _DedupStore(self._dedup_path, dedup_ttl or self._dedup_ttl)
            if store.seen(dedup_key):
                self.log.info("命中发送去重，跳过本次推送: %s", dedup_key)
                report.deduped = True
                report.elapsed = time.time() - started
                return report

        targets = self._select(only, skip)
        if not targets:
            self.log.warning("没有匹配到任何可用通道，本次未发送")
            report.elapsed = time.time() - started
            return report

        self.log.info("开始推送「%s」-> %d 个通道: %s",
                      one_line(title, 40), len(targets),
                      ", ".join(c.name for c in targets))

        for ch in targets:
            try:
                res = ch.send(title, text, markdown)
            except Exception as exc:  # noqa: BLE001 - 兜底：通道实现有 bug 也不能崩
                self.log.exception("通道 %s 抛出未预期异常", ch.name)
                res = SendResult(channel=ch.name, ok=False,
                                 detail=f"未预期异常 {type(exc).__name__}: {exc}",
                                 error_type=type(exc).__name__, retryable=False)

            report.results.append(res)

            if res.ok:
                self.log.info("  [OK]   %s  %s", res.channel, res.detail)
            elif res.skipped:
                self.log.warning("  [SKIP] %s  %s", res.channel, res.detail)
            else:
                self.log.error("  [FAIL] %s  %s", res.channel, res.detail)

            if mode == "failover" and res.ok:
                self.log.info("failover 模式下已成功，停止后续通道")
                break

        if dedup_key and self._dedup_enabled and report.any_ok:
            _DedupStore(self._dedup_path, dedup_ttl or self._dedup_ttl).mark(dedup_key)

        report.elapsed = time.time() - started
        self.log.info("推送结束，耗时 %.2fs -> %s", report.elapsed, report.summary)
        return report

    def _select(self, only, skip) -> list[Channel]:
        def norm(v):
            if v is None:
                return None
            if isinstance(v, str):
                v = [x.strip() for x in v.replace(";", ",").split(",") if x.strip()]
            return {str(x).lower() for x in v}

        only_set, skip_set = norm(only), norm(skip)
        out = []
        for ch in self.channels:
            keys = {ch.name.lower(), ch.type_name.lower()}
            if only_set and not (keys & only_set):
                continue
            if skip_set and (keys & skip_set):
                continue
            out.append(ch)
        return out

    def close(self) -> None:
        try:
            self.session.close()
        except Exception:  # noqa: BLE001
            pass

    def __enter__(self) -> "Pusher":
        return self

    def __exit__(self, *exc) -> None:
        self.close()


class _BrokenChannel(Channel):
    """配置有问题的通道占位符：构建时不抛异常，发送时如实报告失败原因。"""

    type_name = "_broken"

    def __init__(self, raw: dict, ctx: ChannelContext, error: Exception,
                 hint: str = ""):
        self.raw = dict(raw or {})
        self.ctx = ctx
        self.log = get_logger("broken")
        self.session = ctx.session
        self.policy = ctx.policy
        self.tokens = ctx.tokens
        self.network = ctx.network or {}
        self.dry_run = bool(ctx.dry_run)
        self._error = error
        self._hint = hint or ""
        self.type_name = str(self.raw.get("type") or "_broken")
        self.verifying = False

    @property
    def name(self) -> str:
        return str(self.raw.get("name") or self.raw.get("type") or "未命名通道")

    def send(self, title: str, text: str | None = None, markdown: str | None = None) -> SendResult:
        detail = f"配置错误: {self._error}"
        if self._hint:
            detail = f"{detail}。{self._hint}"
        return SendResult(channel=self.name, ok=False, detail=detail,
                          error_type=type(self._error).__name__)

    def check(self) -> str:
        # 必须抛出来。若在这里返回字符串，pusher.check() 会把它标成 [OK]，
        # 于是"通道类型写错了"这种致命配置问题会被自检放过。
        raise self._error


def _resolve_path(path: str | None, base_dir: str) -> str | None:
    """把配置里的相对路径解析成绝对路径。"""
    if not path:
        return None
    path = str(path)
    return path if os.path.isabs(path) else os.path.normpath(os.path.join(base_dir, path))


# ---------------------------------------------------------------------- 便捷函数

def send_text(title: str, text: str | None = None, markdown: str | None = None,
              *, config: dict | None = None, config_path: str | None = None,
              only=None, skip=None, mode: str | None = None,
              dedup_key: str | None = None, dedup_ttl: float | None = None,
              dry_run: bool = False) -> PushReport:
    """一行代码发消息。最常用的入口。

        from wxpush import send_text
        report = send_text("标题", "正文")
    """
    with Pusher(config=config, config_path=config_path, dry_run=dry_run) as p:
        return p.send(title, text, markdown, only=only, skip=skip, mode=mode,
                      dedup_key=dedup_key, dedup_ttl=dedup_ttl)
