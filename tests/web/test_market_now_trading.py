"""锁定 market_now_trading 的 TTL 缓存(方向2 的交易时段判断入口)。

tv_history 是最热路径,每次请求都判一次交易时段;用短 TTL 缓存避免反复构造
exchange / 调 now_trading。exchange 取数或判断异常时保守按「交易中」(短阈值、
更倾向后台刷新到最新),不让一次异常把图表钉死在旧快照。
"""
from cl_app.services import chart_compute


class _FakeEx:
    def __init__(self, trading):
        self._trading = trading
        self.calls = 0

    def now_trading(self):
        self.calls += 1
        return self._trading


def test_market_now_trading_caches_within_ttl(monkeypatch):
    fake = _FakeEx(trading=True)
    monkeypatch.setattr(chart_compute, "get_exchange", lambda m: fake)
    chart_compute._trading_state_cache.clear()
    t0 = 1000.0
    assert chart_compute.market_now_trading("a", now=t0) is True
    assert chart_compute.market_now_trading("a", now=t0 + 5) is True  # TTL 内
    assert fake.calls == 1  # 只问了一次 exchange


def test_market_now_trading_refreshes_after_ttl(monkeypatch):
    fake = _FakeEx(trading=False)
    monkeypatch.setattr(chart_compute, "get_exchange", lambda m: fake)
    chart_compute._trading_state_cache.clear()
    t0 = 2000.0
    assert chart_compute.market_now_trading("a", now=t0) is False
    assert chart_compute.market_now_trading("a", now=t0 + 999) is False  # 超 TTL
    assert fake.calls == 2


def test_market_now_trading_error_defaults_trading(monkeypatch):
    def boom(_m):
        raise RuntimeError("ex init failed")

    monkeypatch.setattr(chart_compute, "get_exchange", boom)
    chart_compute._trading_state_cache.clear()
    # 异常 → 保守按「交易中」(短阈值,更倾向刷新到最新)
    assert chart_compute.market_now_trading("a", now=1.0) is True
