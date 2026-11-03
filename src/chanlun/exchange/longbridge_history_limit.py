"""One fair history queue across processes: at most 60 calls per 30 seconds.

Contract: https://open.longbridge.cn/zh-CN/docs/quote/pull/history-candlestick
Reuse the tested FIFO/priority admission engine, with separate provider state.
No credentials or symbols are persisted; each actual retry needs a new permit.
"""

from pathlib import Path

from .quote_capacity import QuoteConnectionCancelled, worker_quote_cancelled
from .usmart_rate_limit import (
    USmartRateLimiter,
    USmartRateLimitTimeout,
    USmartRateLimitCancelled,
    USmartRateLimitStateError,
    _bucket,
)

MIN_INTERVAL = 0.51
HISTORY_PAGE_WORKERS = 4


class LongbridgeHistoryBusy(TimeoutError):
    retryable = False
    code = "longbridge_history_rate_limit_timeout"

    def __init__(self, retry_after=1):
        self.retry_after = max(1, retry_after)
        super().__init__("长桥历史请求正在统一排队，请稍后重试")

    def __reduce__(self):
        return type(self), (self.retry_after,), self.__dict__


def _default_root():
    from chanlun import config

    return config.get_data_path() / "quote_budget" / "longbridge" / "history"


def _history_bucket(host, channel, endpoint):
    identity, _ = _bucket(host, channel, endpoint)
    return identity, MIN_INTERVAL


class LongbridgeHistoryLimiter:
    def __init__(self, root=None, **clocks):
        self.engine = USmartRateLimiter(
            Path(root) if root is not None else _default_root(),
            bucket=_history_bucket,
            **clocks,
        )

    def wait(
        self, *, priority="interactive", timeout=30.0, cancelled=worker_quote_cancelled
    ):
        try:
            return self.engine.wait(
                host="https://openapi.longbridge.cn",
                channel="history",
                endpoint="historycandlesticks",
                timeout=timeout,
                priority="background" if priority == "prewarm" else "interactive",
                cancelled=cancelled,
            )
        except USmartRateLimitCancelled as exc:
            raise QuoteConnectionCancelled(
                "Longbridge history queue cancelled"
            ) from exc
        except USmartRateLimitTimeout as exc:
            raise LongbridgeHistoryBusy(exc.retry_after) from exc
        except USmartRateLimitStateError as exc:
            raise LongbridgeHistoryBusy(30) from exc


def wait_history_request(*, priority="interactive", timeout=30.0):
    if timeout <= 0:
        raise LongbridgeHistoryBusy()
    return LongbridgeHistoryLimiter().wait(priority=priority, timeout=timeout)
