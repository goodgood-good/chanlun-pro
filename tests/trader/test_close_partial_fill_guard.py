"""R7-T1: N4(零成交守卫 test_close_zero_fill_guard)的部分成交兄弟。市价平仓券商部分成交
(0<实际<请求量, 如富途薄流动性市价单只成交一部分)时, clear 分支 now_pos_rate 按完整意图
比例扣到0而 amount 仍有残量 → 下次 execute 顶部守卫(now_pos_rate==0 → return True)永久跳过
→ 残仓裸持失管(N4 只挡零成交, 未挡部分成交)。修复: 部分成交致 now_pos_rate 归0而 amount>0
时, 按残量占满仓比例反推兜住 now_pos_rate>0, 使下轮 execute 放行继续平残仓。
★回测/paper 恒全额成交(全平 res==amount→amount→0 兜底不触发; 部分平 now_pos_rate>0 兜底不
触发)→ 此兜底为死代码, 回测逐字节不变(仅实盘 futu/tq 券商真单部分成交可现, realmoney)。"""

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


def _short_pos():
    pos = POSITION(code="X", mmd="1sell", amount=10.0, price=10.0)
    pos.now_pos_rate = 1.0
    pos.balance = 100.0
    pos.type = "做空"
    return pos


def test_close_partial_fill_keeps_rate_amount_consistent():
    # 请求平100实成60 → now_pos_rate 不归0, 与残量40保持一致(40/100=0.4)
    t = _long_trader()
    pos = _long_pos()
    t.close_buy = lambda code, position, opt: {"price": 10.0, "amount": 60.0}
    t.execute("X", Operation("X", "sell", "1buy", pos_rate=1.0, key="ck1"), pos)
    assert pos.amount == 40.0
    assert pos.now_pos_rate > 0  # 关键: 不归0(修复前=0.0 裸持)
    assert abs(pos.now_pos_rate - 0.4) < 1e-9


def test_close_partial_fill_then_close_remnant_still_works():
    # 裸持失管核心: 部分成交后, 后续平仓仍能把残仓平掉(不被守卫永久跳过)
    t = _long_trader()
    pos = _long_pos()
    t.close_buy = lambda code, position, opt: {"price": 10.0, "amount": 60.0}
    t.execute("X", Operation("X", "sell", "1buy", pos_rate=1.0, key="ck1"), pos)
    t.close_buy = lambda code, position, opt: {"price": 10.0, "amount": 40.0}
    t.execute("X", Operation("X", "sell", "1buy", pos_rate=1.0, key="ck1"), pos)
    assert pos.amount == 0.0  # 修复前: 恒 40(守卫永久跳过)


def test_close_partial_fill_same_key_only_retries_unfilled_target_rate():
    t = _long_trader()
    pos = _long_pos()
    requested_rates = []
    fills = iter((20.0, 30.0))

    def partial_close(code, position, opt):
        requested_rates.append(opt.pos_rate)
        return {"price": 10.0, "amount": next(fills)}

    t.close_buy = partial_close
    close = Operation("X", "sell", "1buy", pos_rate=0.5, key="ck1")

    assert t.execute("X", close, pos) is True
    assert t.execute("X", close, pos) is True
    assert t.execute("X", close, pos) is False

    assert requested_rates == [0.5, 0.3]
    assert close.pos_rate == 0.5
    assert pos.close_keys["ck1"] == 0.5
    assert pos.amount == 50.0
    assert pos.now_pos_rate == 0.5


def test_close_full_fill_unchanged():
    # 防呆: 全额成交(回测/paper 恒此路径)行为与旧码一致 → now_pos_rate 与 amount 双归0
    t = _long_trader()
    pos = _long_pos()
    t.close_buy = lambda code, position, opt: {"price": 10.0, "amount": 100.0}
    t.execute("X", Operation("X", "sell", "1buy", pos_rate=1.0, key="ck1"), pos)
    assert pos.amount == 0.0
    assert pos.now_pos_rate == 0.0


def test_short_close_partial_fill_same_key_retries_remnant():
    t = BackTestTrader(
        "t", mode="trade", market="currency", init_balance=100000, fee_rate=0.0
    )
    t.datas = _FakeDatas()
    pos = _short_pos()
    requested_rates = []
    fills = iter((6.0, 4.0))

    def partial_close(code, position, opt):
        requested_rates.append(opt.pos_rate)
        return {"price": 10.0, "amount": next(fills)}

    t.close_sell = partial_close
    operation = Operation("X", "sell", "1sell", pos_rate=1.0, key="short-key")

    assert t.execute("X", operation, pos) is True
    assert t.execute("X", operation, pos) is True
    assert t.execute("X", operation, pos) is True
    assert requested_rates == [1.0, 0.4]
    assert operation.pos_rate == 1.0
    assert pos.amount == 0.0
    assert pos.now_pos_rate == 0.0
