"""微信公众号「模板消息」—— 推给关注了公众号的指定微信用户。

适用：你有认证的**服务号**，想把消息发给某个具体微信用户（靠 openid 定位）。
配置：
    1. 公众号后台 → 设置与开发 → 基本配置 → 拿到 AppID / AppSecret
    2. 同一页把服务器出口 IP 加入「IP 白名单」（不加会报 40164）
    3. 功能 → 模板消息 → 从模板库选一个模板，拿到 template_id
    4. 用户需先关注该公众号，才能拿到 ta 的 openid（可用网页授权或后台用户列表）

⚠️ 现实约束（必须知道，否则会白折腾）：
  - **个人订阅号没有模板消息接口权限。** 需要认证服务号（企业主体，300 元/年认证）。
  - 2020 年后微信收紧了这个接口，新注册的号很可能找不到"模板消息"入口，
    官方主推的是「订阅消息」（一次性订阅），需要用户每次主动授权。
  - 模板字段有长度限制，且整条消息有大小上限，长文本必须压缩。

所以：如果你只是"想把消息发到自己微信"，请优先用 pushplus / serverchan 通道，
它们本质也是走公众号，但已经替你完成了认证和模板配置。

配置示例：
    - type: wechat_mp
      enabled: true
      appid: "${MP_APPID}"
      secret: "${MP_SECRET}"
      openid: "${MP_OPENID}"
      template_id: "abcdefg..."
      url: "https://example.com/detail"      # 点击消息跳转的链接，可空
      # 模板字段映射：{title} {text} {date} {summary} 会被替换
      data_fields:
        first:    "{title}"
        keyword1: "{date}"
        keyword2: "{summary}"
        remark:   "{text}"
      field_limit: 200       # 每个字段截断到多少字符
"""

from __future__ import annotations

from datetime import datetime

from ..errors import PermanentError, RateLimitError, TokenExpiredError, RetryableError
from ..text_utils import mask_secret, markdown_to_plain, truncate_bytes
from .base import Channel

_TOKEN_CODES = {40001, 42001, 40014}

_CODE_MESSAGES = {
    40013: "AppID 无效",
    40125: "AppSecret 无效",
    40164: "调用方 IP 不在公众号的 IP 白名单内，请到「基本配置」里添加服务器出口 IP",
    40037: "template_id 无效，请核对模板 ID",
    41028: "模板消息表单格式错误（form_id 无效）",
    41029: "模板消息表单已失效",
    43004: "该用户未关注此公众号，无法发送模板消息",
    40003: "openid 无效或不属于该公众号",
    45009: "接口调用超过限制（默认 10 万次/天，单用户 10 次/小时）",
    43101: "用户已拒收该公众号的消息",
}


class WechatMpChannel(Channel):
    type_name = "wechat_mp"
    aliases = ("mp", "wechat_mp_template", "gzh")
    display_name = "微信公众号模板消息"
    supports_markdown = False          # 模板字段只能塞纯文本
    text_limit = 6000
    min_interval = 0.5
    embed_title = False          # 标题走模板字段 first，不再拼进正文
    required_fields = ("appid", "secret", "openid", "template_id")

    # --------------------------------------------------------------- token

    @property
    def _token_key(self) -> str:
        return f"wechat_mp:{self.field('appid')}"

    def _fetch_token(self) -> tuple[str, float]:
        url = ("https://api.weixin.qq.com/cgi-bin/token"
               f"?grant_type=client_credential&appid={self.field('appid')}"
               f"&secret={self.field('secret')}")
        self.log.info("重新获取公众号 access_token (appid=%s, secret=%s)",
                      self.field("appid"), mask_secret(self.field("secret")))
        data = self._json(self.request("GET", url))
        if "access_token" not in data:
            code, errmsg = data.get("errcode"), data.get("errmsg") or ""
            if code == 40164:
                # 实测报文里会带具体 IP，原样透出，省得用户去猜
                raise PermanentError(f"{_CODE_MESSAGES[40164]}（{errmsg}）", code=code, raw=data)
            if code in _CODE_MESSAGES:
                raise PermanentError(_CODE_MESSAGES[code], code=code, raw=data)
            if code == 45009:
                raise RateLimitError("公众号接口调用超过限制", code=code, raw=data, retry_after=60)
            raise RetryableError(f"获取公众号 access_token 失败 errcode={code} {errmsg}",
                                 code=code, raw=data)
        return str(data["access_token"]), float(data.get("expires_in") or 7200)

    def _token(self) -> str:
        if self.dry_run and not self.verifying:
            return "dry-run-token"
        if self.tokens is None:
            return self._fetch_token()[0]
        return self.tokens.get(self._token_key, 7200.0, self._fetch_token)

    def invalidate_token(self) -> None:
        if self.tokens is not None:
            self.tokens.invalidate(self._token_key)

    # --------------------------------------------------------------- 发送

    def _build_data(self, title: str, content: str) -> dict:
        plain = markdown_to_plain(content)
        head = plain.split("\n", 1)[0] if plain else ""
        field_limit = int(self.raw.get("field_limit") or 200)
        variables = {
            "title": title or "",
            "text": plain,
            "summary": truncate_bytes(plain, field_limit * 3, "…").split("\n")[0],
            "head": head,
            "date": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }

        mapping = self.raw.get("data_fields")
        if not mapping:
            # 没配映射就退化成"只有 remark"的最简模板，兼容性最好
            mapping = {"first": "{title}", "remark": "{text}"}

        out: dict = {}
        for key, tpl in dict(mapping).items():
            try:
                value = str(tpl).format(**variables)
            except (KeyError, IndexError, ValueError):
                # 模板里混进了非占位符的花括号，原样使用，不因为格式问题整个失败
                value = str(tpl)
            out[str(key)] = {"value": truncate_bytes(value, field_limit, "…")}
        return out

    def deliver(self, content: str, *, chunk_index: int, total_chunks: int) -> None:
        title = str(self.raw.get("_last_title") or "")
        payload = {
            "touser": str(self.field("openid")),
            "template_id": str(self.field("template_id")),
            "data": self._build_data(title, content),
        }
        url_link = str(self.raw.get("url") or "").strip()
        if url_link:
            payload["url"] = url_link
        mini = self.raw.get("miniprogram")
        if isinstance(mini, dict) and mini.get("appid") and mini.get("pagepath"):
            payload["miniprogram"] = {"appid": mini["appid"], "pagepath": mini["pagepath"]}

        url = f"https://api.weixin.qq.com/cgi-bin/message/template/send?access_token={self._token()}"
        data = self._post_json(url, payload)
        code = data.get("errcode")
        if code == 0:
            return

        errmsg = data.get("errmsg") or ""
        if code in _TOKEN_CODES:
            raise TokenExpiredError(f"access_token 失效({code}) {errmsg}", code=code, raw=data)
        if code == 45009:
            raise RateLimitError("公众号接口调用超过限制", code=code, raw=data, retry_after=60)
        if code in _CODE_MESSAGES:
            raise PermanentError(_CODE_MESSAGES[code], code=code, raw=data)
        raise RetryableError(f"公众号返回 errcode={code} errmsg={errmsg}", code=code, raw=data)

    # --------------------------------------------------------------- 覆写

    def send(self, title: str, text: str | None = None, markdown: str | None = None):
        # 模板消息的标题要单独塞进 first 字段，不能拼在正文里
        self.raw["_last_title"] = title or ""
        return super().send(title, text, markdown)

    def check(self) -> str:
        tok, expires = self._fetch_token()
        return f"凭证可用（access_token 已取到，有效期 {int(expires)} 秒）"
