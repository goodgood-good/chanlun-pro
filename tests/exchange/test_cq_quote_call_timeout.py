"""回归测试：_quote_call 的超时重建、重建节流、以及与批量池的隔离（防嵌套死锁）。

历史根因（2026-06-17 ~ 06-18）：
1. Python 3.10 下 ``concurrent.futures.TimeoutError`` 不是 builtin ``TimeoutError``，
   原 except 漏接 → 重建永不触发、连接半死无法自愈。
2. 重建无节流 → 重建风暴 + 旧连接累积。
3. ``_quote_call`` 与批量 ``klines`` 共用 ``self.executor`` → klines 分段填满 16-worker
   池后，分段内层 ``_quote_call`` 抢不到 worker → 嵌套提交死锁 → 8s 超时风暴（即便
   longbridge 本身 22ms）。修复：``_quote_call`` 改用独立的 ``_quote_call_executor``。
"""

import threading
import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeoutError

import pytest

from chanlun.exchange.exchange_cq import ExchangeChangQiao


class _FakeEx:
    """复用真实 _quote_call 代码，但不走 ExchangeChangQiao.__init__。"""

    _QUOTE_CALL_TIMEOUT = 8.0
    _quote_call = ExchangeChangQiao._quote_call  # 未绑定函数，实例调用时自动绑 self

    def __init__(self):
        self._quote_context_lock = threading.Lock()
        self._quote_context = object()  # 占位非 None
        self._quote_call_executor = ThreadPoolExecutor(max_workers=2)
        self.rebuilt = []

    def _rebuild_quote_ctx(self):
        self.rebuilt.append(True)


def test_quote_call_rebuilds_on_future_timeout():
    """future 超时（concurrent.futures.TimeoutError）必须触发重建并重新抛出。"""
    ex = _FakeEx()

    def _slow():
        time.sleep(2.0)
        return "ok"

    with pytest.raises(FuturesTimeoutError):
        ex._quote_call(_slow, timeout=0.2)

    assert ex.rebuilt == [True], (
        "future 超时必须触发 _rebuild_quote_ctx()；若为空说明 except 子句"
        "漏掉了 concurrent.futures.TimeoutError（Python<3.11 与 builtin 不同类）"
    )


def test_quote_call_returns_value_when_fast():
    """正常快速返回路径不受影响，且不触发重建。"""
    ex = _FakeEx()
    assert ex._quote_call(lambda: 42, timeout=2.0) == 42
    assert ex.rebuilt == []


def test_rebuild_throttled_against_storm(monkeypatch):
    """连续多次超时重建被节流：冷却窗口内只真正新建一次 QuoteContext（防重建风暴）。"""
    import chanlun.exchange.exchange_cq as cqmod

    built = []
    monkeypatch.setattr(cqmod, "QuoteContext", lambda config: built.append(1))

    class _Ex:
        _REBUILD_MIN_INTERVAL = cqmod.ExchangeChangQiao._REBUILD_MIN_INTERVAL
        _last_rebuild_ts = None
        _rebuild_quote_ctx = cqmod.ExchangeChangQiao._rebuild_quote_ctx

        def __init__(self):
            self._quote_context_lock = threading.Lock()
            self._quote_context = object()
            self.config = object()
            self._quote_call_executor = ThreadPoolExecutor(max_workers=1)

    ex = _Ex()
    for _ in range(5):
        ex._rebuild_quote_ctx()

    assert len(built) == 1, f"节流失效：冷却窗口内重建了 {len(built)} 次（应为 1）"


def test_quote_call_pool_isolated_from_batch_pool():
    """_quote_call 必须用独立池，不能与批量 klines 的 self.executor 共用，否则
    分段填满有界池后内层 _quote_call 抢不到 worker → 嵌套提交死锁。

    复现：2 个“分段”占满批量池(size=2)，每个分段内部再调 _quote_call。
    独立池下内层调用照常拿到 worker、秒回；若回退成共用池则会死锁(result 超时)。
    """

    class _Ex:
        _QUOTE_CALL_TIMEOUT = 8.0
        _quote_call = ExchangeChangQiao._quote_call

        def __init__(self):
            self._quote_context_lock = threading.Lock()
            self._quote_context = object()
            self.executor = ThreadPoolExecutor(max_workers=2)              # 批量分段池
            self._quote_call_executor = ThreadPoolExecutor(max_workers=2)  # _quote_call 专用池

        def _rebuild_quote_ctx(self):
            pass

    ex = _Ex()
    assert ex.executor is not ex._quote_call_executor

    def _segment():
        # 模拟 klines 分段任务：自身跑在 ex.executor 上，内部再调 _quote_call
        return ex._quote_call(lambda: "ok", timeout=2.0)

    futs = [ex.executor.submit(_segment) for _ in range(2)]
    results = [f.result(timeout=5) for f in futs]
    assert results == ["ok", "ok"]


def test_lb_low_priority_sets_and_restores():
    """lb_low_priority() 把当前线程标记为 prewarm，退出后恢复。"""
    from chanlun.exchange.exchange_cq import lb_low_priority, _lb_call_priority

    assert getattr(_lb_call_priority, "value", "interactive") == "interactive"
    with lb_low_priority():
        assert _lb_call_priority.value == "prewarm"
    assert _lb_call_priority.value == "interactive"


def test_fetch_candlesticks_selects_limiter_by_priority(monkeypatch):
    """_fetch_candlesticks_api 按 priority 选限流桶：prewarm→prewarm_rate_limiter，否则交互桶。"""
    import chanlun.exchange.exchange_cq as cqmod

    used = []

    class _Limiter:
        def __init__(self, tag):
            self.tag = tag

        def wait(self):
            used.append(self.tag)

    class _StubTracker:
        def is_exhausted(self, limit):
            return False

        def has_symbol(self, s):
            return True

        def add_symbol(self, s):
            pass

        def count(self):
            return 0

    monkeypatch.setattr(cqmod.LbQuotaTracker, "instance", classmethod(lambda cls: _StubTracker()))

    class _Ex:
        _fetch_candlesticks_api = cqmod.ExchangeChangQiao._fetch_candlesticks_api

        def __init__(self):
            self.rate_limiter = _Limiter("interactive")
            self.prewarm_rate_limiter = _Limiter("prewarm")

        def _quote_call(self, fn, timeout=None):
            return ["candle"]  # 跳过真实 longbridge

        def _quote_ctx(self):
            return None

    ex = _Ex()
    ex._fetch_candlesticks_api("TSLA.US", None, None, 1000, None, None, priority="prewarm")
    ex._fetch_candlesticks_api("TSLA.US", None, None, 1000, None, None, priority="interactive")
    assert used == ["prewarm", "interactive"]


def test_rate_limiter_throttles_after_fairness_fix():
    """锁外 sleep 后限流仍正确：calls=2 立即放行 2 个，第 3 个等约一个 period。"""
    from chanlun.exchange.exchange_cq import RateLimiter

    rl = RateLimiter(calls=2, period=0.3)
    t0 = time.time()
    rl.wait()
    rl.wait()
    assert time.time() - t0 < 0.15, "前 2 个不应被限"
    rl.wait()
    assert time.time() - t0 >= 0.25, "第 3 个应等待约一个 period"
