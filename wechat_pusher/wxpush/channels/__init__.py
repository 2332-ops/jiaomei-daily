"""通道注册表。

新增一个平台只需要两步：
  1. 在 channels/ 下建一个文件，继承 Channel 并实现 deliver()
  2. 在下面 REGISTRY 里登记
"""

from __future__ import annotations

from ..errors import ConfigError
from .base import Channel, ChannelContext, SendResult
from .console import ConsoleChannel
from .pushplus import PushplusChannel
from .qq_bot import QqBotChannel
from .serverchan import ServerchanChannel
from .wechat_mp import WechatMpChannel
from .wecom_app import WecomAppChannel
from .wecom_bot import WecomBotChannel

REGISTRY: dict[str, type[Channel]] = {
    WecomBotChannel.type_name: WecomBotChannel,
    WecomAppChannel.type_name: WecomAppChannel,
    WechatMpChannel.type_name: WechatMpChannel,
    PushplusChannel.type_name: PushplusChannel,
    ServerchanChannel.type_name: ServerchanChannel,
    QqBotChannel.type_name: QqBotChannel,
    ConsoleChannel.type_name: ConsoleChannel,
}

#: 别名 -> 规范名
ALIASES: dict[str, str] = {}
for _cls in REGISTRY.values():
    for _alias in _cls.aliases:
        ALIASES[_alias] = _cls.type_name


def resolve_type(type_name: str) -> str:
    key = str(type_name or "").strip().lower()
    if key in REGISTRY:
        return key
    if key in ALIASES:
        return ALIASES[key]
    raise ConfigError(
        f"未知的通道类型: {type_name!r}。可用类型: {', '.join(sorted(REGISTRY))}")


def build_channel(raw: dict, ctx: ChannelContext) -> Channel:
    """按配置构造通道实例。"""
    if not isinstance(raw, dict):
        raise ConfigError(f"通道配置必须是字典，收到: {type(raw).__name__}")
    canonical = resolve_type(raw.get("type"))
    return REGISTRY[canonical]({**raw, "type": canonical}, ctx)


def list_channels() -> list[dict]:
    return [
        {
            "type": cls.type_name,
            "aliases": list(cls.aliases),
            "name": cls.display_name,
            "markdown": cls.supports_markdown,
            "text_limit_bytes": cls.text_limit,
            "required": list(cls.required_fields),
        }
        for cls in REGISTRY.values()
    ]


__all__ = ["Channel", "ChannelContext", "SendResult", "REGISTRY", "ALIASES",
           "resolve_type", "build_channel", "list_channels"]
