"""D9-#3 [LOW]: 数字货币 open_buy/open_sell 飞书通知「数量」字段误用 open_usdt(USDT 保证金)
而非 res['amount'](实际成交币数)。账本 db.order_save 用的是 res['amount'](正确), 仅通知误导。
修复: msg 的「数量」改用 res['amount']。纯通知文本, 不动下单/账本逻辑。

exchange_binance 依赖 ccxt(本环境未装), 注入 bare stub 后 __new__ 绕 __init__ 直测 open_*。
"""
import sys
import types

if "ccxt" not in sys.modules:
    sys.modules["ccxt"] = types.ModuleType("ccxt")

from chanlun.trader import trader_currency  # noqa: E402
from chanlun.trading.base import Operation  # noqa: E402


class _FakeTick:
    def __init__(self, last):
        self.last = last


class _FakeZx:
    def add_stock(self, *a, **k):
        return None


class _FakeBnEx:
    def positions(self, code=""):
        return []

    def balance(self):
        return {"free": 8000.0}

    def ticks(self, codes):
        return {codes[0]: _FakeTick(last=100.0)}

    def order(self, code, o_type, amount, args=None):
        # 实际成交币数 0.5, 明显区别于 open_usdt(=980) 与 请求量(=19.6)
        return {"price": 100.0, "amount": 0.5}


def _mk(monkeypatch, captured):
    t = trader_currency.TraderCurrency.__new__(trader_currency.TraderCurrency)
    t.ex = _FakeBnEx()
    t.poss_max = 8
    t.leverage = 2
    t.zx = _FakeZx()
    t.log = None
    t._broker_already_holds = lambda code: False
    t.risk_precheck = lambda *a, **k: (True, "")
    monkeypatch.setattr(trader_currency.utils, "send_fs_msg",
                        lambda market, title, msg, *a, **k: captured.append(msg))
    monkeypatch.setattr(trader_currency.db, "order_save", lambda *a, **k: None)
    return t


def test_open_buy_msg_uses_coin_amount_not_usdt(monkeypatch):
    captured = []
    t = _mk(monkeypatch, captured)
    res = t.open_buy("BTC/USDT", Operation("BTC/USDT", "buy", "1buy", key="k1"))
    assert res == {"price": 100.0, "amount": 0.5}
    assert len(captured) == 1
    assert "数量 0.5" in captured[0]      # res['amount'] 实际成交币数
    assert "980" not in captured[0]        # 不再泄漏 open_usdt(USDT 保证金)


def test_open_sell_msg_uses_coin_amount_not_usdt(monkeypatch):
    captured = []
    t = _mk(monkeypatch, captured)
    res = t.open_sell("BTC/USDT", Operation("BTC/USDT", "buy", "1sell", key="k1"))
    assert res == {"price": 100.0, "amount": 0.5}
    assert len(captured) == 1
    assert "数量 0.5" in captured[0]
    assert "980" not in captured[0]