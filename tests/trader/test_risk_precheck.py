"""M5: risk_precheck 各分支; 默认阈值 None 时恒通过 (行为不变)。"""

from chanlun.trading.backtest_trader import BackTestTrader
from chanlun.trading.base import Operation


def _trader():
    return BackTestTrader("t", mode="trade", market="currency")


def _opt():
    return Operation("BTC", "open", "1buy")


def test_default_thresholds_none_always_ok():
    """关键安全属性: 默认阈值全 None → 恒 ok=True, 与现状完全一致。"""
    tr = _trader()
    ok, reason = tr.risk_precheck("BTC", _opt(), est_price=100, est_amount=1, est_notional=100)
    assert ok is True
    assert reason == ""


def test_nan_price_rejected():
    tr = _trader()
    ok, reason = tr.risk_precheck("BTC", _opt(), float("nan"), 1, 100)
    assert ok is False
    assert "价格非法" in reason


def test_zero_price_rejected():
    tr = _trader()
    ok, _ = tr.risk_precheck("BTC", _opt(), 0, 1, 100)
    assert ok is False


def test_none_price_rejected():
    tr = _trader()
    ok, _ = tr.risk_precheck("BTC", _opt(), None, 1, 100)
    assert ok is False


def test_zero_amount_rejected():
    tr = _trader()
    ok, reason = tr.risk_precheck("BTC", _opt(), 100, 0, 100)
    assert ok is False
    assert "数量" in reason


def test_below_min_order_notional_rejected():
    tr = _trader()
    tr.min_order_notional = 50
    ok, reason = tr.risk_precheck("BTC", _opt(), 100, 1, 10)  # notional 10 < 50
    assert ok is False
    assert "最小下单额" in reason


def test_over_single_notional_rejected():
    tr = _trader()
    tr.max_single_notional = 1000
    ok, reason = tr.risk_precheck("BTC", _opt(), 100, 1, 5000)  # 5000 > 1000
    assert ok is False
    assert "超单笔上限" in reason


def test_within_single_notional_ok():
    tr = _trader()
    tr.max_single_notional = 1000
    ok, _ = tr.risk_precheck("BTC", _opt(), 100, 1, 500)
    assert ok is True


def test_over_total_notional_rejected(monkeypatch):
    tr = _trader()
    tr.max_total_notional = 1000

    # mock 已持仓 balance 合计 800
    class _P:
        balance = 800

    monkeypatch.setattr(tr, "hold_positions", lambda: [_P()])
    ok, reason = tr.risk_precheck("BTC", _opt(), 100, 1, 300)  # 800+300=1100 > 1000
    assert ok is False
    assert "超组合总上限" in reason


def test_within_total_notional_ok(monkeypatch):
    tr = _trader()
    tr.max_total_notional = 1000
    monkeypatch.setattr(tr, "hold_positions", lambda: [])
    ok, _ = tr.risk_precheck("BTC", _opt(), 100, 1, 300)
    assert ok is True
