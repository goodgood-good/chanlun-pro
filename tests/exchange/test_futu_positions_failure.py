# -*- coding: utf-8 -*-
"""富途持仓查询失败不能伪装成已确认空仓；全程使用离线交易上下文替身。"""

import sys
import types

import pytest

if "futu" not in sys.modules:
    futu_stub = types.ModuleType("futu")
    futu_stub.OpenSecTradeContext = object
    futu_stub.OpenQuoteContext = object
    futu_stub.TrdMarket = type("TrdMarket", (), {"HK": "HK"})
    futu_stub.RET_OK = "RET_OK"
    futu_stub.SecurityType = type("SecurityType", (), {"STOCK": "STOCK"})
    sys.modules["futu"] = futu_stub

from chanlun.exchange import exchange_futu  # noqa: E402
from chanlun.exchange.exchange_futu import ExchangeFutu  # noqa: E402
from chanlun.trader.trader_hk_stock import TraderHKStock  # noqa: E402
from chanlun.trading.base import Operation, POSITION  # noqa: E402


class _FakeTradeContext:
    def __init__(self, ret, data):
        self.ret = ret
        self.data = data

    def position_list_query(self, *, code):
        return self.ret, self.data


def test_positions_query_failure_raises_instead_of_confirming_flat(monkeypatch):
    """非 RET_OK 是未知状态，必须抛错，不能返回代表确认空仓的空列表。"""
    context = _FakeTradeContext("RET_ERROR", "OpenD disconnected")
    monkeypatch.setattr(exchange_futu, "TTX", lambda: context)

    with pytest.raises(RuntimeError, match="Futu position query failed"):
        ExchangeFutu.positions(None, "HK.00700")


class _FakeExchange:
    """只允许持仓查询；任何下单调用都会被计数并令测试失败。"""

    def __init__(self):
        self.order_calls = 0

    def positions(self, code=""):
        return ExchangeFutu.positions(None, code)

    def order(self, *args, **kwargs):
        self.order_calls += 1
        raise AssertionError("position query failure must not place an order")


@pytest.mark.parametrize(
    ("close_method", "mmd"),
    [("close_buy", "1buy"), ("close_sell", "1sell")],
)
def test_close_stops_without_fake_fill_or_order_when_position_query_fails(
    monkeypatch, close_method, mmd
):
    """查询失败必须令港股平仓返回 False，且不得制造本地成交或调用真实下单路径。"""
    context = _FakeTradeContext("RET_ERROR", "OpenD disconnected")
    monkeypatch.setattr(exchange_futu, "TTX", lambda: context)

    trader = TraderHKStock.__new__(TraderHKStock)
    fake_exchange = _FakeExchange()
    trader.ex = fake_exchange
    trader.log = None
    alerts = []
    trader._safe_alert = lambda *args, **kwargs: alerts.append(args)
    position = POSITION(code="HK.00700", mmd=mmd, price=300.0, amount=100)
    operation = Operation("HK.00700", "sell", mmd)

    result = getattr(trader, close_method)("HK.00700", position, operation)

    assert result is False
    assert fake_exchange.order_calls == 0
    assert len(alerts) == 1


@pytest.mark.parametrize(
    ("open_method", "opt", "mmd"),
    [
        ("open_buy", "buy", "1buy"),
        ("open_sell", "sell", "1sell"),
    ],
)
def test_open_stops_when_initial_position_query_fails(
    monkeypatch, open_method, opt, mmd
):
    """首次全量持仓查询失败也应安全返回，不能抛断交易循环。"""
    context = _FakeTradeContext("RET_ERROR", "OpenD disconnected")
    monkeypatch.setattr(exchange_futu, "TTX", lambda: context)

    trader = TraderHKStock.__new__(TraderHKStock)
    fake_exchange = _FakeExchange()
    trader.ex = fake_exchange
    trader.b_space = 3
    trader.log = None
    alerts = []
    trader._safe_alert = lambda *args, **kwargs: alerts.append(args)

    result = getattr(trader, open_method)(
        "HK.00700", Operation("HK.00700", opt, mmd)
    )

    assert result is False
    assert fake_exchange.order_calls == 0
    assert len(alerts) == 1


class _OpenQueryRaceExchange:
    """首次全量快照已有目标仓，随后按代码确认查询失败。"""

    def __init__(self, first_positions=None):
        self.position_calls = 0
        self.order_calls = 0
        self.first_positions = (
            [{"code": "HK.00700", "amount": 100}]
            if first_positions is None
            else first_positions
        )

    def positions(self, code=""):
        self.position_calls += 1
        if self.position_calls == 1:
            return self.first_positions
        raise RuntimeError("second position query failed")

    def stock_info(self, code):
        return {"code": code, "name": "腾讯控股", "lot_size": 100}

    def can_trade_val(self, code):
        return {"max_margin_buy": 1000, "max_margin_short": 1000}

    def order(self, *args, **kwargs):
        self.order_calls += 1
        # 避免触发任何消息/数据库副作用；旧实现会走到这里后按未成交返回 False。
        return {"dealt_amount": 0}


@pytest.mark.parametrize(
    ("open_method", "opt", "mmd"),
    [
        ("open_buy", "buy", "1buy"),
        ("open_sell", "sell", "1sell"),
    ],
)
def test_open_does_not_order_when_second_position_confirmation_fails(
    open_method, opt, mmd
):
    """任一持仓确认查询失败都必须 fail-closed，不能重复开真实仓。"""
    trader = TraderHKStock.__new__(TraderHKStock)
    fake_exchange = _OpenQueryRaceExchange()
    trader.ex = fake_exchange
    trader.b_space = 3
    trader.log = None
    alerts = []
    trader._safe_alert = lambda *args, **kwargs: alerts.append(args)

    result = getattr(trader, open_method)(
        "HK.00700", Operation("HK.00700", opt, mmd)
    )

    assert result is False
    assert fake_exchange.position_calls >= 1
    assert fake_exchange.order_calls == 0
    assert len(alerts) == 1


@pytest.mark.parametrize(
    ("open_method", "opt", "mmd"),
    [
        ("open_buy", "buy", "1buy"),
        ("open_sell", "sell", "1sell"),
    ],
)
def test_open_fails_closed_when_code_confirmation_query_fails(open_method, opt, mmd):
    """全量快照为空后，按代码确认失败也不得继续下单。"""
    trader = TraderHKStock.__new__(TraderHKStock)
    fake_exchange = _OpenQueryRaceExchange(first_positions=[])
    trader.ex = fake_exchange
    trader.b_space = 3
    trader.log = None
    alerts = []
    trader._safe_alert = lambda *args, **kwargs: alerts.append(args)

    result = getattr(trader, open_method)(
        "HK.00700", Operation("HK.00700", opt, mmd)
    )

    assert result is False
    assert fake_exchange.position_calls == 2
    assert fake_exchange.order_calls == 0
    assert len(alerts) == 1
