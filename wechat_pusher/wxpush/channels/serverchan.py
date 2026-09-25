"""Server 酱 Turbo（ServerChan）—— 同样是借道公众号推送到个人微信。

配置：https://sct.ftqq.com 微信扫码登录 → SendKey → 复制

实测确认的两个返回（注意：**失败时 HTTP 状态码是 400，body 才是结构化 JSON**，
所以判断成败必须解析 body，不能只看 resp.ok）：
    HTTP 400 {"message":"[AUTH]错误的Key","code":40001,...,"scode":461}
    HTTP 400 {"message":"[INPUT]title 不能为空","code":20001,...,"scode":460}
    成功时 HTTP 200 {"code":0,"message":"","data":{"pushid":"...","readkey":"...","error":"SUCCESS","errno":0}}
"""

from __future__ import annotations

from ..errors import PermanentError, RateLimitError, RetryableError
from ..text_utils import mask_secret
from .base import Channel

# 实测 + 官方文档
_CODE_MESSAGES = {
    40001: "错误的 SendKey，请到 sct.ftqq.com 重新复制",
    40002: "请求参数格式错误",
    20001: "参数错误：title 不能为空",
    20002: "参数错误：desp 超长（正文上限约 32KB）",
    40000: "推送失败，通道返回异常",
}

# Server 酱 Turbo 对单个 SendKey 有频率限制，保守取 12 秒间隔（5 条/分钟）
_PERMANENT_CODES = {40001, 40002, 20001, 20002}
_RATE_CODES = {429, 40003, 40004}


class ServerchanChannel(Channel):
    type_name = "serverchan"
    aliases = ("server_chan", "sct", "ftqq", "serverchanturbo")
    display_name = "Server 酱 Turbo（个人微信）"
    supports_markdown = True
    # 官方正文上限约 32KB，但手机阅读体验和接口稳定性考虑，取 3500 字节分片
    text_limit = 3500
    markdown_limit = 3500
    min_interval = 12.0
    embed_title = False          # 有独立的 title 字段，正文里不再重复
    required_fields = ("sendkey",)

    @property
    def _url(self) -> str:
        base = str(self.raw.get("url") or "https://sctapi.ftqq.com").rstrip("/")
        return f"{base}/{self.field('sendkey')}.send"

    def deliver(self, content: str, *, chunk_index: int, total_chunks: int) -> None:
        form = {
            "title": str(self.raw.get("_last_title") or "消息推送"),
            "desp": content,
        }
        tags = self.raw.get("tags")
        if tags:
            form["tags"] = (",".join(str(t) for t in tags)
                            if isinstance(tags, (list, tuple)) else str(tags))
        if self.raw.get("short"):
            form["short"] = str(self.raw["short"])

        params = {}
        if self.raw.get("channel"):
            # 9=服务号模板消息(默认) 3=方糖服务号 ... 见官方文档
            params["channel"] = self.raw["channel"]

        if self.dry_run and not self.verifying:
            # Server 酱发的是表单不是 JSON，走不了 _post_json，所以自己调一次
            # 统一的 dry-run 短路点，保证行为与其它通道一致
            self._dry_run_stop(self._url, len(content.encode("utf-8")))

        resp = self.request("POST", self._url, data=form, params=params or None)
        data = self._json(resp)
        code = data.get("code")

        try:
            code_int = int(code)
        except (TypeError, ValueError):
            code_int = None

        # 注意：code 可能是 0(成功) / 既有数字也有字符串形式
        if code_int == 0:
            inner = (data.get("data") or {}) if isinstance(data.get("data"), dict) else {}
            # 外层 code=0 只代表"请求被受理"，真正的推送结果在 data.error 里
            if str(inner.get("error", "SUCCESS")).upper() not in ("SUCCESS", "", "NONE"):
                raise RetryableError(
                    f"Server 酱受理成功但推送失败: {inner.get('error')}",
                    code=code, raw=data)
            return

        msg = data.get("message") or data.get("info") or ""
        if code_int in _PERMANENT_CODES:
            raise PermanentError(_CODE_MESSAGES.get(code_int, str(msg)),
                                 code=code, raw=data)
        if code_int in _RATE_CODES or resp.status_code == 429:
            raise RateLimitError(f"Server 酱限流: {msg}", code=code, raw=data, retry_after=60)
        raise RetryableError(
            f"Server 酱返回 code={code} message={msg} (http={resp.status_code})",
            code=code, raw=data)

    def send(self, title: str, text: str | None = None, markdown: str | None = None):
        self.raw["_last_title"] = title or ""
        return super().send(title, text, markdown)

    def describe(self) -> str:
        return (f"{self.name}(serverchan, sendkey="
                f"{mask_secret(self.raw.get('sendkey'))})")
