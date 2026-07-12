"""H3-c: CTPState.restore_order_ref 单调恢复; M4: get_alive_orders 过滤未终结挂单。"""

from types import SimpleNamespace

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


# ---------- D1-HIGH-1 幽灵仓: begin_position_query epoch reconciliation ----------
def test_begin_position_query_prunes_stale_full_scope():
    """全量查询后,券商未返回的陈旧 Position!=0 键被剔除(幽灵仓修复)。"""
    st = CTPState()
    st.set_position("rb2405_2", FakePosInfo("rb2405", "2", 3))  # 上一轮持仓
    st.set_position("au2406_2", FakePosInfo("au2406", "2", 2))  # 上一轮持仓
    # 本轮全量查询:券商只返回 rb(au 已全平 → 券商不再返回该行)
    st.begin_position_query()  # 全量 scope=None
    st.set_position("rb2405_2", FakePosInfo("rb2405", "2", 3))
    st.mark_position_query_done()
    snap = st.get_positions_snapshot()
    assert "rb2405_2" in snap
    assert "au2406_2" not in snap, "券商已不返回的幽灵仓未被剔除(原永不清空会残留)"


def test_begin_position_query_scoped_only_prunes_that_code():
    """单 code scope 查询只剔除该 code 的未见键,其它 code 不动。"""
    st = CTPState()
    st.set_position("rb2405_2", FakePosInfo("rb2405", "2", 3))
    st.set_position("au2406_2", FakePosInfo("au2406", "2", 2))
    st.begin_position_query(scope_code="rb2405")  # 只查 rb,rb 已平无返回
    st.mark_position_query_done()
    snap = st.get_positions_snapshot()
    assert "rb2405_2" not in snap  # scope 内未见 → 剔除
    assert "au2406_2" in snap  # 不在 scope → 保留


def test_prepare_position_query_does_not_prune():
    """prepare(open 单 code 路径)不开启 reconciliation,向后兼容不剔除陈旧键。"""
    st = CTPState()
    st.set_position("rb2405_2", FakePosInfo("rb2405", "2", 3))
    st.prepare_position_query()
    st.mark_position_query_done()
    assert "rb2405_2" in st.get_positions_snapshot()


def test_ctp_state_snapshot_round_trip_preserves_orders_trades_and_quarantine():
    st = CTPState()
    st.restore_order_ref(88)
    order = SimpleNamespace(
        OrderRef="88",
        InstrumentID="rb2405",
        OrderStatus="3",
        VolumeTraded=1,
        ExchangeID="SHFE",
        OrderSysID="sys-88",
        FrontID=7,
        SessionID=9,
    )
    trade = SimpleNamespace(
        TradeID="trade-1",
        ExchangeID="SHFE",
        OrderRef="88",
        InstrumentID="rb2405",
        Price=3500.0,
        Volume=1,
    )
    st.set_order("88", order)
    assert st.set_trade(trade) is True
    assert st.set_trade(trade) is False
    st.mark_order_reconciliation_required("rb2405", "88", 1)

    snapshot = st.export_snapshot()
    assert isinstance(snapshot["orders"]["88"], dict)
    assert snapshot["trades"]["SHFE:trade-1"]["Price"] == 3500.0

    restored = CTPState()
    assert restored.import_snapshot(snapshot) is True
    assert restored.order_ref == 88
    assert restored.get_order("88").OrderSysID == "sys-88"
    assert len(restored.get_trades_snapshot()) == 1
    assert restored.get_order_reconciliation_required("rb2405") == {"88": 1}
    assert restored.is_reconciliation_ready() is False


def test_reconciliation_barrier_requires_explicit_completion():
    st = CTPState()
    assert st.is_reconciliation_ready() is True
    st.require_reconciliation("front disconnected")
    assert st.is_reconciliation_ready() is False
    assert "disconnected" in st.get_reconciliation_reason()
    st.complete_reconciliation()
    assert st.is_reconciliation_ready() is True


def test_order_query_prunes_only_baseline_not_new_live_callback():
    st = CTPState()
    st.set_order("old", FakeOrder(OrderStatus="3", InstrumentID="rb2405"))
    st.begin_order_query(11)
    st.set_order("live", FakeOrder(OrderStatus="3", InstrumentID="rb2405"))
    st.mark_order_query_done(11)

    assert st.get_order("old") is None
    assert st.get_order("live") is not None


def test_scoped_order_query_does_not_prune_other_code():
    st = CTPState()
    st.set_order("rb", FakeOrder(OrderStatus="3", InstrumentID="rb2405"))
    st.set_order("au", FakeOrder(OrderStatus="3", InstrumentID="au2406"))
    st.begin_order_query(12, scope_code="rb2405")
    st.mark_order_query_done(12)

    assert st.get_order("rb") is None
    assert st.get_order("au") is not None
