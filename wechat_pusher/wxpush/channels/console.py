"""控制台通道 —— 只在本机打印，不产生任何外部推送。

用途：
  1. 第一次跑通流程时先看文案，别浪费推送额度
  2. 在 CI / 本地调试时替代真实通道
  3. 配合 --only console 验证"消息内容组装、分片、去重"这些不依赖网络的部分
"""

from __future__ import annotations

import sys

from .base import Channel


class ConsoleChannel(Channel):
    type_name = "console"
    display_name = "本机控制台（调试用）"
    supports_markdown = True
    text_limit = 100000
    markdown_limit = 100000
    required_fields = ()

    def deliver(self, content: str, *, chunk_index: int, total_chunks: int) -> None:
        stamp = f"[{chunk_index + 1}/{total_chunks}]" if total_chunks > 1 else ""
        out = sys.stdout
        out.write("\n" + "=" * 60 + "\n")
        out.write(f"  console 通道输出 {stamp}\n")
        out.write("=" * 60 + "\n")
        out.write(content + "\n")
        out.write("=" * 60 + "\n")
        out.flush()

    def check(self) -> str:
        return "始终可用（仅本机打印，不发送任何外部请求）"
