"""QQ 机器人（QQ 开放平台官方 API）—— 推到 QQ 群 / QQ 单聊。

**这是 QQ 侧唯一的官方接口。** 第三方"QQ 机器人框架"（OneBot / NapCat / go-cqhttp /
LLOneBot 等）走的是逆向 QQ 客户端协议，违反平台用户协议、有封号风险，本模块不实现、
也不建议使用。想省事请用 PushPlus（推到个人微信），那是合规路径里成本最低的。

配置：
    - type: qq_bot
      enabled: true
      appid: "${QQ_BOT_APPID}"
      clientsecret: "${QQ_BOT_SECRET}"
      group_openid: "xxxx"      # 发到群（与 user_openid 二选一）
      # user_openid: "yyyy"     # 发到单聊
      sandbox: false            # 开发期先用沙箱环境，设 true
      msg_type: "text"          # text(0) | markdown(2)

## 三条硬限制（都是平台的，不是本模块的）

1. **主动消息要求机器人已上线。** 未上线的机器人在正式环境发主动消息会被拒：
   `{"code":40034102,"message":"主动消息失败, 无权限"}`。
   沙箱环境不受此限（但沙箱只能操作沙箱群/沙箱单聊）。
2. **用户可以单方面关掉主动消息。** QQ 客户端 →「允许主动发送」开关。
   关掉后无论怎么发都失败，而且**返回里不会告诉你原因**——这是最容易误判成
   "程序坏了"的一种情况。
3. **频控**：群聊主动消息，单关系维度 20 条/分钟，每个群每天最多接收 1000 条；
   Bot 维度未认证 30 条/分钟、已认证 60 条/分钟。

## openid 怎么拿（这一步绕不过去）

`group_openid` / `user_openid` **不是 QQ 号**，只能由平台在事件里下发，无法自行推导。
做法：把机器人加进目标群（或加为好友），**让它收到一条消息**（群里 @ 它一下），
从事件的 `d.group_openid` / `d.author.user_openid` 取值。
仓库内 `tools/qq_fetch_openid.py` 就是干这件事的（连网关、收到就把 openid 打出来）。

## 接口与实测记录（2026-09-25 用伪造凭证真实调用核对）

| 调用 | 返回 |
|---|---|
| `POST bots.qq.com/app/getAppAccessToken`（假 appId） | HTTP **200** `{"code":10004,"message":"机器人不存在"}` |
| `POST api.sgroup.qq.com/v2/groups/{id}/messages`（假 token） | HTTP **401** `{"message":"AccessToken无效或过期","code":11244,"err_code":40011027}` |
| 同上，沙箱域名 | 同上（两个环境都可达） |
| `GET /gateway`（假 token） | HTTP 401，同上 |
| appId 传垃圾值 | 网关直接返 **502 HTML**（不是 JSON！） |

两个结论：
- 取 token 失败也返回 **HTTP 200**，成败**只能解析 body**；
- **`code` 与 `err_code` 是两个不同的值**（11244 vs 40011027），文档未说明二者关系，
  本模块以 `code` 为主判据、`err_code` 一并带在报错里便于排查；
- 发消息接口的鉴权失败返回 **HTTP 401**，这是少数"状态码本身有意义"的场景。
"""

from __future__ import annotations

import re

from ..errors import PermanentError, RateLimitError, TokenExpiredError, RetryableError
from ..text_utils import mask_secret, one_line
from .base import Channel

_TOKEN_URL = "https://bots.qq.com/app/getAppAccessToken"
_BASE_PROD = "https://api.sgroup.qq.com"
_BASE_SANDBOX = "https://sandbox.api.sgroup.qq.com"

# ---------------------------------------------------------------- 错误码
# 实测确认（用伪造凭证真实调用得到，可复现）
_VERIFIED = {
    10004: "appId 对应的机器人不存在，请核对开放平台的 AppID",
    11244: "access_token 无效或已过期（会先刷新 token 再重试）",
}
# 来自官方文档错误码表 / 社区实测报告，本机无法在没有真实凭证的情况下复现，
# 归类按文档语义，标注出来以便日后核对
_FROM_DOC = {
    100007: "AppID 无效，或机器人状态不正常（被封禁/已删除）",
    100016: "AppID 或 ClientSecret 不正确，请与开放平台管理端核对",
    40034102: "主动消息失败、无权限 —— 机器人未上线，或用户关闭了「允许主动发送」",
}
_RATE_CODES = {
    100001: "请求过于频繁（取 token 接口）",
    22009: "发送消息频率超限，请降低发送频率",
}
_TOKEN_CODES = {11244, 11243, 11242}    # 11244 已实测，其余为文档中的鉴权类

# openid 在 URL path 里，日志里要遮掉。
# 注意分组要**包含前导斜杠**，否则替换后会把 /v2 粘连到域名上（实测踩过：
# 输出成 https://api.sgroup.qq.comv2/groups/... 这种畸形 URL）。
_OPENID_SEG = re.compile(r"(/v2/(?:groups|users)/)([^/]+)")


class QqBotChannel(Channel):
    type_name = "qq_bot"
    aliases = ("qq", "qqbot", "qq_channel", "qq_official")
    display_name = "QQ 机器人（QQ 群 / 单聊）"
    supports_markdown = True
    # 官方 wiki 未公布消息长度硬上限（第三方文档说的 3000 字符不是官方口径）。
    # 取 3000 字节保守值，超长自动分片续发 —— 宁可多一条，也不要整条被拒。
    text_limit = 3000
    markdown_limit = 3000
    # 群聊主动消息单关系维度 20 条/分钟；未认证 Bot 维度 30 条/分钟。
    # 取 3 秒间隔，留足余量。
    min_interval = 3.0
    # 消息内容即全部，没有独立的 title 字段
    embed_title = True
    required_fields = ("appid", "clientsecret")

    def validate(self) -> None:
        super().validate()
        if not (str(self.raw.get("group_openid", "") or "").strip()
                or str(self.raw.get("user_openid", "") or "").strip()):
            raise PermanentError(
                "必须配置 group_openid（发到群）或 user_openid（发到单聊）之一。\n"
                "openid 不是 QQ 号，无法自行推导 —— 把机器人加进群后 @ 它一次，"
                "从事件里取；可直接运行：python tools/qq_fetch_openid.py")
        if str(self.raw.get("group_openid", "") or "").strip() and \
                str(self.raw.get("user_openid", "") or "").strip():
            self.log.warning("group_openid 与 user_openid 同时配置，将优先发到群")

    # --------------------------------------------------------------- 环境

    @property
    def base_url(self) -> str:
        return _BASE_SANDBOX if self.raw.get("sandbox") else _BASE_PROD

    @property
    def target(self) -> tuple[str, str]:
        """返回 (场景, openid) —— 场景为 'groups' 或 'users'。"""
        grp = str(self.raw.get("group_openid", "") or "").strip()
        if grp:
            return "groups", grp
        return "users", str(self.raw.get("user_openid", "") or "").strip()

    def _mask_target(self) -> str:
        scene, oid = self.target
        return f"{scene}/{mask_secret(oid, 4, 4)}"

    # --------------------------------------------------------------- token

    @property
    def _token_key(self) -> str:
        return f"qq_bot:{self.field('appid')}"

    def _post(self, url: str, payload: dict, headers: dict | None = None) -> dict:
        """QQ 专用请求：需要处理"非 JSON 响应"这种实测出现过的情况。"""
        if self.dry_run and not self.verifying:
            import json as _json
            size = len(_json.dumps(payload, ensure_ascii=False).encode("utf-8"))
            # 日志前把 URL 里的 openid 遮掉
            self._dry_run_stop(_OPENID_SEG.sub(r"\1***", url), size)

        resp = self.request("POST", url, json=payload, headers=headers)
        text = resp.text or ""
        try:
            data = resp.json()
        except ValueError:
            data = None

        if not isinstance(data, dict):
            # 实测：appId 传垃圾值时网关直接返 502 HTML。5xx 归可重试（可能是网关抖动），
            # 4xx 且非 JSON 说明请求本身有问题，重试没有意义。
            if resp.status_code >= 500:
                raise RetryableError(
                    f"QQ 网关返回 {resp.status_code} 且响应非 JSON: {one_line(text, 160)}")
            raise PermanentError(
                f"QQ 接口返回 {resp.status_code} 且响应非 JSON: {one_line(text, 160)}")

        data.setdefault("_http_status", resp.status_code)
        return data

    def _fetch_token(self) -> tuple[str, float]:
        payload = {
            "appId": str(self.field("appid")),
            "clientSecret": str(self.field("clientsecret")),
        }
        self.log.info("重新获取 QQ access_token (appId=%s clientSecret=%s)",
                      self.field("appid"), mask_secret(self.field("clientsecret")))
        data = self._post(_TOKEN_URL, payload)

        code = data.get("code")
        if code not in (None, 0):
            if code in _RATE_CODES:
                raise RateLimitError(f"取 access_token 失败：{_RATE_CODES[code]}",
                                     code=code, raw=data, retry_after=60)
            if code in _VERIFIED:
                raise PermanentError(_VERIFIED[code], code=code, raw=data)
            if code in _FROM_DOC:
                raise PermanentError(_FROM_DOC[code], code=code, raw=data)
            raise PermanentError(
                f"取 access_token 失败 code={code} {data.get('message') or ''}",
                code=code, raw=data)

        token = str(data.get("access_token") or "")
        if not token:
            raise RetryableError(f"取 access_token 返回里没有 access_token: {data}")

        # 注意：文档示例里 expires_in 是**字符串** "7200"，不是数字。做兼容转换，
        # 否则 float() 之外的用法会在某些部署上直接抛异常。
        try:
            expires = float(data.get("expires_in") or 7200)
        except (TypeError, ValueError):
            self.log.warning("expires_in 不是数字(%r)，按 7200 秒处理", data.get("expires_in"))
            expires = 7200.0
        return token, expires

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

    def _headers(self) -> dict:
        # X-Union-Appid 必须带，否则部分场景鉴权不通过
        return {
            "Authorization": f"QQBot {self._token()}",
            "X-Union-Appid": str(self.field("appid")),
        }

    def _msg_type(self) -> int:
        raw = self.raw.get("msg_type", "text")
        if isinstance(raw, int):
            return raw
        return {"text": 0, "markdown": 2}.get(str(raw).lower(), 0)

    def deliver(self, content: str, *, chunk_index: int, total_chunks: int) -> None:
        msg_type = self._msg_type()
        if msg_type not in (0, 2):
            raise PermanentError(
                f"msg_type 只支持 text(0) / markdown(2)，收到: {self.raw.get('msg_type')!r}")

        if total_chunks > 1:
            content = f"{content}\n[{chunk_index + 1}/{total_chunks}]"

        if msg_type == 2:
            # 传了 markdown 之后 content 必须为空，否则平台报参数错误
            payload = {"msg_type": 2, "markdown": {"content": content}}
        else:
            payload = {"msg_type": 0, "content": content}

        # 被动回复：带上 msg_id 则 5 分钟内有效、不计入主动消息额度。
        # 本程序是定时主动推送，正常不带；保留配置口子以便需要时使用。
        if str(self.raw.get("msg_id", "") or "").strip():
            payload["msg_id"] = str(self.raw["msg_id"]).strip()
            payload["msg_seq"] = int(self.raw.get("msg_seq") or 1)

        scene, openid = self.target
        url = f"{self.base_url}/v2/{scene}/{openid}/messages"

        data = self._post(url, payload, headers=self._headers())

        http = data.get("_http_status")
        code = data.get("code")
        err_code = data.get("err_code")
        msg = data.get("message") or data.get("msg") or ""

        # 成功时 QQ 返回 HTTP 200 且 body 里**没有 code 字段**（带 id/timestamp），
        # 所以不能用 code == 0 判断
        if http == 200 and code in (None, 0):
            return

        code_str = f"code={code}" + (f"/err_code={err_code}" if err_code not in (None, code) else "")
        if code in _TOKEN_CODES or (http == 401 and code is None):
            raise TokenExpiredError(
                f"access_token 失效({code_str}) {msg}", code=code, raw=data)
        if code in _RATE_CODES:
            raise RateLimitError(f"{_RATE_CODES[code]}", code=code, raw=data, retry_after=60)
        if http == 429:
            raise RateLimitError("QQ 接口限流(HTTP 429)", code=code, raw=data, retry_after=60)
        if code in _VERIFIED:
            raise PermanentError(_VERIFIED[code], code=code, raw=data)
        if code in _FROM_DOC:
            raise PermanentError(_FROM_DOC[code], code=code, raw=data)
        if http is not None and 500 <= int(http) < 600:
            raise RetryableError(f"QQ 服务端 {http} {msg} ({code_str})", code=code, raw=data)
        raise PermanentError(
            f"QQ 接口返回 http={http} {code_str} {msg}", code=code, raw=data)

    # --------------------------------------------------------------- 自检

    def check(self) -> str:
        """真实取一次 access_token，能验出 appId / clientSecret 是否配错。"""
        _, expires = self._fetch_token()
        env = "沙箱" if self.raw.get("sandbox") else "正式"
        return (f"凭证可用（access_token 已取到，有效期 {int(expires)} 秒）｜"
                f"环境={env}｜目标={self._mask_target()}｜"
                f"注意：openid 是否正确、机器人是否已上线，只有真发一条才能确认")
