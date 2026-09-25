"""企业微信「自建应用」消息 —— **唯一能推给指定微信账号（成员）的官方方式**。

这是本模块的重点。群机器人只能发群，要"发给某个人"，必须走自建应用：

    企业微信后台 → 应用管理 → 自建 → 创建应用 → 拿到 AgentId / Secret
    我的企业 → 企业信息 → 拿到 CorpID
    应用 → 「可见范围」里把目标成员加进去（漏了会报 60011）
    应用 → 「企业可信IP」把服务器出口 IP 加进去（漏了会报 60020，仅部分情况触发）

关键前提（很多人卡在这里）：
**成员必须关注「微信插件」才能在个人微信里收到这条消息。**
企业微信 App → 我 → 设置 → 新消息通知 → 微信插件 → 扫码关注。
不关注的话，消息只会出现在企业微信 App 里，个人微信收不到。

配置示例：
    - type: wecom_app
      enabled: true
      name: "发给我自己"
      corpid: "${WECOM_CORPID}"
      corpsecret: "${WECOM_SECRET}"
      agentid: 1000002
      touser: "ZhangSan"          # 成员账号（UserID），多个用 | 分隔
      # toparty: "1|2"            # 按部门发
      # totag: "1"                # 按标签发
      # touser: "@all"            # 发给全部成员（慎用）
      msg_type: "text"            # text 或 markdown
"""

from __future__ import annotations

from ..errors import (ConfigError, PermanentError, RateLimitError,
                      TokenExpiredError, RetryableError)
from ..text_utils import mask_secret
from .base import Channel

# 这些错误码的语义固定为"重取 token 就能好"
_TOKEN_CODES = {40014, 42001, 41001}

# 实测确认：{"errcode":40014,"errmsg":"invalid access_token"}
# 实测确认：{"errcode":40013,"errmsg":"invalid corpid, hint:[...]"}（取 token 时）
# 其余为官方文档错误码表
_CODE_MESSAGES = {
    40013: "corpid 无效，检查企业 ID 是否复制完整",
    60011: "调用方没有权限操作该成员/部门，请把成员加入应用的「可见范围」",
    60020: "当前 IP 不在企业的可信 IP 列表内，请在应用设置里添加服务器出口 IP",
    81013: "touser 不存在或该成员未关注企业微信",
    67203: "该应用今日的消息推送数量已达上限",
    40003: "touser 不合法（成员账号拼写错误，或成员不在应用可见范围内）",
    301002: "无权限操作指定的 user",
    45033: "接口调用超过限制，请降低频率",
}


class WecomAppChannel(Channel):
    type_name = "wecom_app"
    aliases = ("wecom_agent", "qyweixin_app", "wecom_appmsg")
    display_name = "企业微信应用消息（可指定成员）"
    supports_markdown = True
    text_limit = 2048
    markdown_limit = 4096
    min_interval = 0.6          # 应用消息限流比群机器人宽松，但不能太快
    required_fields = ("corpid", "corpsecret", "agentid")

    def validate(self) -> None:
        super().validate()
        # 收件人缺了就没法发，放到配置阶段就报错，别等到发送时才炸
        if not any(str(self.raw.get(k, "") or "").strip()
                   for k in ("touser", "toparty", "totag")):
            raise ConfigError(
                "必须至少配置 touser / toparty / totag 之一（touser 是成员账号，"
                "多个用 | 分隔，@all 表示全部成员）")
        try:
            int(self.raw.get("agentid"))
        except (TypeError, ValueError):
            raise ConfigError(
                f"agentid 必须是数字，收到: {self.raw.get('agentid')!r}"
                "（在企业微信后台「应用管理」里能看到，是一个整数）")

    # --------------------------------------------------------------- token

    @property
    def _token_key(self) -> str:
        return f"wecom_app:{self.field('corpid')}:{self.field('agentid')}"

    def _fetch_token(self) -> tuple[str, float]:
        url = ("https://qyapi.weixin.qq.com/cgi-bin/gettoken"
               f"?corpid={self.field('corpid')}&corpsecret={self.field('corpsecret')}")
        self.log.info("重新获取企业微信 access_token (corpsecret=%s)",
                      mask_secret(self.field("corpsecret")))
        data = self._json(self.request("GET", url))
        code = data.get("errcode")
        if code != 0:
            errmsg = data.get("errmsg") or ""
            if code == 40013:
                raise PermanentError(_CODE_MESSAGES[40013], code=code, raw=data)
            if code == 40001:
                raise PermanentError(
                    "corpsecret 无效。注意别把「应用的 Secret」和「通讯录同步 Secret」搞混",
                    code=code, raw=data)
            raise RetryableError(f"获取 access_token 失败 errcode={code} {errmsg}",
                                 code=code, raw=data)
        return str(data.get("access_token") or ""), float(data.get("expires_in") or 7200)

    def _token(self) -> str:
        # dry-run 下不取真实 token，也绝不把假 token 写进缓存
        # （写进去的话，下一次真实运行会先撞一次 40014 才自愈）
        if self.dry_run and not self.verifying:
            return "dry-run-token"
        if self.tokens is None:
            return self._fetch_token()[0]
        return self.tokens.get(self._token_key, 7200.0, self._fetch_token)

    def invalidate_token(self) -> None:
        if self.tokens is not None:
            self.tokens.invalidate(self._token_key)

    # --------------------------------------------------------------- 发送

    def _audience(self) -> dict:
        """构造收件人。validate() 已保证至少有一个非空。"""
        out = {}
        for key in ("touser", "toparty", "totag"):
            val = self.raw.get(key)
            if val is None:
                continue
            if isinstance(val, (list, tuple)):
                val = "|".join(str(x).strip() for x in val if str(x).strip())
            val = str(val).strip()
            if val:
                out[key] = val
        return out

    def deliver(self, content: str, *, chunk_index: int, total_chunks: int) -> None:
        msg_type = str(self.raw.get("msg_type") or "text").lower()
        if msg_type not in ("text", "markdown"):
            raise PermanentError(f"msg_type 只支持 text / markdown，收到: {msg_type}")

        if msg_type == "text" and total_chunks > 1:
            content = f"{content}\n[{chunk_index + 1}/{total_chunks}]"

        payload = {
            "agentid": int(self.field("agentid")),
            "msgtype": msg_type,
            msg_type: {"content": content},
            "safe": int(self.raw.get("safe") or 0),
        }
        payload.update(self._audience())
        # 同一内容重复推送时企业微信会去重，导致"重试成功但对方没收到"的错觉。
        # 用内容哈希做去重检查，既保留平台保护，又不影响真实重试。
        enable_dedup = bool(self.raw.get("enable_duplicate_check", False))
        payload["enable_duplicate_check"] = 1 if enable_dedup else 0
        if enable_dedup:
            payload["duplicate_check_interval"] = int(
                self.raw.get("duplicate_check_interval") or 1800)

        url = ("https://qyapi.weixin.qq.com/cgi-bin/message/send?access_token="
               + self._token())

        data = self._post_json(url, payload)
        code = data.get("errcode")
        if code == 0:
            # 注意：errcode=0 不代表所有人都收到，个别成员可能失败
            invalid = data.get("invaliduser") or ""
            if invalid:
                self.log.warning("以下成员推送失败（不存在或未关注）: %s", invalid)
            if data.get("unlicenseduser"):
                self.log.warning("以下成员无接口权限: %s", data.get("unlicenseduser"))
            return

        errmsg = data.get("errmsg") or ""
        if code in _TOKEN_CODES:
            raise TokenExpiredError(f"access_token 失效({code}) {errmsg}", code=code, raw=data)
        if code in (45009, 45033):
            raise RateLimitError("企业微信接口调用超过限制", code=code, raw=data, retry_after=60)
        if code in _CODE_MESSAGES:
            raise PermanentError(_CODE_MESSAGES[code], code=code, raw=data)
        raise RetryableError(f"企业微信返回 errcode={code} errmsg={errmsg}", code=code, raw=data)

    # --------------------------------------------------------------- 自检

    def check(self) -> str:
        """只验证凭证是否可用（真实取一次 token），不发消息。"""
        tok, expires = self._fetch_token()
        return f"凭证可用（access_token 已取到，有效期 {int(expires)} 秒）"
