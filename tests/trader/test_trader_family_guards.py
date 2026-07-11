# -*- coding: utf-8 -*-
"""R19 trader 层守卫:
- trader_a_stock.open_buy: 固定 5 万预算对高价股整手取整必得 0 股, 不得伪造买入(通知+自选+落库)。
- trader_futures.close_buy/close_sell: 券商已无仓的 M1 快速返回路径须清"我的持仓"自选防幽灵滞留
  (正常平仓路径有 zx.del, 快速返回路径原遗漏)。
均 realmoney/neither 路径, mock 单测, 真金灰度留用户。
"""
import datetime  # noqa: F401
import sys
import types

# trader_futures → exchange_tq 需 tqsdk(futures extra, 未装)→ 万能桩(__getattr__ 解析任意
# tqsdk.X 属性, 覆盖 class-body 注解如 g_api: tqsdk.TqApi)。须在 import trader_futures 前。
class _AnyModule(types.ModuleType):
    def __getattr__(self, name):
        v = type(name, (), {})
        setattr(self, name, v)
        return v


if "tqsdk" not in sys.modules:
    _tq = _AnyModule("tqsdk")
    _objs = _AnyModule("tqsdk.objs")
    _tq.objs = _objs
    sys.modules["tqsdk"] = _tq
    sys.modules["tqsdk.objs"] = _objs

from chanlun.trader import trader_a_stock, trader_futures  # noqa: E402
from chanlun.trading.base import POSITION, Operation  # noqa: E402


class _Tick:
    def __init__(self, last):
        self.last = last


class _RecZx:
    def __init__(self):
        self.added = []
        self.deled = []

    def add_stock(self, *a, **k):
        self.added.append(a)

    def del_stock(self, *a, **k):
        self.deled.append(a)


# ---------- trader_a_stock.open_buy 高价股 0 股守卫 ----------
def _a_trader(price):
    t = trader_a_stock.TraderAStock.__new__(trader_a_stock.TraderAStock)
    t.ex = type(
        "E",
        (),
        {
            "ticks": lambda self, codes: {codes[0]: _Tick(price)},
            "stock_info": lambda self, code: {"code": code, "name": "测试股"},
        },
    )()
    t.zx = _RecZx()
    return t


def test_a_open_buy_high_price_zero_lot_rejected(monkeypatch):
    """茅台类高价股(price=1700): 50000/1700=29.4 → 整手取整 0 股 → 返 False, 不加自选/不落库。"""
    t = _a_trader(price=1700.0)
    monkeypatch.setattr(trader_a_stock.utils, "send_fs_msg", lambda *a, **k: None)
    db_saves = []
    monkeypatch.setattr(trader_a_stock.db, "order_save", lambda *a, **k: db_saves.append(a))
    res = t.open_buy("SH.600519", Operation("SH.600519", "buy", "1buy", key="k1"))
    assert res is False, "高价股 0 股必须返 False"
    assert t.zx.added == [], "0 股不得加自选"
    assert db_saves == [], "0 股不得落库"


def test_a_open_buy_normal_price_ok(monkeypatch):
    """回归: 低价股(price=10)→ 50000/10=5000 股, 正常开仓, 守卫不误伤。"""
    t = _a_trader(price=10.0)
    monkeypatch.setattr(trader_a_stock.utils, "send_fs_msg", lambda *a, **k: None)
    monkeypatch.setattr(trader_a_stock.db, "order_save", lambda *a, **k: None)
    res = t.open_buy("SH.600000", Operation("SH.600000", "buy", "1buy", key="k1"))
    assert res is not False
    assert res["amount"] == 5000


# ---------- trader_futures close M1 快速返回清自选 ----------
def _fut_trader():
    t = trader_futures.TraderFutures.__new__(trader_futures.TraderFutures)
    t.zx = _RecZx()
    t._safe_alert = lambda *a, **k: None
    t.query_broker_position = lambda code: ("ok", {})  # 券商无持仓 → 快速返回
    return t


def _pos(mmd):
    pos = POSITION(code="rb2510", mmd=mmd)
    pos.price = 3500.0
    pos.amount = 1
    return pos


def test_futures_close_buy_broker_empty_clears_zx():
    """券商已无多仓的快速返回路径须清自选(否则幽灵标的永久滞留)。"""
    t = _fut_trader()
    res = t.close_buy("rb2510", _pos("1buy"), Operation("rb2510", "sell", "1buy", key="k1"))
    assert res is not False
    assert t.zx.deled == [("我的持仓", "rb2510")], "快速返回须清自选"


def test_futures_close_sell_broker_empty_clears_zx():
    t = _fut_trader()
    res = t.close_sell("rb2510", _pos("1sell"), Operation("rb2510", "buy", "1sell", key="k1"))
    assert res is not False
    assert t.zx.deled == [("我的持仓", "rb2510")]
