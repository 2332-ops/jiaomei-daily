"""推送异常体系。

核心设计：把"重试有没有意义"编码进**异常类型**里，而不是让调用方去猜各家平台的
errcode 含义。调用方只需要区分四种情况：

    PermanentError     凭证错/参数错/无权限 —— 重试一百次还是失败，立刻放弃并告警
    TokenExpiredError   access_token 失效 —— 刷新 token 后重试，必然能成功
    RateLimitError      触发限流 —— 按平台给的冷却时间等待后重试
    RetryableError      网络抖动/服务端 5xx —— 指数退避重试

这条边界是这类推送程序能否长期稳定运行的分水岭：把 40014（token 过期）当成永久失败，
程序会在运行 2 小时后彻底哑掉；把 93000（webhook key 无效）当成可重试，会白白等满
4 次退避、拖慢整轮任务。
"""

from __future__ import annotations


class PushError(Exception):
    """所有推送异常的基类。"""

    #: 该类错误是否值得重试（子类覆盖）
    retryable = False

    def __init__(self, message: str, *, code: object = None, raw: object = None):
        super().__init__(message)
        self.message = message
        #: 平台返回的原始错误码（企业微信是 errcode，PushPlus 是 code …）
        self.code = code
        #: 平台返回的完整响应体，便于排查时原样打日志
        self.raw = raw

    def __str__(self) -> str:
        if self.code is None:
            return self.message
        return f"{self.message} [code={self.code}]"

    def as_dict(self) -> dict:
        return {"type": type(self).__name__, "message": self.message,
                "code": self.code, "retryable": self.retryable}


class ConfigError(PushError):
    """配置缺失或非法。属于使用者的错，不该重试。"""

    retryable = False


class DryRunStop(PushError):
    """dry-run 下的"虚拟发送"信号 —— **不是错误**。

    为什么要用异常来做这件事：dry-run 的短路必须只有一处实现。

    早期做法是让请求层返回一个形如 `{"errcode": 0}` 的哨兵 dict，由各通道自己判断
    "这是不是 dry-run"。它埋着一个结构性缺陷：哨兵长得像"成功响应"，于是只有
    那些**恰好奇妙地用 errcode 判断成败**的通道能正常工作；PushPlus 用 `code`
    字段判断，拿到哨兵后 `code=None`，dry-run 直接被误判成失败（实测暴露，
    且此前所有 dry-run 测试都走的是企业微信通道，从未覆盖到）。

    改成抛异常之后，通道完全不需要知道 dry-run 的存在，"漏判"在结构上不可能发生。

    不继承 PermanentError（虽然同样是 retryable=False）：它不是失败，需要由
    base.send() 单独捕获并按成功处理。retryable=False 保证它穿过 call_with_retry
    时不会被重试。
    """

    retryable = False


class PermanentError(PushError):
    """永久性失败：重试无意义，必须人工介入。"""

    retryable = False


class RetryableError(PushError):
    """临时性失败：网络抖动、服务端 5xx、未知错误，退避后可重试。"""

    retryable = True


class RateLimitError(RetryableError):
    """触发平台限流。带 retry_after 表示建议等待秒数。"""

    retryable = True

    def __init__(self, message: str, *, code: object = None, raw: object = None,
                 retry_after: float | None = None):
        super().__init__(message, code=code, raw=raw)
        self.retry_after = retry_after


class TokenExpiredError(RetryableError):
    """access_token 无效或已过期。重试前必须先刷新 token。"""

    retryable = True


def is_retryable(exc: BaseException) -> bool:
    """判断一个异常是否值得重试。

    规则：
      - 已是 PushError -> 看它自己的 retryable 标记
      - 其他异常（requests 的各种 NetworkError、超时、ConnectionError、乃至
        我们自己没预料到的 KeyError）一律按"可重试"处理。理由：网络类异常占了
        绝大多数；而真正的永久性错误（凭证/参数）在各通道里都已经被显式转成
        PermanentError 了，不会走到这个分支。

    统一用这个函数判断，避免各处出现 `getattr(exc, "retryable", False)` 这种
    写法 —— 那会让 requests.ConnectionError 被误判成"不可重试"。
    """
    if isinstance(exc, PushError):
        return exc.retryable
    return True
