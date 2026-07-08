"""Round8 BUG-1: 加仓(同 open_uid 多次不同 key 开仓)时 pos.price 应为持仓量加权均价,
不覆盖为最后一笔成交价。否则 position_record 的 now_profit=(close-pos.price)*总amount
与真浮盈不符, 并污染 max_profit_rate/max_loss_rate → 移动止损/保本触发时机变 + trade 模式
权益曲线失真。默认 pos_rate=1 策略单笔满仓不触发(now_pos_rate>=1 早退), 此处用分数
pos_rate 加仓复现。"""

import datetime

from chanlun.trading.backtest_trader import BackTestTrader
from chanlun.trading.base import Operation


class _FakeDatas:
    def __init__(self):
        self.now_date = datetime.datetime(2024, 1, 1, 9, 30, 0)
        self._px = {"open": 10.0, "close": 10.0, "high": 10.0, "low": 10.0}

    def last_k_info(self, code):
        return dict(self._px)


def _trader_two_tranches():
    t = BackTestTrader("t", mode="trade", market="us", init_balance=100000, fee_rate=0.0)
    t.datas = _FakeDatas()

    def fixed_open_buy(code, opt, amount=None):
        return {"price": t.datas._px["close"], "amount": 100.0}

    t.open_buy = fixed_open_buy

    # tranche 1: 100 @ 10
    t.datas._px = {"open": 10.0, "close": 10.0, "high": 10.0, "low": 10.0}
    t.execute("X", Operation(code="X", opt="buy", mmd="1buy", pos_rate=0.5, key="k1", open_uid="X:1buy"))
    # tranche 2 (加仓): 100 @ 20, 同 uid 不同 key
    t.datas._px = {"open": 20.0, "close": 20.0, "high": 20.0, "low": 20.0}
    t.execute("X", Operation(code="X", opt="buy", mmd="1buy", pos_rate=0.5, key="k2", open_uid="X:1buy"))
    return t


def test_add_position_uses_weighted_average_price():
    t = _trader_two_tranches()
    pos = t.positions["X:1buy"]
    assert pos.amount == 200.0
    # 加权均价 = (100*10 + 100*20)/200 = 15.0 (BUG 前 = 20.0 最后一笔)
    assert abs(pos.price - 15.0) < 1e-9


def test_add_position_mark_to_market_profit_correct():
    t = _trader_two_tranches()
    pos = t.positions["X:1buy"]
    # 盯市价=20: 真浮盈 = 20*200 - (100*10+100*20) = 4000-3000 = 1000
    t.datas._px = {"open": 20.0, "close": 20.0, "high": 20.0, "low": 20.0}
    t.update_position_record()
    record_dt = t.get_now_datetime().strftime(t.record_dt_format)
    assert abs(t.hold_profit_history[record_dt] - 1000.0) < 1e-6