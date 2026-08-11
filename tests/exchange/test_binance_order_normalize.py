"""Round11 N5: Binance order() 规范化返回 {price:成交均价, amount:成交量}。

ccxt 市价单 create_order 典型返回 price=None(成交均价在 average)、amount=请求量(成交量在
filled)。适配器必须返回真实成交均价与成交量。ccxt 未装时注入 stub，并通过 object.__new__
绕过 __init__ 测试纯规范化逻辑。
"""

import sys
import types

if "ccxt" not in sys.modules:
    sys.modules["ccxt"] = types.ModuleType("ccxt")

from chanlun.exchange.exchange_binance import ExchangeBinance  # noqa: E402


def _binance(create_result):
    # @fun.singleton 把类包成函数, 原类在 __wrapped__(functools.wraps 设置)
    ex = object.__new__(ExchangeBinance.__wrapped__)

    class _Fake:
        def set_leverage(self, *a, **k):
            return None

        def create_order(self, **k):
            return create_result

    ex.exchange = _Fake()
    return ex


def test_binance_order_normalizes_market_price():
    """price=None(市价单)→ 取 average 成交均价, 非 None。"""
    res = _binance(
        {"id": "1", "price": None, "amount": 0.5, "filled": 0.5, "average": 60000.0}
    ).order("BTC/USDT", "open_long", 0.5, {"leverage": 1})
    assert res["price"] == 60000.0
    assert res["price"] * res["amount"] == 30000.0  # 下游算术不再 TypeError


def test_binance_order_partial_fill_uses_filled_amount():
    """amount 用实际成交量 filled(非请求量)。"""
    res = _binance(
        {"id": "2", "price": None, "amount": 1.0, "filled": 0.3, "average": 100.0}
    ).order("BTC/USDT", "open_long", 1.0, {"leverage": 1})
    assert res["amount"] == 0.3
    assert res["price"] == 100.0
