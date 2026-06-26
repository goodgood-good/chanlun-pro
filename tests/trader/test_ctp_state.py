"""H3-c: CTPState.restore_order_ref 单调恢复; M4: get_alive_orders 过滤未终结挂单。"""

from chanlun.trader._ctp_state import CTPState
from tests.trader.conftest import FakeOrder, FakePosInfo


# ---------- H3-c restore_order_ref ----------
def test_restore_order_ref_advances_when_larger():
    st = CTPState()
    assert st.order_ref == 0
    st.restore_order_ref(1005)
    assert st.order_ref == 1005
    # 下一个 ref 从恢复值继续递增, 不撞历史
    assert st.next_order_ref() == "1006"


def test_restore_order_ref_no_regress_when_smaller():
    st = CTPState()
    st.next_order_ref()  # ->1
    st.next_order_ref()  # ->2
    st.restore_order_ref(1)  # 小于当前, 不回退
    assert st.order_ref == 2


def test_restore_order_ref_zero_noop():
    st = CTPState()
    st.next_order_ref()  # ->1
    st.restore_order_ref(0)
    assert st.order_ref == 1


# ---------- M4 get_alive_orders ----------
def test_get_alive_orders_returns_unfinished():
    st = CTPState()
    # NoTradeQueueing='3' 与 PartTradedQueueing='1' 算存活; AllTraded='0'/Canceled='5' 不算
    st.set_order("r1", FakeOrder(OrderStatus="3", InstrumentID="rb2405"))
    st.set_order("r2", FakeOrder(OrderStatus="1", InstrumentID="rb2405"))
    st.set_order("r3", FakeOrder(OrderStatus="0", InstrumentID="rb2405"))  # 全成
    st.set_order("r4", FakeOrder(OrderStatus="5", InstrumentID="rb2405"))  # 已撤
    alive = st.get_alive_orders()
    refs = {ref for ref, _o in alive}
    assert refs == {"r1", "r2"}


def test_get_alive_orders_filter_by_code():
    st = CTPState()
    st.set_order("r1", FakeOrder(OrderStatus="3", InstrumentID="rb2405"))
    st.set_order("r2", FakeOrder(OrderStatus="3", InstrumentID="au2406"))
    alive = st.get_alive_orders("rb2405")
    assert [ref for ref, _o in alive] == ["r1"]


def test_get_alive_orders_empty_when_all_finished():
    st = CTPState()
    st.set_order("r1", FakeOrder(OrderStatus="0", InstrumentID="rb2405"))
    st.set_order("r2", FakeOrder(OrderStatus="5", InstrumentID="rb2405"))
    assert st.get_alive_orders() == []


# ---------- D1-HIGH-1 get_position_count 只计非零持仓 ----------
def test_get_position_count_only_counts_nonzero():
    """审计 D1-HIGH-1: max_pos 门控应只数真实持仓(Position!=0)。

    原 len(positions) 把已平仓的陈旧 0 仓键也计入 → 交易过 >=max_pos 个不同合约后
    门控恒达上限 → 永久禁开仓。
    """
    st = CTPState()
    st.set_position("rb2405_2", FakePosInfo("rb2405", "2", 3))  # 持多 3 手
    st.set_position("au2406_2", FakePosInfo("au2406", "2", 0))  # 已平(0 仓残留键)
    st.set_position("IF2403_3", FakePosInfo("IF2403", "3", 0))  # 已平(0 仓残留键)
    # 原实现 len()=3 → 误判达到 max_pos=3 → 永久禁开仓; 修复后只数非零 = 1
    assert st.get_position_count() == 1


def test_get_position_count_zero_when_all_flat():
    st = CTPState()
    st.set_position("rb2405_2", FakePosInfo("rb2405", "2", 0))
    st.set_position("au2406_3", FakePosInfo("au2406", "3", 0))
    assert st.get_position_count() == 0
