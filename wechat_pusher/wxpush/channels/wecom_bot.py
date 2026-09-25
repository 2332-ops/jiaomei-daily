"""企业微信群机器人 —— 推送到群聊，最省事的一条路。

适用：给"一个群"发消息（你可以给自己建一个只有自己的群，就等价于推给个人）。
配置：群聊 → 右上角 → 群机器人 → 添加 → 复制 Webhook 地址
文档：https://developer.work.weixin.qq.com/document/path/91770

限制（官方文档）：
  - text 类型 content 最长 2048 字节；markdown 类型最长 4096 字节
  - markdown 不支持 @成员，@ 只在 text 类型生效
  - 每个机器人每分钟最多 20 条

实测确认的错误码：
  {"errcode":93000,"errmsg":"invalid webhook url, hint:[...]"}   key 不对
"""

from __future__ import annotations

import re

from ..errors import PermanentError, RateLimitError, RetryableError
from ..text_utils import mask_secret
from .base import Channel

_WEBHOOK_KEY = re.compile(r"[?&]key=([0-9a-zA-Z\-_]+)")


class WecomBotChannel(Channel):
    type_name = "wecom_bot"
    aliases = ("wecom", "wecom_webhook", "qyweixin_bot")
    display_name = "企业微信群机器人"
    supports_markdown = True
    text_limit = 2048
    markdown_limit = 4096
    # 官方限流 20 条/分钟 -> 保守取 3.2 秒间隔
    min_interval = 3.2
    required_fields = ("webhook",)

    def validate(self) -> None:
        # 兼容只写 key 不写完整 URL 的配置
        if not str(self.raw.get("webhook", "") or "").strip() and \
                str(self.raw.get("key", "") or "").strip():
            self.raw["webhook"] = ("https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key="
                                   + str(self.raw["key"]).strip())
        super().validate()
        url = str(self.raw["webhook"])
        if not url.startswith("https://") or "key=" not in url:
            raise PermanentError(
                "webhook 格式不对，应形如 "
                "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=xxxxxxxx-xxxx-...")

    @property
    def webhook(self) -> str:
        return str(self.raw["webhook"]).strip()

    def check(self) -> str:
        """机器人 Webhook 没有独立的"探活"接口 —— 唯一的验证方式是真发一条。

        这里不去猜"发个空消息看是否报参数错误"这类旁路做法：企业微信没把这
        种行为写进文档，不同部署可能真的把空消息投递到群里，代价比收益大。
        所以如实告诉用户怎么验证，而不是给一个看似通过的自检结果。
        """
        return ("webhook 格式正确（key=…%s）。真实性未验证——企业微信机器人"
                "只能靠真发一条来确认，执行："
                "python send.py -t \"连通性测试\" --text \"测试\""
                % (mask_secret(_WEBHOOK_KEY.search(self.webhook).group(1), 4, 4)
                   if _WEBHOOK_KEY.search(self.webhook) else "????"))

    def _mentions(self) -> dict:
        out = {}
        lst = self.raw.get("mentioned_list") or []
        mob = self.raw.get("mentioned_mobile_list") or []
        if isinstance(lst, str):
            lst = [x.strip() for x in lst.replace(";", ",").split(",") if x.strip()]
        if isinstance(mob, str):
            mob = [x.strip() for x in mob.replace(";", ",").split(",") if x.strip()]
        if lst:
            out["mentioned_list"] = list(lst)
        if mob:
            out["mentioned_mobile_list"] = list(mob)
        return out

    def deliver(self, content: str, *, chunk_index: int, total_chunks: int) -> None:
        msg_type = str(self.raw.get("msg_type") or ("markdown" if self.supports_markdown else "text")).lower()
        mentions = self._mentions()

        if msg_type == "text":
            body: dict = {"content": content}
            body.update(mentions)
        else:
            if mentions:
                self.log.warning("企业微信 markdown 消息不支持 @成员，本次已忽略 mentioned_list")
            body = {"content": content}

        if total_chunks > 1:
            body["content"] = f"{body['content']}\n[{chunk_index + 1}/{total_chunks}]"

        data = self._post_json(self.webhook, {"msgtype": msg_type, msg_type: body})
        code = data.get("errcode")
        if code == 0:
            return

        errmsg = data.get("errmsg") or ""
        if code == 93000:
            raise PermanentError(
                "webhook key 无效。到群设置里重新复制机器人 Webhook 地址",
                code=code, raw=data)
        if code == 45009:
            raise RateLimitError("企业微信接口调用超过限制（20 条/分钟）",
                                 code=code, raw=data, retry_after=60)
        if code == 40058:
            raise PermanentError(f"参数不合法：{errmsg}", code=code, raw=data)
        raise RetryableError(f"企业微信返回 errcode={code} errmsg={errmsg}", code=code, raw=data)

    def mask_url(self) -> str:
        m = _WEBHOOK_KEY.search(self.webhook)
        return f"...key={m.group(1)[:4]}...{m.group(1)[-4:]}" if m else self.webhook
