"""D9-#1 [HIGH]: 港股 open_buy/open_sell 券商受理但未成交(富途 dealt_amount=0, 市价单挂单/
被拒仍 ret==RET_OK -> order 是 truthy dict)时须视为开仓失败 return False, 不记本地仓。
否则 execute 记 now_pos_rate>0 而 amount=0 的幽灵仓 -> run() 跳过 amount==0 仓 + execute
平仓守卫(pos.amount==0.0 -> return True)永不平 -> 券商后续成交=裸持无止损。镜像平仓侧
Round11 N4(backtest_trader.py:1129/1260)。

exchange_futu 依赖 futu SDK(本环境未装), 注入最小 stub(仅 OpenSecTradeContext 供 TTX
返回注解 def 时求值)后 object.__new__ 绕 __init__ 直测 open_buy/open_sell 纯逻辑。
★frozen: 真富途下单口径需灰度验证; 本测仅覆盖零成交守卫, 不连柜台。
"""
import sys
import types

if "futu" not in sys.modules:
    _fm = types.ModuleType("futu")
    _fm.OpenSecTradeContext = object
    sys.modules["futu"] = _fm

from chanlun.trader import trader_hk_stock  # noqa: E402
from chanlun.trading.base import Operation  # noqa: E402


class _FakeZx:
    def add_stock(self, *a, **k):
        return None


class _FakeFutuEx:
    """最小富途交易所替身: order 返回可控 dealt_amount。"""

    def __init__(self, dealt_amount):
        self._dealt = dealt_amount

    def positions(self, code=""):
        return []

    def stock_info(self, code):
        return {"code": code, "name": "测试股", "lot_size": 100}

    def can_trade_val(self, code):
        return {"max_margin_buy": 100000.0, "max_margin_short": 100000.0}

    def order(self, code, o_type, amount):
        # ret==RET_OK 但未成交: dealt_amount=0(挂单/被拒), 仍是 truthy dict
        return {"dealt_avg_price": 10.0, "dealt_amount": self._dealt}


def _mk(dealt_amount, monkeypatch):
    t = trader_hk_stock.TraderHKStock.__new__(trader_hk_stock.TraderHKStock)
    t.ex = _FakeFutuEx(dealt_amount)
    t.b_space = 3
    t.zx = _FakeZx()
    t.log = None
    t._broker_already_holds = lambda code: False
    t._safe_alert = lambda *a, **k: None
    monkeypatch.setattr(trader_hk_stock.utils, "send_fs_msg", lambda *a, **k: None)
    monkeypatch.setattr(trader_hk_stock.db, "order_save", lambda *a, **k: None)
    return t


def test_open_buy_zero_fill_returns_false(monkeypatch):
    t = _mk(0, monkeypatch)
    res = t.open_buy("HK.00700", Operation("HK.00700", "buy", "1buy", key="k1"))
    assert res is False


def test_open_sell_zero_fill_returns_false(monkeypatch):
    t = _mk(0, monkeypatch)
    res = t.open_sell("HK.00700", Operation("HK.00700", "buy", "1sell", key="k1"))
    assert res is False


def test_open_buy_filled_returns_position(monkeypatch):
    t = _mk(200, monkeypatch)
    res = t.open_buy("HK.00700", Operation("HK.00700", "buy", "1buy", key="k1"))
    assert res == {"price": 10.0, "amount": 200}


def test_open_sell_filled_returns_position(monkeypatch):
    t = _mk(200, monkeypatch)
    res = t.open_sell("HK.00700", Operation("HK.00700", "buy", "1sell", key="k1"))
    assert res == {"price": 10.0, "amount": 200}