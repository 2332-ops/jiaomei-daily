"""文本工具：按**字节**计算长度、切分、截断。

为什么必须按字节而不是按字符：
企业微信官方文档给出的限制是「text 类型消息内容最长 2048 **字节**」
（markdown 是 4096 字节），中文一个字占 3 字节。用 len(str) 判断长度，
一条 800 字的中文消息看着"才 800 字"，实际已经 2400 字节，超限被拒。
"""

from __future__ import annotations

import re


def utf8_len(text: str) -> int:
    """UTF-8 编码后的字节数。"""
    return len(text.encode("utf-8"))


def truncate_bytes(text: str, limit: int, suffix: str = "…") -> str:
    """截断到不超过 limit 字节，且不切断多字节字符。"""
    if limit <= 0:
        return ""
    if utf8_len(text) <= limit:
        return text
    budget = max(0, limit - utf8_len(suffix))
    out: list[str] = []
    used = 0
    for ch in text:
        n = len(ch.encode("utf-8"))
        if used + n > budget:
            break
        out.append(ch)
        used += n
    return "".join(out) + suffix


def _hard_split(line: str, limit: int) -> list[str]:
    """单行超长时按字符硬切。"""
    out: list[str] = []
    buf: list[str] = []
    used = 0
    for ch in line:
        n = len(ch.encode("utf-8"))
        if used + n > limit and buf:
            out.append("".join(buf))
            buf, used = [], 0
        buf.append(ch)
        used += n
    if buf:
        out.append("".join(buf))
    return out


def split_by_bytes(text: str, limit: int) -> list[str]:
    """把长文本切成若干片，每片 UTF-8 字节数 <= limit。

    优先在换行处断开 —— 报告类文本在段落中间被截断，手机上读起来很别扭。
    仅在单行本身就超限时才硬切。
    """
    if limit <= 0:
        raise ValueError("limit 必须大于 0")
    limit = max(1, limit)
    if utf8_len(text) <= limit:
        return [text]

    chunks: list[str] = []
    buf: list[str] = []
    used = 0

    for line in text.split("\n"):
        pieces = _hard_split(line, limit - 1) if utf8_len(line) > limit - 1 else [line]
        for piece in pieces:
            n = utf8_len(piece) + 1  # 预留换行符的 1 字节
            if used + n > limit and buf:
                chunks.append("\n".join(buf))
                buf, used = [], 0
            buf.append(piece)
            used += n
    if buf:
        chunks.append("\n".join(buf))
    return chunks


_MD_PATTERNS = (
    (re.compile(r"!\[[^\]]*\]\([^)]*\)"), ""),          # 图片
    (re.compile(r"\[([^\]]*)\]\([^)]*\)"), r"\1"),      # 链接只保留文字
    (re.compile(r"^\s{0,3}#{1,6}\s*", re.M), ""),        # 标题井号
    (re.compile(r"\*\*([^*]+)\*\*"), r"\1"),             # 粗体
    (re.compile(r"(?<!\*)\*([^*]+)\*(?!\*)"), r"\1"),    # 斜体
    (re.compile(r"`([^`]*)`"), r"\1"),                   # 行内代码
    (re.compile(r"^\s*>\s?", re.M), ""),                 # 引用
    (re.compile(r"^\s*[-*+]\s+", re.M), ""),             # 无序列表的短横线
    (re.compile(r"^\s*\d+[.)]\s+", re.M), ""),           # 有序列表的 1. 2.
    (re.compile(r"^\s*\|.*\|\s*$", re.M), ""),           # 表格行整行丢弃
    (re.compile(r"^\s*[-:| ]{3,}\s*$", re.M), ""),       # 表格分隔线
    (re.compile(r"[ \t]+$", re.M), ""),                  # 行尾空白
    (re.compile(r"\n{3,}"), "\n\n"),                     # 压缩空行
)


def markdown_to_plain(text: str) -> str:
    """把 Markdown 降级成纯文本，供只吃 text 的通道使用。

    注意：表格会被整行丢弃而不是转成文本 —— 手机上看不了宽表格，
    宁可少内容，也不要推一排乱七八糟的竖线过去。
    """
    out = text
    for pat, repl in _MD_PATTERNS:
        out = pat.sub(repl, out)
    return out.strip()


def mask_secret(value: object, keep_head: int = 6, keep_tail: int = 4) -> str:
    """日志里不要打完整凭证。"""
    s = str(value or "")
    if not s:
        return "(空)"
    if len(s) <= keep_head + keep_tail:
        return "*" * len(s)
    return f"{s[:keep_head]}…{s[-keep_tail:]}(len={len(s)})"


def one_line(text: str, max_len: int = 60) -> str:
    """把多行文本压成一行，用于日志。"""
    s = re.sub(r"\s+", " ", str(text or "")).strip()
    return s if len(s) <= max_len else s[: max_len - 1] + "…"
