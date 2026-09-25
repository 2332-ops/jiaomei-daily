"""PushPlus（推送加）—— 把消息送到**个人微信**，配置成本最低的一条路。

原理：你扫码关注它的公众号，它用公众号给你推。所以严格说这是"借道公众号"，
不是微信官方给你的 API —— 但对个人用户来说这是最省事的方案。

适用：
  - 只想推给自己 → 填 token 即可
  - 想推给多个微信号 → 用「群组」：在 PushPlus 后台建群组，把家人/同事拉进来，
    配置 topic（群组编码），一次推送全员收到
  - 想推给某个好友 → 用「一对一消息」的 to 字段（好友的 token）

配置：https://www.pushplus.plus 微信扫码登录 → 发送消息 → 复制 token

实测确认的错误返回（HTTP 200，成败看 body 的 code）：
    {"code":903,"data":"无效的用户token","msg":"用户令牌不正确"}
    {"code":905,"data":"未实名认证用户无法发送消息。实名地址：https://verify.pushplus.plus",
     "msg":"账户未进行实名认证"}

⚠️ 905 常被误读成"未绑定微信"，实际是"账户未实名认证"。PushPlus 已把实名认证设为
发消息的前置条件，token 完全正确时依然返回 905 —— 排查时不要再去反复核对 token。
"""

from __future__ import annotations

from ..errors import PermanentError, RateLimitError, RetryableError
from ..text_utils import mask_secret
from .base import Channel

# 200 成功；900/902/903/905 均为"账号或令牌不可用"，属永久错误
# （903、905 已用真实凭证实测确认；其余按官方 code 表归类）
_PERMANENT_CODES = {
    900: "用户不存在",
    902: "token 不存在",
    903: "用户令牌不正确，请到 PushPlus 后台重新复制 token",
    905: "账户未完成实名认证 —— PushPlus 不允许未实名账号发消息，"
         "去 https://verify.pushplus.plus 完成实名即可（与 token 无关）",
}
_RATE_CODES = {999: "系统繁忙，请稍后重试"}


class PushplusChannel(Channel):
    type_name = "pushplus"
    aliases = ("push_plus", "pushplus_plus")
    display_name = "PushPlus（个人微信）"
    supports_markdown = True
    # PushPlus 官方对 content 的限制说明较模糊，取 3500 字节保守值，
    # 超长自动分片，避免整条被拒
    text_limit = 3500
    markdown_limit = 3500
    min_interval = 1.2
    embed_title = False          # 有独立的 title 字段，正文里不再重复
    required_fields = ("token",)

    #: PushPlus 支持的模板格式
    TEMPLATES = ("html", "txt", "json", "markdown", "cloudMonitor", "jenkins", "route")

    def deliver(self, content: str, *, chunk_index: int, total_chunks: int) -> None:
        template = str(self.raw.get("template") or "markdown").lower()
        if template not in self.TEMPLATES:
            raise PermanentError(
                f"template 不支持: {template}，可选 {', '.join(self.TEMPLATES)}")

        payload = {
            "token": str(self.field("token")),
            "title": str(self.raw.get("_last_title") or self.raw.get("title") or "消息推送"),
            "content": content,
            "template": template,
        }
        # topic = 群组编码（一对多）；to = 好友 token（一对一）
        for key in ("topic", "to", "channel"):
            val = str(self.raw.get(key, "") or "").strip()
            if val:
                payload[key] = val
        if self.raw.get("webhook"):
            payload["webhook"] = str(self.raw["webhook"])
        if self.raw.get("callbackUrl"):
            payload["callbackUrl"] = str(self.raw["callbackUrl"])

        url = str(self.raw.get("url") or "https://www.pushplus.plus/send")
        data = self._post_json(url, payload)
        code = data.get("code")

        # 有的部署返回字符串 "200"，统一转成 int 比较
        try:
            code_int = int(code)
        except (TypeError, ValueError):
            code_int = None

        if code_int == 200:
            return

        msg = data.get("msg") or data.get("data") or ""
        if code_int in _PERMANENT_CODES:
            hint = _PERMANENT_CODES[code_int]
            # 平台的 msg 常是一句笼统的话（如"账户未进行实名认证"），真正能照着做的
            # 地址在 data 里（如"实名地址：https://..."）。丢掉它等于让用户自己去猜，
            # 所以只要 data 提供了我们文案里没有的内容，就一并带上。
            extra = data.get("data")
            if isinstance(extra, str) and extra.strip() and extra.strip() not in hint:
                hint = f"{hint}｜平台原文：{extra.strip()}"
            raise PermanentError(hint, code=code, raw=data)
        if code_int in _RATE_CODES or code_int in (429, 500, 502, 503):
            raise RateLimitError(str(msg) or "PushPlus 限流", code=code, raw=data, retry_after=30)
        raise RetryableError(f"PushPlus 返回 code={code} msg={msg}", code=code, raw=data)

    def send(self, title: str, text: str | None = None, markdown: str | None = None):
        self.raw["_last_title"] = title or ""
        return super().send(title, text, markdown)

    def describe(self) -> str:
        scope = "个人" if not self.raw.get("topic") else f"群组:{self.raw['topic']}"
        return f"{self.name}(pushplus/{scope}, token={mask_secret(self.raw.get('token'))})"
