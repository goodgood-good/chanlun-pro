"""M3: CTP 仅 OrderStatus==AllTraded/PartTradedQueueing 且有成交量才算成功。

覆盖 _ctp_order_filled_amount 各 OrderStatus 分支 (真实代码, 经 conftest stub 离线 import)。
"""

from types import SimpleNamespace

from chanlun.trader._ctp_state import CTPState
from tests.trader.conftest import FakeOrder


def _filled(status, volume):
    # 延迟 import: conftest 的 session fixture 已注入 openctp stub
    from chanlun.trader.trader_ctp import _ctp_order_filled_amount

    return _ctp_order_filled_amount(FakeOrder(OrderStatus=status, VolumeTraded=volume))


def test_all_traded_returns_volume():
    # AllTraded='0', 成交 2 手 → 2
    assert _filled("0", 2) == 2


def test_part_traded_queueing_returns_volume():
    # PartTradedQueueing='1', 部分成交 1 手 → 1
    assert _filled("1", 1) == 1


def test_part_traded_not_queueing_returns_volume():
    # PartTradedNotQueueing='2', 部分成交后余量不再排队，也必须承认实际成交
    assert _filled("2", 1) == 1


def test_no_trade_queueing_returns_zero():
    # NoTradeQueueing='3' 未成交 → 0 (调用方据此 return False, 不记本地仓)
    assert _filled("3", 0) == 0


def test_canceled_returns_zero():
    # Canceled='5' 已撤/拒单 → 0
    assert _filled("5", 0) == 0


def test_canceled_with_partial_fill_returns_cumulative_volume():
    # Canceled 仅表示余量已撤；VolumeTraded 是撤单前累计真实成交量，必须承认
    assert _filled("5", 3) == 3


def test_all_traded_zero_volume_returns_zero():
    # 状态全成但成交量字段还是 0 (时序未刷新): 保守判 0 (M3 灰度第 1 步须确认时序)
    assert _filled("0", 0) == 0


def test_status_none_returns_zero():
    assert _filled(None, 0) == 0


def test_volume_none_returns_zero():
    # VolumeTraded 为 None 时 (or 0) 不崩, 返回 0
    assert _filled("0", None) == 0


# ---------- D1-HIGH-3: 部分成交立即撤剩余未成挂单 ----------
def test_settle_part_traded_cancels_remaining_for_part_traded():
    """PartTradedQueueing → 撤剩余未成挂单(防续成致券商持仓>本地); AllTraded/Canceled → no-op。"""
    from chanlun.trader.trader_ctp import CTPTrader

    tr = CTPTrader.__new__(CTPTrader)  # 绕过 __init__(不连 CTP 柜台)
    state = CTPState()
    tr.trader_api = SimpleNamespace(state=state)
    cancelled = []
    tr.cancel_order = lambda ref: cancelled.append(ref)

    part = FakeOrder(OrderStatus="1")
    state.set_order("r1", part)
    tr._settle_part_traded(part, "r1", "rb2405")  # 部分成交 → 撤剩余
    assert cancelled == ["r1"]
    tr._settle_part_traded(FakeOrder(OrderStatus="0"), "r2", "rb2405")  # 全成 → 不撤
    tr._settle_part_traded(FakeOrder(OrderStatus="5"), "r3", "rb2405")  # 已撤 → 不撤
    assert cancelled == ["r1"]
