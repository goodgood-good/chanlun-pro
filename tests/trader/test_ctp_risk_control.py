"""H1: CTPTrader.check_stop_loss / check_position_time / get_positions 真实逻辑。

经 conftest stub 离线 import。
"""

from datetime import datetime, timedelta

import pytest

from chanlun.trading.base import POSITION
from tests.trader.conftest import FakePosInfo, FakeTick


@pytest.fixture
def ctp():
    from chanlun.trader.trader_ctp import CTPTrader

    tr = object.__new__(CTPTrader)
    tr.max_hold_days = 5
    tr.positions = {}

    class _Ex:
        broker_id = "9999"
        user_id = "test"
        _ticks = {}

        def ticks(self, codes):
            return {c: self._ticks.get(c) for c in codes if c in self._ticks}

    tr.ex = _Ex()
    return tr


# ---------- check_stop_loss ----------
def test_stop_loss_long_triggers_below_loss_price(ctp):
    ctp.ex._ticks = {"rb2405": FakeTick(last=95.0)}
    pos = POSITION(code="rb2405", mmd="1buy", amount=1, loss_price=96.0)
    assert ctp.check_stop_loss(pos) is True  # 95 < 96 触发


def test_stop_loss_long_no_trigger_above_loss_price(ctp):
    ctp.ex._ticks = {"rb2405": FakeTick(last=97.0)}
    pos = POSITION(code="rb2405", mmd="1buy", amount=1, loss_price=96.0)
    assert ctp.check_stop_loss(pos) is False


def test_stop_loss_short_triggers_above_loss_price(ctp):
    ctp.ex._ticks = {"rb2405": FakeTick(last=105.0)}
    pos = POSITION(code="rb2405", mmd="1sell", amount=1, loss_price=104.0)
    assert ctp.check_stop_loss(pos) is True  # 105 > 104 触发


def test_stop_loss_none_loss_price_no_trigger(ctp):
    ctp.ex._ticks = {"rb2405": FakeTick(last=10.0)}
    pos = POSITION(code="rb2405", mmd="1buy", amount=1, loss_price=None)
    assert ctp.check_stop_loss(pos) is False


def test_stop_loss_zero_loss_price_no_trigger(ctp):
    ctp.ex._ticks = {"rb2405": FakeTick(last=10.0)}
    pos = POSITION(code="rb2405", mmd="1buy", amount=1, loss_price=0)
    assert ctp.check_stop_loss(pos) is False


def test_stop_loss_nan_price_no_trigger(ctp):
    ctp.ex._ticks = {"rb2405": FakeTick(last=float("nan"))}
    pos = POSITION(code="rb2405", mmd="1buy", amount=1, loss_price=96.0)
    assert ctp.check_stop_loss(pos) is False


def test_stop_loss_no_tick_no_trigger(ctp):
    ctp.ex._ticks = {}  # 无行情
    pos = POSITION(code="rb2405", mmd="1buy", amount=1, loss_price=96.0)
    assert ctp.check_stop_loss(pos) is False


# ---------- check_position_time ----------
def test_position_time_triggers_when_over_max_days(ctp):
    pos = POSITION(
        code="rb2405",
        mmd="1buy",
        amount=1,
        open_datetime=datetime.now() - timedelta(days=6),
    )
    assert ctp.check_position_time(pos) is True


def test_position_time_no_trigger_within_max_days(ctp):
    pos = POSITION(
        code="rb2405",
        mmd="1buy",
        amount=1,
        open_datetime=datetime.now() - timedelta(days=1),
    )
    assert ctp.check_position_time(pos) is False


def test_position_time_none_open_datetime_no_trigger(ctp):
    pos = POSITION(code="rb2405", mmd="1buy", amount=1, open_datetime=None)
    assert ctp.check_position_time(pos) is False


def test_position_time_disabled_when_max_hold_days_none(ctp):
    ctp.max_hold_days = None
    pos = POSITION(
        code="rb2405",
        mmd="1buy",
        amount=1,
        open_datetime=datetime.now() - timedelta(days=100),
    )
    assert ctp.check_position_time(pos) is False


# ---------- get_positions 查询失败退回本地 ----------
def test_get_positions_query_fail_falls_back_to_local(ctp):
    """查询失败时不臆造空列表, 退回本地非空持仓快照 (防风控误判无仓)。"""

    class _State:
        def prepare_position_query(self):
            pass

        def next_request_id(self):
            return 1

        def wait_for_position_query(self, t):
            return False  # 查询超时失败

        def get_positions_snapshot(self):
            return {}

    class _Api:
        def __init__(self):
            self.state = _State()

        def ReqQryInvestorPosition(self, req, n):
            return 0

    ctp.trader_api = _Api()
    local = POSITION(code="rb2405", mmd="1buy", amount=2, loss_price=96.0)
    ctp.positions = {"rb2405:1buy": local}
    result = ctp.get_positions()
    assert len(result) == 1
    assert result[0].code == "rb2405"
    assert result[0].amount == 2


def test_get_positions_backfills_open_datetime_from_local(ctp):
    """券商无 open_datetime/loss_price 时, 用本地同 code 回填。"""
    od = datetime.now() - timedelta(days=2)

    class _State:
        def prepare_position_query(self):
            pass

        def next_request_id(self):
            return 1

        def wait_for_position_query(self, t):
            return True

        def get_positions_snapshot(self):
            return {"rb2405_2": FakePosInfo("rb2405", "2", 1, OpenPrice=100.0)}

    class _Api:
        def __init__(self):
            self.state = _State()

        def ReqQryInvestorPosition(self, req, n):
            return 0

    ctp.trader_api = _Api()
    ctp.positions = {
        "rb2405:1buy": POSITION(
            code="rb2405", mmd="1buy", amount=1, loss_price=96.0, open_datetime=od
        )
    }
    result = ctp.get_positions()
    assert len(result) == 1
    assert "buy" in result[0].mmd
    assert result[0].loss_price == 96.0
    assert result[0].open_datetime == od
