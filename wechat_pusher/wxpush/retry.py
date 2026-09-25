"""重试策略：区分"该等多久"和"该不该等"。

三种退避来源，优先级从高到低：
  1. 平台明确给了冷却时间（Retry-After 响应头 / RateLimitError.retry_after）—— 照做
  2. 指数退避 base * 2^n，上限 cap
  3. 在退避值上下加随机抖动，避免多个通道/多只股票在同一秒集中重试再次被限流
"""

from __future__ import annotations

import random
import time
from dataclasses import dataclass
from typing import Callable

from .errors import PermanentError, RateLimitError, is_retryable


@dataclass
class RetryPolicy:
    attempts: int = 4          # 总尝试次数（含首次）
    base: float = 1.0          # 首次退避基数（秒）
    cap: float = 30.0          # 指数退避的单次上限（秒）
    jitter: float = 0.35       # 抖动比例，0 表示不抖动
    # 平台明确给出冷却时间时，最多愿意等多久。这个值和 cap 分开是有原因的：
    # 企业微信限流后要求等 60 秒，若被 cap=30 截断，30 秒后重试必然再被拒，
    # 白白消耗一次重试次数、还可能加重限流。要么老实等满，要么直接放弃。
    retry_after_cap: float = 120.0

    def delay(self, attempt: int, retry_after: float | None = None) -> float:
        """attempt 为从 0 开始的已失败次数，返回下次尝试前应等待的秒数。"""
        if retry_after is not None:
            # 平台给的冷却时间不抖动：抖早了会被拒，抖晚了纯浪费
            return max(0.0, min(self.retry_after_cap, float(retry_after)))
        raw = min(self.cap, self.base * (2 ** attempt))
        if self.jitter <= 0:
            return raw
        return raw * (1 - self.jitter + random.random() * 2 * self.jitter)


def call_with_retry(fn: Callable[[], object],
                    policy: RetryPolicy,
                    *,
                    on_retry: Callable[[Exception, int], None] | None = None,
                    sleep: Callable[[float], None] = time.sleep) -> tuple[object, int]:
    """执行 fn()，按 policy 重试。

    on_retry(exc, attempt_index) 在每次**即将重试前**调用，用来做副作用准备
    （例如 TokenExpiredError 时刷新 access_token、ProxyError 时切换直连）。

    返回 (fn 的返回值, 实际尝试次数)。全部失败则抛出最后一次异常。
    """
    attempts = max(1, int(policy.attempts))
    last_exc: Exception | None = None

    for i in range(attempts):
        try:
            return fn(), i + 1
        except PermanentError as exc:
            # 永久失败直接冒泡，不消耗重试次数。
            # 但仍要标上"实际尝试过 1 次" —— 否则调用方汇总出的 attempts=0
            # 会被读成"根本没发出去"，而事实是发出去并被明确拒绝了。
            exc.attempts = i + 1
            raise
        except Exception as exc:  # noqa: BLE001 - 网络异常类型繁多，统一兜住
            last_exc = exc
            if not is_retryable(exc) or i >= attempts - 1:
                exc.attempts = i + 1
                break
            retry_after = getattr(exc, "retry_after", None)
            if isinstance(exc, RateLimitError) and retry_after is None:
                retry_after = min(policy.retry_after_cap, 30.0)
            if on_retry is not None:
                try:
                    on_retry(exc, i)
                except Exception:  # noqa: BLE001 - 副作用钩子失败不应中断重试
                    pass
            sleep(policy.delay(i, retry_after))

    assert last_exc is not None
    raise last_exc
