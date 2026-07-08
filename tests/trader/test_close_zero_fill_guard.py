"""Round11 N4: 平仓时券商实际成交量<=0(市价单被拒/未成交, 如富途 dealt_amount=0)返回
{amount:0}(非 False), 被 execute 当成功: clear 分支 now_pos_rate 全额扣到0而 amount 仍满,
下一次 execute 顶部守卫(now_pos_rate==0 → return True)从此永久跳过该仓所有平仓 →
裸持失管(该止损的止不住)。修复: 实际成交量<=0 视为平仓失败 return False, 不减仓。"""

import datetime

from chanlun.trading.backtest_trader import BackTestTrader
from chanlun.trading.base import POSITION, Operation


class _FakeDatas:
    def __init__(self):
        self.now_date = datetime.datetime(2024, 1, 2, 10, 0, 0)


def _long_trader():
    t = BackTestTrader("t", mode="trade", market="us", init_balance=100000, fee_rate=0.0)
    t.datas = _FakeDatas()
    return t


def _long_pos():
    pos = POSITION(code="X", mmd="1buy", amount=100.0, price=10.0)
    pos.now_pos_rate = 1.0
    pos.balance = 1000.0
    pos.type = "做多"
    return pos


def test_close_zero_fill_does_not_corrupt_position():
    """close_buy 返回 amount=0(拒单) → 仓位状态不被破坏(now_pos_rate/amount 不失同步)。"""
    t = _long_trader()
    pos = _long_pos()
    t.close_buy = lambda code, position, opt: {"price": 10.0, "amount": 0.0}
    t.execute("X", Operation("X", "sell", "1buy", pos_rate=1.0, key="ck1"), pos)
    # 修复前 bug: now_pos_rate 被扣到 0 而 amount 仍 100
    assert pos.now_pos_rate == 1.0
    assert pos.amount == 100.0


def test_close_zero_fill_then_valid_close_still_works():
    """裸持失管核心: 一次拒单(amount=0)后, 后续正常平仓仍能平掉(不被守卫永久跳过)。"""
    t = _long_trader()
    pos = _long_pos()
    t.close_buy = lambda code, position, opt: {"price": 10.0, "amount": 0.0}
    t.execute("X", Operation("X", "sell", "1buy", pos_rate=1.0, key="ck1"), pos)
    t.close_buy = lambda code, position, opt: {"price": 10.0, "amount": 100.0}
    t.execute("X", Operation("X", "sell", "1buy", pos_rate=1.0, key="ck2"), pos)
    # 修复前: 首次拒单已把 now_pos_rate 扣到0 → 守卫永久跳过 → amount 恒 100
    assert pos.amount == 0.0