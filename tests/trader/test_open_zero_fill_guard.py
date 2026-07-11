# -*- coding: utf-8 -*-
"""R16-C1 (HIGH realmoney): execute() 开仓分支缺平仓侧已有的零成交守卫(Round11 N4)。

exchange_binance.order() 对 MARKET 单永不返 False(只 N5 归一化 filled/amount), 交易所受理但
0 成交(status=REJECTED/EXPIRED 带 executedQty=0, 或极端行情 0 fill)时返 {price,amount:0} 真值
dict。trader_currency.open_buy/open_sell 只判 `if res is False` 放行 → execute():939/1025 后也只
判 `if res is False`(缺 CLOSE 侧 :1129 的 `if res.get("amount",0)<=0: return False`)→ now_pos_rate
照增+open_keys 标记+假开仓记录, 已发飞书"开仓成功"+db假成交+zx.add_stock 自选加仓不可撤。
可达=reboot_trader_currency 无条件 while True 真实 Binance 下单(CTP N1/N2·Binance N5 同档)。

修复=两 OPEN 分支补 `if self.mode == "trade" and res.get("amount",0)<=0: return False`(base 类
一处覆盖 currency/futures/ctp)。限 trade: signal 回测的 amount=0(高价股整手取整,如茅台
价 1700 时 int((100000/1700)*0.99/100)*100=0)是既有口径, 不改以保 golden/回测基线字节不变。
realmoney: mock 单测验证, 真金灰度留用户。
"""
import datetime

from chanlun.trading.backtest_trader import BackTestTrader
from chanlun.trading.base import Operation


class _FakeDatas:
    def __init__(self):
        self.now_date = datetime.datetime(2024, 1, 2, 10, 0, 0)


def _trader(market):
    t = BackTestTrader("t", mode="trade", market=market, init_balance=100000, fee_rate=0.0)
    t.datas = _FakeDatas()
    return t


def test_open_buy_zero_fill_not_faked_as_success():
    """做多零成交 → execute 返 False, 新建仓 now_pos_rate/amount 不增, 不标 open_key。"""
    t = _trader("us")
    t.open_buy = lambda code, opt: {"price": 10.0, "amount": 0.0}  # Binance 零成交
    result = t.execute("X", Operation("X", "buy", "1buy", pos_rate=1.0, key="ok1"), None)
    assert result is False
    pos = t.positions["X:1buy"]  # execute 在 open_buy 前已建 pos
    assert pos.now_pos_rate == 0.0, "零成交不得增 now_pos_rate"
    assert pos.amount == 0.0
    assert "ok1" not in pos.open_keys


def test_open_sell_zero_fill_not_faked_as_success():
    """做空(currency can_short)零成交 → 同样不当成功。"""
    t = _trader("currency")
    t.open_sell = lambda code, opt: {"price": 10.0, "amount": 0.0}
    result = t.execute("X", Operation("X", "buy", "1sell", pos_rate=1.0, key="ok2"), None)
    assert result is False
    pos = t.positions["X:1sell"]
    assert pos.now_pos_rate == 0.0
    assert pos.amount == 0.0
    assert "ok2" not in pos.open_keys


def test_open_buy_normal_fill_still_works():
    """回归: 正常成交 amount>0 仍正常开仓。"""
    t = _trader("us")
    t.open_buy = lambda code, opt: {"price": 10.0, "amount": 100.0}
    t.execute("X", Operation("X", "buy", "1buy", pos_rate=1.0, key="ok3"), None)
    pos = t.positions["X:1buy"]
    assert pos.now_pos_rate == 1.0
    assert pos.amount == 100.0


def test_signal_mode_zero_amount_not_rejected():
    """基线中立: signal 模式 amount=0(高价股整手取整)是既有口径, 护栏只限 trade 不介入。

    否则会改回测/信号输出(茅台等高价股 signal 回测 amount 恒取整为 0), 触信号红线。
    """
    t = BackTestTrader("t", mode="signal", market="a", init_balance=100000, fee_rate=0.0)
    t.datas = _FakeDatas()
    t.open_buy = lambda code, opt: {"price": 10.0, "amount": 0.0}
    result = t.execute("X", Operation("X", "buy", "1buy", pos_rate=1.0, key="ok4"), None)
    assert result is not False, "signal 模式不得因 amount=0 拒绝(维持既有基线相位)"
    pos = t.positions["X:1buy"]
    assert pos.now_pos_rate == 1.0, "signal 原口径: now_pos_rate 照增"