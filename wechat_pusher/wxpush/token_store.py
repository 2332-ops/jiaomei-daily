"""access_token 缓存。

企业微信 / 公众号的 access_token 有硬性约束，踩错就掉坑：

1. **有效期 7200 秒，且是"active 计时"** —— 每次调用接口都会把有效期重置为 7200 秒，
   但只在原有效期 5 分钟内才算续期成功。换句话说：**频繁取 token 反而会提前作废旧 token**。
   所以必须缓存，绝不能"每次发送都重新 gettoken"。
2. **同一个应用多次 gettoken 会让上一个失效。** 如果你同时在别处（比如另一个脚本、
   n8n、服务端接口）也取了这个应用的 token，你的 token 会被对方挤掉，表现为周期性 40014。
   这是最常见的"半夜突然收不到消息"的根因。
3. **一定要留安全余量。** 缓存到 7200 秒整才刷新，在网络慢的时候会正好卡在过期瞬间。
   默认提前 300 秒刷新。

因此这里做了两件事：落盘缓存（进程重启后不用重新取，减少 token 被挤掉的概率）
+ 失效即主动作废（收到 40014/42001 后立刻丢弃，下次必定重取）。
"""

from __future__ import annotations

import json
import os
import threading
import time
from typing import Callable


class TokenStore:
    def __init__(self, cache_path: str | None = None, safety_margin: float = 300.0):
        self.cache_path = cache_path
        self.safety_margin = float(safety_margin)
        self._mem: dict[str, dict] = {}
        self._lock = threading.Lock()
        self._loaded = False

    # ------------------------------------------------------------------ 内部

    def _load(self) -> None:
        if self._loaded or not self.cache_path:
            self._loaded = True
            return
        self._loaded = True
        try:
            with open(self.cache_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                self._mem = data
        except FileNotFoundError:
            self._mem = {}
        except Exception:  # noqa: BLE001 - 缓存损坏不该让程序起不来
            self._mem = {}

    def _persist(self) -> None:
        if not self.cache_path:
            return
        try:
            d = os.path.dirname(os.path.abspath(self.cache_path))
            if d:
                os.makedirs(d, exist_ok=True)
            tmp = self.cache_path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self._mem, f, ensure_ascii=False, indent=2)
            os.replace(tmp, self.cache_path)  # 原子替换，避免断电写坏缓存
        except Exception:  # noqa: BLE001
            pass

    # ------------------------------------------------------------------ 对外

    def get(self, name: str, ttl: float,
            fetcher: Callable[[], tuple[str, float]]) -> str:
        """取 token。命中未过期缓存则直接返回，否则调用 fetcher() 并写缓存。

        fetcher() 应返回 (token, expires_in_seconds)。
        """
        with self._lock:
            self._load()
            now = time.time()
            item = self._mem.get(name)
            if item and item.get("token") and float(item.get("expire_at", 0)) > now:
                return str(item["token"])

            token, expires_in = fetcher()
            if not token:
                raise RuntimeError(f"fetcher 未返回 token: {name}")
            ttl = float(ttl or expires_in or 7200)
            effective = max(60.0, min(ttl, float(expires_in or ttl)))
            self._mem[name] = {
                "token": token,
                "expire_at": now + max(60.0, effective - self.safety_margin),
                "fetched_at": now,
            }
            self._persist()
            return token

    def invalidate(self, name: str) -> None:
        """主动作废（收到 40014 / 42001 时调用）。"""
        with self._lock:
            self._load()
            if self._mem.pop(name, None) is not None:
                self._persist()

    def peek(self, name: str) -> dict | None:
        with self._lock:
            self._load()
            return self._mem.get(name)
