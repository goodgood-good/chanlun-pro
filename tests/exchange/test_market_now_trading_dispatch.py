"""市场感知的交易时段判断必须把 HK/US 传给支持该参数的适配器。"""

from pathlib import Path

from chanlun.exchange import Market, market_now_trading


class _MarketAwareExchange:
    def __init__(self):
        self.received = None

    def now_trading(self, market="us"):
        self.received = market
        return market == "hk"


class _LegacyExchange:
    def __init__(self):
        self.calls = 0

    def now_trading(self):
        self.calls += 1
        return True


class _LegacyUnknownExchange:
    def now_trading(self):
        return None


def test_market_now_trading_dispatches_market_only_when_supported():
    aware = _MarketAwareExchange()
    assert market_now_trading(aware, Market.HK) is True
    assert aware.received == "hk"

    legacy = _LegacyExchange()
    assert market_now_trading(legacy, Market.HK) is True
    assert legacy.calls == 1


def test_market_now_trading_preserves_legacy_unknown_state():
    """ExchangeDB 用 None 表示未知，不能被共享分派器改写成明确休市。"""
    assert market_now_trading(_LegacyUnknownExchange(), Market.A) is None


def test_market_sensitive_callers_use_shared_dispatcher():
    root = Path(__file__).resolve().parents[2]
    expected = {
        "src/chanlun/signal_monitor/scheduler.py": (
            "market_now_trading(ex, task.market)",
        ),
        "web/chanlun_chart/cl_app/alert_tasks.py": (
            "market_now_trading(ex, alert_config.market)",
        ),
        "web/chanlun_chart/cl_app/blueprints/other.py": (
            "market_now_trading(ex, market)",
        ),
        "src/chanlun/recursive_bt/monitor/live_monitor.py": (
            "market_now_trading(ex, market)",
        ),
    }
    for rel, needles in expected.items():
        source = (root / rel).read_text(encoding="utf-8")
        for needle in needles:
            assert needle in source, f"{rel} 未通过共享入口传递 market"
