"""Round11 N5: Binance order() 规范化返回 {price:成交均价, amount:成交量}。

ccxt 市价单 create_order 典型返回 price=None(成交均价在 average)、amount=请求量(成交量在
filled), 原样返回致 trader_currency 记 None 价污染账本(msg/db.order_save)+ 平仓分支
res['price']*res['amount'] None 算术 TypeError。ccxt 未装, 注入 stub 后 object.__new__ 绕
__init__ 测 order() 纯规范化逻辑。★真 Binance 需灰度验证 create_order 返回口径与 mock 一致。
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