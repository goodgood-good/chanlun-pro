"""Round11 N1/N2: CTP 同 code 券商去重 + query_broker_position 覆写。

N1: open_buy/open_sell 原仅 get_position_count>=max_pos 总数门, 无同 code 去重 →
崩溃/丢盘重启后 self.positions 空但券商有仓时, 策略下一 tick 对同合约二次真单致持仓翻倍
(HK/currency/futures 已修此项, CTP 漏)。inline 复用 open 已查的持仓快照做同 code 拦截。
N2: 基类 query_broker_position 走 self.ex.positions(对 CTP=MarketCTP.positions raise) →
reconcile_positions 恒 no-op; CTPTrader 覆写走 trader_api 真查询。

经 conftest stub 离线 import 真实 trader_ctp(不连柜台)。灰度: 真 CTP 需验证
OnRspQryInvestorPosition 填充快照口径与本测试 mock 一致。
"""

import pytest

from chanlun.trading.base import POSITION, Operation
from tests.trader.conftest import FakeOrder, FakePosInfo, FakeTick


@pytest.fixture
def ctp(monkeypatch):
    from chanlun.trader.trader_ctp import CTPTrader

    tr = object.__new__(CTPTrader)
    tr.max_pos = 5
    tr.log = None
    tr.risk_precheck = lambda *a, **k: (True, "")

    class _Ex:
        broker_id = "9999"
        user_id = "test"

        def ticks(self, codes):
            return {c: FakeTick(last=100.0, buy1=99.5, sell1=100.5) for c in codes}

    tr.ex = _Ex()

    recorded = {"orders": []}

    class _State:
        def __init__(self):
            self._ref = 0
            self._snapshot = {}

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
            return FakeOrder(OrderStatus="0", VolumeTraded=1)

        def prepare_position_query(self):
            return None

        def begin_position_query(self, scope_code=None, request_id=None):
            return None

        def next_request_id(self):
            return 1

        def wait_for_position_query(self, timeout):
            return True

        def get_position_count(self):
            return sum(1 for p in self._snapshot.values() if getattr(p, "Position", 0) != 0)

        def get_positions_snapshot(self):
            return dict(self._snapshot)

        def get_alive_orders(self, code):
            return []

    class _Api:
        def __init__(self):
            self.state = _State()
            self.front_id = 1
            self.session_id = 1

        def ReqOrderInsert(self, req, n):
            recorded["orders"].append({"Direction": req.Direction})
            return 0

        def ReqQryInvestorPosition(self, req, n):
            return 0

    tr.trader_api = _Api()
    tr._recorded = recorded

    import chanlun.trader.trader_ctp as mod

    monkeypatch.setattr(mod.db, "order_save", lambda *a, **k: None)
    monkeypatch.setattr(mod.utils, "send_fs_msg", lambda *a, **k: None)
    return tr


def test_open_buy_blocked_when_same_code_held(ctp):
    """券商已持有该 code(Position!=0)→ open_buy 去重返回 False, 不发单。"""
    ctp.trader_api.state._snapshot = {"rb2405_2": FakePosInfo("rb2405", "2", 1)}
    res = ctp.open_buy("rb2405", Operation("rb2405", "buy", "1buy", msg="t"))
    assert res is False
    assert len(ctp._recorded["orders"]) == 0


def test_open_buy_proceeds_when_no_same_code(ctp):
    """券商无该 code 持仓 → open_buy 正常下单(控制组, 证去重不误拦)。"""
    ctp.trader_api.state._snapshot = {}
    res = ctp.open_buy("rb2405", Operation("rb2405", "buy", "1buy", msg="t"))
    assert res is not False
    assert res["requested_amount"] == 1
    assert len(ctp._recorded["orders"]) == 1


def test_open_buy_allows_partial_fill_retry_when_broker_matches_local(ctp):
    ctp.trader_api.state._snapshot = {
        "rb2405_2": FakePosInfo("rb2405", "2", 1)
    }
    ctp.positions = {
        "rb2405:1buy": POSITION(code="rb2405", mmd="1buy", amount=1)
    }

    res = ctp.open_buy(
        "rb2405", Operation("rb2405", "buy", "1buy", key="partial", msg="t")
    )

    assert res is not False
    assert res["requested_amount"] == 1
    assert len(ctp._recorded["orders"]) == 1


def test_open_buy_stops_when_existing_order_cancel_is_unconfirmed(ctp):
    """旧活动单撤单未确认时不得继续开多，避免旧新两单随后双成。"""
    ctp.trader_api.state._snapshot = {}
    ctp.trader_api.state.get_alive_orders = lambda code: [("old-buy", None)]
    ctp.cancel_order = lambda ref: False

    res = ctp.open_buy("rb2405", Operation("rb2405", "buy", "1buy", msg="t"))

    assert res is False
    assert ctp._recorded["orders"] == []


def test_open_buy_stops_when_canceled_old_order_has_fill(ctp):
    """持仓查询后旧挂单又成交并撤余量时，必须停止新单等待下轮重新对账。"""
    ctp.trader_api.state._snapshot = {}
    ctp.trader_api.state.get_alive_orders = lambda code: [("old-filled", None)]

    def cancel_with_fill(ref):
        ctp.trader_api.state._order = FakeOrder(
            OrderStatus="5", VolumeTraded=1, InstrumentID="rb2405"
        )
        return True

    ctp.cancel_order = cancel_with_fill

    res = ctp.open_buy("rb2405", Operation("rb2405", "buy", "1buy", msg="t"))

    assert res is False
    assert ctp._recorded["orders"] == []


def test_open_sell_blocked_when_same_code_held(ctp):
    """券商已持有该 code → open_sell 同样去重返回 False。"""
    ctp.trader_api.state._snapshot = {"rb2405_3": FakePosInfo("rb2405", "3", 1)}
    res = ctp.open_sell("rb2405", Operation("rb2405", "sell", "1sell", msg="t"))
    assert res is False
    assert len(ctp._recorded["orders"]) == 0


def test_open_sell_stops_when_existing_order_cancel_is_unconfirmed(ctp):
    """旧活动单撤单未确认时不得继续开空。"""
    ctp.trader_api.state._snapshot = {}
    ctp.trader_api.state.get_alive_orders = lambda code: [("old-sell", None)]
    ctp.cancel_order = lambda ref: False

    res = ctp.open_sell("rb2405", Operation("rb2405", "sell", "1sell", msg="t"))

    assert res is False
    assert ctp._recorded["orders"] == []


def test_query_broker_position_reports_held(ctp):
    """N2: 覆写的 query_broker_position 走 trader_api → 持有该 code 返 (ok, 非空)。"""
    ctp.trader_api.state._snapshot = {"rb2405_2": FakePosInfo("rb2405", "2", 1)}
    status, pos = ctp.query_broker_position("rb2405")
    assert status == "ok"
    assert bool(pos)


def test_query_broker_position_empty_when_not_held(ctp):
    """N2: 未持该 code → (ok, 空), reconcile/_broker_already_holds 据此判无仓。"""
    ctp.trader_api.state._snapshot = {"au2406_2": FakePosInfo("au2406", "2", 1)}
    status, pos = ctp.query_broker_position("rb2405")
    assert status == "ok"
    assert not pos
