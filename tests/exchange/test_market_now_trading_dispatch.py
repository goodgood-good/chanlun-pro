"""Every exchange implements the single market-aware session contract."""

from pathlib import Path

from chanlun.exchange import Market, market_now_trading


class _MarketAwareExchange:
    def __init__(self):
        self.received = None

    def now_trading(self, market: str):
        self.received = market
        return market == "hk"


class _UnknownExchange:
    def now_trading(self, market: str):
        return None


def test_market_now_trading_always_dispatches_market():
    aware = _MarketAwareExchange()
    assert market_now_trading(aware, Market.HK) is True
    assert aware.received == "hk"


def test_market_now_trading_preserves_unknown_state():
    assert market_now_trading(_UnknownExchange(), Market.A) is None


def test_market_sensitive_callers_use_shared_dispatcher():
    root = Path(__file__).resolve().parents[2]
    expected = {
        "web/chanlun_chart/cl_app/blueprints/other.py": (
            "market_now_trading(ex, market)",
        ),
        "web/chanlun_chart/cl_app/services/holding_group_monitor.py": (
            "market_now_trading(exchange, market)",
        ),
    }
    for rel, needles in expected.items():
        source = (root / rel).read_text(encoding="utf-8")
        for needle in needles:
            assert needle in source, f"{rel} 未通过共享入口传递 market"
