"""H2: force_close / lock_position 用 "buy" in pos.mmd 判向 (POSITION 无 direction 字段);
force_close_all 伪造 POSITION 不再 TypeError。

经 conftest stub 离线 import 真实 trader_ctp。
"""

import pytest

from chanlun.trading.base import POSITION, Operation
from tests.trader.conftest import FakeOrder, FakePosInfo, FakeTick


@pytest.fixture
def ctp(monkeypatch):
    """构造一个不连柜台的 CTPTrader 实例 (绕过 __init__), 注入 mock 依赖。"""
    from chanlun.trader.trader_ctp import CTPTrader

    tr = object.__new__(CTPTrader)

    # ---- mock ex.ticks ----
    class _Ex:
        broker_id = "9999"
        user_id = "test"

        def ticks(self, codes):
            return {c: FakeTick(last=100.0, buy1=99.5, sell1=100.5) for c in codes}

    tr.ex = _Ex()

    # ---- mock trader_api.state ----
    recorded = {"orders": []}

    class _State:
        def __init__(self):
            self._ref = 0
            self._snapshot = {}
            self._order = FakeOrder(OrderStatus="0", VolumeTraded=1)

        def next_order_ref(self):
            self._ref += 1
            return str(self._ref)

        def register_order_wait(self, ref, instrument_id=None):
            return None

        def mark_order_submitted(self, ref):
            return None

        def wait_for_order(self, ref, timeout):
            return True

        def get_order(self, ref):
            # 返回全成订单, 使 M3 判定通过
            return self._order

        def prepare_position_query(self):
            return None

        def begin_position_query(self, scope_code=None, request_id=None):
            return None

        def next_request_id(self):
            return 1

        def wait_for_position_query(self, timeout):
            return True

        def get_positions_snapshot(self):
            return self._snapshot

        def get_alive_orders(self, code=None):
            return []

    class _Api:
        def __init__(self):
            self.state = _State()
            self.front_id = 1
            self.session_id = 1

        def ReqOrderInsert(self, req, n):
            recorded["orders"].append(
                {"Direction": req.Direction, "LimitPrice": req.LimitPrice}
            )
            return 0

        def ReqQryInvestorPosition(self, req, n):
            return 0

    tr.trader_api = _Api()
    tr._recorded = recorded

    # ---- mock db.order_save + utils.send_fs_msg (避免真实落库/发飞书) ----
    import chanlun.trader.trader_ctp as mod

    monkeypatch.setattr(mod.db, "order_save", lambda *a, **k: None)
    monkeypatch.setattr(mod.utils, "send_fs_msg", lambda *a, **k: None)

    return tr


def test_force_close_long_uses_sell_direction(ctp):
    """多头 (mmd 含 buy) → 用 THOST_FTDC_D_Sell 卖平 + 对手价买一 buy1(C18)。"""
    from chanlun.trader import trader_ctp as mod

    pos = POSITION(code="rb2405", mmd="1buy", type="做多", amount=1)
    opt = Operation("rb2405", "close", "risk", msg="测试")
    res = ctp.force_close("rb2405", pos, opt)
    assert res is not False
    last = ctp._recorded["orders"][-1]
    assert last["Direction"] == mod.THOST_FTDC_D_Sell
    assert last["LimitPrice"] == 99.5  # C18 对手价=买一 buy1(卖出平多即时成交)


def test_force_close_short_uses_buy_direction(ctp):
    """空头 (mmd 含 sell) → 用 THOST_FTDC_D_Buy 买平 + 对手价卖一 sell1(C18)。"""
    from chanlun.trader import trader_ctp as mod

    pos = POSITION(code="rb2405", mmd="1sell", type="做空", amount=1)
    opt = Operation("rb2405", "close", "risk", msg="测试")
    res = ctp.force_close("rb2405", pos, opt)
    assert res is not False
    last = ctp._recorded["orders"][-1]
    assert last["Direction"] == mod.THOST_FTDC_D_Buy
    assert last["LimitPrice"] == 100.5  # C18 对手价=卖一 sell1(买入平空即时成交)


def test_force_close_bc_buy_mmd_treated_as_long(ctp):
    """背驰类 mmd (down_pz_bc_buy 含 buy) 也判为多头。"""
    from chanlun.trader import trader_ctp as mod

    pos = POSITION(code="rb2405", mmd="down_pz_bc_buy", type="做多", amount=1)
    opt = Operation("rb2405", "close", "risk", msg="测试")
    ctp.force_close("rb2405", pos, opt)
    assert ctp._recorded["orders"][-1]["Direction"] == mod.THOST_FTDC_D_Sell


def test_lock_position_no_trade_terminal_does_not_record_success(ctp):
    """NoTradeNotQueueing 是零成交终态，锁仓不能按请求量伪记成功。"""
    ctp.trader_api.state._order = FakeOrder(
        OrderStatus="4", VolumeTraded=0, InstrumentID="rb2405"
    )
    pos = POSITION(code="rb2405", mmd="1buy", type="做多", amount=2)
    opt = Operation("rb2405", "lock", "risk", msg="测试")

    assert ctp.lock_position("rb2405", pos, opt) is False


def test_lock_position_stops_when_existing_order_cancel_is_unconfirmed(ctp):
    """旧活动单撤单未确认时不得再发锁仓单。"""
    ctp.trader_api.state.get_alive_orders = lambda code: [("old-lock", None)]
    ctp.cancel_order = lambda ref: False
    pos = POSITION(code="rb2405", mmd="1buy", type="做多", amount=2)
    opt = Operation("rb2405", "lock", "risk", msg="测试")

    assert ctp.lock_position("rb2405", pos, opt) is False
    assert ctp._recorded["orders"] == []


def test_close_buy_stops_remaining_legs_while_first_leg_is_still_alive(ctp):
    """上期所首腿部分成交未终结时，不得继续发第二条平昨腿。"""
    info = FakePosInfo("rb2405", "2", 2)
    info.ExchangeID = "SHFE"
    info.YdPosition = 1
    ctp.trader_api.state._snapshot = {"rb2405_2": info}
    leg_calls = []

    def first_leg_only(*args):
        leg_calls.append(args)
        return 1

    ctp._send_close_leg = first_leg_only
    ctp.trader_api.state.get_alive_orders = lambda code: [("pending-leg", None)]
    pos = POSITION(code="rb2405", mmd="1buy", type="做多", amount=2)
    opt = Operation("rb2405", "close", "risk", msg="测试")

    result = ctp.close_buy("rb2405", pos, opt)

    assert result["amount"] == 1
    assert len(leg_calls) == 1


def test_force_close_all_does_not_crash(ctp):
    """force_close_all 伪造 POSITION(mmd=...) 不再 TypeError, 多+空各 1 仓都平。"""
    # 注入券商持仓快照: 一多 (PosiDirection='2') 一空
    ctp.trader_api.state._snapshot = {
        "rb2405_2": FakePosInfo("rb2405", "2", 1, OpenPrice=100.0),
        "au2406_3": FakePosInfo("au2406", "3", 1, OpenPrice=500.0),
    }
    opt = Operation("ALL", "close", "risk", msg="全平")
    results = ctp.force_close_all(opt)
    # 两仓都触发 force_close 且返回结果 (无 TypeError)
    assert len(results) == 2


# ============================================================================
# 终检R13-#1 (CRIT): force_close 成交后同步本地 self.positions 账本
# force_close 只发柜台单+db.order_save, 从不碰 self.positions → 残留僵尸条目
# (amount!=0/now_pos_rate>=1) 永久拉黑 execute() 同 open_uid 重开 + 污染同 code
# 新仓 loss_price/open_datetime。修复=成交后按 code+方向清零匹配条目+归档+落盘。
# ============================================================================


def test_force_close_syncs_local_ledger(ctp):
    """强平成交后必须清零 self.positions 匹配条目 + 归档, 否则 execute() 守卫
    (amount!=0) 永久拦截同 open_uid 重开。"""
    from chanlun.trading.base import POSITION, Operation

    zombie = POSITION(code="rb2405", mmd="1buy", type="做多", amount=1, loss_price=90.0)
    zombie.now_pos_rate = 1.0
    ctp.positions = {"rb2405:1buy": zombie}
    ctp.positions_history = {}
    ctp._pkl_key = None  # 不触发真实落盘(仅验证内存账本清零)

    # get_positions() 造的 broker POSITION(mmd 字面 "buy")
    pos = POSITION(code="rb2405", mmd="buy", type="做多", amount=1)
    opt = Operation("rb2405", "close", "risk", msg="超时强平")
    res = ctp.force_close("rb2405", pos, opt)
    assert res is not False  # 强平成交

    assert ctp.positions["rb2405:1buy"].amount == 0
    assert ctp.positions["rb2405:1buy"].now_pos_rate == 0
    assert len(ctp.positions_history.get("rb2405", [])) == 1  # 归档保留轨迹


def test_force_close_partial_fill_only_reduces_local_ledger(ctp):
    """强平只成交 1/2 手时，本地只扣实际量且未清仓不得提前归档。"""
    live = POSITION(
        code="rb2405", mmd="1buy", type="做多", amount=2, balance=2000, loss_price=90.0
    )
    live.now_pos_rate = 1.0
    ctp.positions = {"rb2405:1buy": live}
    ctp.positions_history = {}
    ctp._pkl_key = None
    ctp.trader_api.state._order = FakeOrder(OrderStatus="0", VolumeTraded=1)
    pos = POSITION(code="rb2405", mmd="buy", type="做多", amount=2)
    opt = Operation("rb2405", "close", "risk", msg="部分强平")

    result = ctp.force_close("rb2405", pos, opt)

    assert result["amount"] == 1
    assert ctp.positions["rb2405:1buy"].amount == 1
    assert ctp.positions["rb2405:1buy"].balance == 1000
    assert ctp.positions["rb2405:1buy"].now_pos_rate == 0.5
    assert ctp.positions_history == {}


def test_force_close_failure_keeps_local_ledger(ctp):
    """强平未成交(total<=0)时不得清零本地账本(仓位仍在, 清零=丢失裸持轨迹)。"""
    from chanlun.trading.base import POSITION, Operation

    ctp._send_close_leg = lambda *a, **k: 0  # 每腿 0 成交 → total<=0 → 返回 False

    live = POSITION(code="rb2405", mmd="1buy", type="做多", amount=1, loss_price=90.0)
    live.now_pos_rate = 1.0
    ctp.positions = {"rb2405:1buy": live}
    ctp.positions_history = {}
    ctp._pkl_key = None

    pos = POSITION(code="rb2405", mmd="buy", type="做多", amount=1)
    opt = Operation("rb2405", "close", "risk", msg="超时强平")
    res = ctp.force_close("rb2405", pos, opt)
    assert res is False
    assert ctp.positions["rb2405:1buy"].amount == 1  # 原样保留
    assert ctp.positions_history == {}


def test_force_close_long_leaves_short_ledger_intact(ctp):
    """平多仓只清零同 code 多头条目, 同 code 空头账本不受影响(方向选择性)。"""
    from chanlun.trading.base import POSITION, Operation

    long_z = POSITION(code="rb2405", mmd="1buy", type="做多", amount=1)
    long_z.now_pos_rate = 1.0
    short_p = POSITION(code="rb2405", mmd="1sell", type="做空", amount=2)
    short_p.now_pos_rate = 1.0
    ctp.positions = {"rb2405:1buy": long_z, "rb2405:1sell": short_p}
    ctp.positions_history = {}
    ctp._pkl_key = None

    pos = POSITION(code="rb2405", mmd="buy", type="做多", amount=1)  # 平多
    opt = Operation("rb2405", "close", "risk", msg="超时强平")
    ctp.force_close("rb2405", pos, opt)

    assert ctp.positions["rb2405:1buy"].amount == 0   # 多仓清零
    assert ctp.positions["rb2405:1sell"].amount == 2  # 空仓不动
