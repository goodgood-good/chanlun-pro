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

        def next_order_ref(self):
            self._ref += 1
            return str(self._ref)

        def register_order_wait(self, ref):
            return None

        def wait_for_order(self, ref, timeout):
            return True

        def get_order(self, ref):
            # 返回全成订单, 使 M3 判定通过
            return FakeOrder(OrderStatus="0", VolumeTraded=1)

        def prepare_position_query(self):
            return None

        def next_request_id(self):
            return 1

        def wait_for_position_query(self, timeout):
            return True

        def get_positions_snapshot(self):
            return self._snapshot

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
    """多头 (mmd 含 buy) → 用 THOST_FTDC_D_Sell 卖平 + sell1 价。"""
    from chanlun.trader import trader_ctp as mod

    pos = POSITION(code="rb2405", mmd="1buy", type="做多", amount=1)
    opt = Operation("rb2405", "close", "risk", msg="测试")
    res = ctp.force_close("rb2405", pos, opt)
    assert res is not False
    last = ctp._recorded["orders"][-1]
    assert last["Direction"] == mod.THOST_FTDC_D_Sell
    assert last["LimitPrice"] == 100.5  # sell1


def test_force_close_short_uses_buy_direction(ctp):
    """空头 (mmd 含 sell) → 用 THOST_FTDC_D_Buy 买平 + buy1 价。"""
    from chanlun.trader import trader_ctp as mod

    pos = POSITION(code="rb2405", mmd="1sell", type="做空", amount=1)
    opt = Operation("rb2405", "close", "risk", msg="测试")
    res = ctp.force_close("rb2405", pos, opt)
    assert res is not False
    last = ctp._recorded["orders"][-1]
    assert last["Direction"] == mod.THOST_FTDC_D_Buy
    assert last["LimitPrice"] == 99.5  # buy1


def test_force_close_bc_buy_mmd_treated_as_long(ctp):
    """背驰类 mmd (down_pz_bc_buy 含 buy) 也判为多头。"""
    from chanlun.trader import trader_ctp as mod

    pos = POSITION(code="rb2405", mmd="down_pz_bc_buy", type="做多", amount=1)
    opt = Operation("rb2405", "close", "risk", msg="测试")
    ctp.force_close("rb2405", pos, opt)
    assert ctp._recorded["orders"][-1]["Direction"] == mod.THOST_FTDC_D_Sell


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
