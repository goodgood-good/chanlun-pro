"""Round8 BUG-2: 多日加仓时 pos.open_date 应更新为最近加仓日期(用于 T+1 判定),
不冻结在首笔。否则跨日加仓后当日加仓的股当天即可平仓 -> 绕过 A股 T+1 -> 高估可达收益。
默认 pos_rate=1 策略单笔满仓不加仓 -> open_date 仍=首笔日, 零行为变更。"""

import datetime

from chanlun.trading.backtest_trader import BackTestTrader
from chanlun.trading.base import Operation


class _FakeDatas:
    def __init__(self):
        self.now_date = datetime.datetime(2024, 1, 1, 9, 30, 0)
        self._px = {"open": 10.0, "close": 10.0, "high": 10.0, "low": 10.0}

    def last_k_info(self, code):
        return dict(self._px)


def test_multiday_add_updates_open_date_for_t1():
    t = BackTestTrader("t", mode="trade", market="a", init_balance=100000, fee_rate=0.0)
    t.datas = _FakeDatas()

    def fixed_open_buy(code, opt, amount=None):
        return {"price": t.datas._px["close"], "amount": 100.0}

    t.open_buy = fixed_open_buy

    # tranche 1: day1
    t.datas.now_date = datetime.datetime(2024, 1, 1, 9, 30, 0)
    t.execute("X", Operation(code="X", opt="buy", mmd="1buy", pos_rate=0.5, key="k1", open_uid="X:1buy"))
    assert t.positions["X:1buy"].open_date == "2024-01-01"

    # tranche 2 (加仓): day2
    t.datas.now_date = datetime.datetime(2024, 1, 2, 9, 30, 0)
    t.execute("X", Operation(code="X", opt="buy", mmd="1buy", pos_rate=0.5, key="k2", open_uid="X:1buy"))
    # BUG 前: open_date 冻结在 2024-01-01 -> day2 当日加仓股可当日平(绕过 T+1)
    assert t.positions["X:1buy"].open_date == "2024-01-02"