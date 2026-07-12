"""CTP 报单回报时序与超时撤单的离线资金安全回归测试。"""

import threading

from types import SimpleNamespace

from chanlun.trader._ctp_state import CTPState
from tests.trader.conftest import FakeOrder, FakePosInfo


def test_no_trade_queueing_does_not_finish_wait_before_all_traded():
    """排队首报仍是活动 GFD 单，必须继续等后续可结算回报。"""
    state = CTPState()
    state.register_order_wait("17", instrument_id="rb2405")

    state.set_order(
        "17",
        FakeOrder(OrderStatus="3", VolumeTraded=0, InstrumentID="rb2405"),
    )
    assert state.wait_for_order("17", timeout=0.01) is False

    state.set_order(
        "17",
        FakeOrder(OrderStatus="0", VolumeTraded=2, InstrumentID="rb2405"),
    )
    assert state.wait_for_order("17", timeout=0.1) is True
    assert state.get_order("17").VolumeTraded == 2


def test_no_trade_not_queueing_and_canceled_finish_wait():
    """零成交不再排队与已撤单均是终态，等待方必须及时结束。"""
    for ref, status in (("18", "4"), ("19", "5")):
        state = CTPState()
        state.register_order_wait(ref, instrument_id="rb2405")
        state.set_order(
            ref,
            FakeOrder(OrderStatus=status, VolumeTraded=0, InstrumentID="rb2405"),
        )
        assert state.wait_for_order(ref, timeout=0.1) is True


def test_part_traded_queueing_wakes_cancel_path_without_deadlock():
    """部分成交仍排队并非终态，但须唤醒调用方立即撤余单。"""
    state = CTPState()
    state.register_order_wait("20", instrument_id="rb2405")
    state.set_order(
        "20",
        FakeOrder(OrderStatus="1", VolumeTraded=1, InstrumentID="rb2405"),
    )

    assert state.wait_for_order("20", timeout=0.1) is True
    assert [ref for ref, _ in state.get_alive_orders()] == ["20"]


def test_first_callback_releases_pre_registered_instrument_metadata():
    """首报已有 InstrumentID 后，撤单可读 orders，新预注册表不得无界增长。"""
    state = CTPState()
    state.register_order_wait("21", instrument_id="rb2405")
    assert state.get_order_instrument("21") == "rb2405"

    state.set_order(
        "21",
        FakeOrder(OrderStatus="3", VolumeTraded=0, InstrumentID="rb2405"),
    )

    assert state.get_order("21").InstrumentID == "rb2405"
    assert state.get_order_instrument("21") is None


def test_terminal_callback_without_instrument_releases_pending_metadata():
    """终态已证明不再活动，即使缺 InstrumentID 也必须无条件释放 pending。"""
    state = CTPState()
    state.register_order_wait("21b", instrument_id="rb2405")
    state.mark_order_submitted("21b")

    state.set_order("21b", FakeOrder(OrderStatus="5", InstrumentID=""))

    assert state.wait_for_order("21b", timeout=0.1) is True
    assert state.get_order_instrument("21b") is None
    assert state.get_alive_orders() == []


def test_missing_instrument_active_order_is_not_duplicated_in_alive_orders():
    """活动首报缺合约时 orders 与 pending 是同一 ref，只能暴露一次。"""
    state = CTPState()
    state.register_order_wait("21c", instrument_id="rb2405")
    state.mark_order_submitted("21c")
    state.set_order("21c", FakeOrder(OrderStatus="3", InstrumentID=""))

    alive = state.get_alive_orders()
    assert [ref for ref, _ in alive] == ["21c"]


def test_only_accepted_pre_callback_order_is_exposed_as_alive():
    """注册与 ReqOrderInsert 返回之间的短窗口不能把尚未发出的请求当成挂单。"""
    state = CTPState()
    state.register_order_wait("22", instrument_id="rb2405")
    assert state.get_alive_orders("rb2405") == []

    state.mark_order_submitted("22")
    assert state.get_alive_orders("rb2405") == [("22", None)]


def test_insert_rejection_discards_wait_and_instrument_metadata():
    """ReqOrderInsert 立即拒绝时，没有回调负责清理，调用方必须主动 discard。"""
    from chanlun.trader import trader_ctp as mod

    state = CTPState()

    class FakeTraderApi:
        def __init__(self):
            self.state = state

        def ReqOrderInsert(self, req, request_id):
            return 1

    trader = mod.CTPTrader.__new__(mod.CTPTrader)
    trader.trader_api = FakeTraderApi()

    assert trader._send_close_leg("rb2405", "sell", 1, "close", 3500) == 0
    assert state.get_order_instrument("1") is None
    assert "1" not in state._order_events


def test_cancel_order_uses_registered_instrument_before_first_callback():
    """首个 OnRtnOrder 缺失时，仍可用报单时元数据构造并确认撤单。"""
    from chanlun.trader.trader_ctp import CTPTrader

    state = CTPState()
    state.register_order_wait("23", instrument_id="au2406")
    action_calls = []

    class FakeTraderApi:
        front_id = 7
        session_id = 11

        def __init__(self):
            self.state = state

        def ReqOrderAction(self, req, request_id):
            action_calls.append((req, request_id))
            state.set_order(
                "23",
                FakeOrder(
                    OrderStatus="5", VolumeTraded=0, InstrumentID="au2406"
                ),
            )
            return 0

    trader = CTPTrader.__new__(CTPTrader)
    trader.ex = SimpleNamespace(broker_id="9999", user_id="test")
    trader.trader_api = FakeTraderApi()

    assert trader.cancel_order("23") is True
    assert len(action_calls) == 1
    req, request_id = action_calls[0]
    assert req.InstrumentID == "au2406"
    assert req.OrderRef == "23"
    assert req.FrontID == 7
    assert req.SessionID == 11
    assert request_id == 1


def test_cancel_falls_back_when_callback_omits_instrument_id():
    """异常首报缺合约字段时，仍保留并使用预注册 InstrumentID 撤单。"""
    from chanlun.trader.trader_ctp import CTPTrader

    state = CTPState()
    state.register_order_wait("24", instrument_id="ag2406")
    state.mark_order_submitted("24")
    state.set_order("24", FakeOrder(OrderStatus="3", InstrumentID=""))
    action_reqs = []

    class FakeTraderApi:
        front_id = 2
        session_id = 4

        def __init__(self):
            self.state = state

        def ReqOrderAction(self, req, request_id):
            action_reqs.append(req)
            state.set_order(
                "24", FakeOrder(OrderStatus="5", InstrumentID="ag2406")
            )
            return 0

    trader = CTPTrader.__new__(CTPTrader)
    trader.ex = SimpleNamespace(broker_id="9999", user_id="test")
    trader.trader_api = FakeTraderApi()

    assert trader.cancel_order("24") is True
    assert action_reqs[0].InstrumentID == "ag2406"


def test_close_leg_timeout_attempts_cancel_without_false_success_log(monkeypatch):
    """平仓无首报时仍尝试撤单，且发送失败不能记录成“已撤”。"""
    from chanlun.trader import trader_ctp as mod

    state = CTPState()
    insert_calls = []
    action_calls = []
    warnings = []

    class FakeTraderApi:
        front_id = 3
        session_id = 5

        def __init__(self):
            self.state = state

        def ReqOrderInsert(self, req, request_id):
            insert_calls.append((req, request_id))
            return 0

        def ReqOrderAction(self, req, request_id):
            action_calls.append((req, request_id))
            return 1  # 柜台未接受撤单请求

    trader = mod.CTPTrader.__new__(mod.CTPTrader)
    trader.ex = SimpleNamespace(broker_id="9999", user_id="test")
    trader.trader_api = FakeTraderApi()
    monkeypatch.setattr(mod, "_CTP_CALLBACK_TIMEOUT", 0.01)
    monkeypatch.setattr(mod.LogUtil, "warning", warnings.append)

    assert trader._send_close_leg("rb2405", "sell", 1, "close", 3500) == 0
    assert len(insert_calls) == 1
    assert state.get_order_instrument("1") == "rb2405"
    assert len(action_calls) == 1
    assert action_calls[0][0].InstrumentID == "rb2405"
    assert state.get_alive_orders("rb2405") == [("1", None)]
    assert any("撤单未确认" in warning for warning in warnings)
    assert not any("已撤" in warning for warning in warnings)


def test_timeout_boundary_all_traded_is_settled_without_cancel_request():
    """wait 刚超时即收到 AllTraded 时，必须按最新累计成交记账而非返回 0。"""
    from chanlun.trader import trader_ctp as mod

    state = CTPState()
    action_calls = []

    def timeout_after_fill(ref, timeout):
        state.set_order(
            ref,
            FakeOrder(OrderStatus="0", VolumeTraded=2, InstrumentID="rb2405"),
        )
        return False

    state.wait_for_order = timeout_after_fill

    class FakeTraderApi:
        front_id = 1
        session_id = 2

        def __init__(self):
            self.state = state

        def ReqOrderInsert(self, req, request_id):
            return 0

        def ReqOrderAction(self, req, request_id):
            action_calls.append(req)
            return 1

    trader = mod.CTPTrader.__new__(mod.CTPTrader)
    trader.ex = SimpleNamespace(broker_id="9999", user_id="test")
    trader.trader_api = FakeTraderApi()

    assert trader._send_close_leg("rb2405", "sell", 2, "close", 3500) == 2
    assert action_calls == []


def test_timeout_cancel_reloads_canceled_cumulative_fill():
    """超时撤单确认时 Canceled 的累计成交必须返回，不能制造本地幽灵仓。"""
    from chanlun.trader import trader_ctp as mod

    state = CTPState()

    def timeout_while_queueing(ref, timeout):
        state.set_order(
            ref,
            FakeOrder(OrderStatus="3", VolumeTraded=0, InstrumentID="rb2405"),
        )
        return False

    state.wait_for_order = timeout_while_queueing

    class FakeTraderApi:
        front_id = 1
        session_id = 2

        def __init__(self):
            self.state = state

        def ReqOrderInsert(self, req, request_id):
            return 0

        def ReqOrderAction(self, req, request_id):
            state.set_order(
                req.OrderRef,
                FakeOrder(
                    OrderStatus="5", VolumeTraded=1, InstrumentID=req.InstrumentID
                ),
            )
            return 0

    trader = mod.CTPTrader.__new__(mod.CTPTrader)
    trader.ex = SimpleNamespace(broker_id="9999", user_id="test")
    trader.trader_api = FakeTraderApi()

    assert trader._send_close_leg("rb2405", "sell", 2, "close", 3500) == 1


def test_part_traded_cancel_reloads_later_cumulative_fill():
    """撤余单在途新增成交后，调用方必须使用最终 Canceled 的累计量。"""
    from chanlun.trader import trader_ctp as mod

    state = CTPState()
    initial = FakeOrder(
        OrderStatus="1", VolumeTraded=1, InstrumentID="rb2405"
    )
    state.set_order("25", initial)
    trader = mod.CTPTrader.__new__(mod.CTPTrader)
    trader.trader_api = SimpleNamespace(state=state)

    def cancel_with_later_fill(ref):
        state.set_order(
            ref,
            FakeOrder(OrderStatus="5", VolumeTraded=2, InstrumentID="rb2405"),
        )
        return True

    trader.cancel_order = cancel_with_later_fill

    settled = trader._settle_part_traded(initial, "25", "rb2405")
    assert settled.VolumeTraded == 2
    assert settled.OrderStatus == "5"


def test_current_partial_cancel_unconfirmed_quarantines_code(monkeypatch):
    """本次部分成交撤单未确认时须隔离 code，防余单终态后从 alive 消失。"""
    from chanlun.trader import trader_ctp as mod

    state = CTPState()
    initial = FakeOrder(
        OrderStatus="1", VolumeTraded=1, InstrumentID="rb2405"
    )
    state.set_order("25b", initial)
    trader = mod.CTPTrader.__new__(mod.CTPTrader)
    trader.trader_api = SimpleNamespace(state=state)
    cancel_calls = []

    def cancel_unconfirmed(ref):
        cancel_calls.append(ref)
        return False

    trader.cancel_order = cancel_unconfirmed
    monkeypatch.setattr(mod.utils, "send_fs_msg", lambda *args, **kwargs: None)

    settled = trader._settle_part_traded(initial, "25b", "rb2405")

    assert settled is initial
    assert state.get_order_reconciliation_required("rb2405") == {
        "25b": 1
    }
    assert trader._cancel_alive_orders_before_submit("rb2405", "open_buy") is False
    assert cancel_calls == ["25b", "25b"]


def test_async_insert_rejection_wakes_waiter_as_no_trade_terminal():
    """SDK 的异步插单拒绝必须由 MyTraderCallback 写入终态并唤醒等待。"""
    from chanlun.trader.trader_ctp import MyTraderCallback

    callback = MyTraderCallback(SimpleNamespace())
    callback.state.register_order_wait("26", instrument_id="rb2405")
    callback.state.mark_order_submitted("26")
    input_order = SimpleNamespace(OrderRef="26", InstrumentID="rb2405")
    rsp_info = SimpleNamespace(ErrorID=31, ErrorMsg="order rejected")

    callback.OnRspOrderInsert(input_order, rsp_info, 1, True)

    assert callback.state.wait_for_order("26", timeout=0.1) is True
    rejected = callback.state.get_order("26")
    assert rejected.OrderStatus == "4"
    assert rejected.VolumeTraded == 0
    assert "order rejected" in rejected.StatusMsg


def test_close_leg_stops_when_existing_order_cancel_is_unconfirmed(monkeypatch):
    """旧平仓单状态未知时不得重复发平仓腿，避免累计超平。"""
    from chanlun.trader import trader_ctp as mod

    state = CTPState()
    state.register_order_wait("old-close", instrument_id="rb2405")
    state.mark_order_submitted("old-close")
    insert_calls = []

    class FakeTraderApi:
        def __init__(self):
            self.state = state

        def ReqOrderInsert(self, req, request_id):
            insert_calls.append(req)
            return 0

    trader = mod.CTPTrader.__new__(mod.CTPTrader)
    trader.ex = SimpleNamespace(broker_id="9999", user_id="test")
    trader.trader_api = FakeTraderApi()
    trader.cancel_order = lambda ref: False
    monkeypatch.setattr(mod.utils, "send_fs_msg", lambda *args, **kwargs: None)

    assert trader._send_close_leg("rb2405", "sell", 2, "close", 3500) == 0
    assert insert_calls == []


def test_old_order_fill_quarantine_blocks_second_submit_attempt(monkeypatch):
    """旧单撤后有成交时，即使终态已从 alive 消失，第二轮仍须持续熔断。"""
    from chanlun.trader import trader_ctp as mod

    state = CTPState()
    state.register_order_wait("old-filled", instrument_id="rb2405")
    state.mark_order_submitted("old-filled")
    cancel_calls = []

    class FakeTraderApi:
        def __init__(self):
            self.state = state

    trader = mod.CTPTrader.__new__(mod.CTPTrader)
    trader.trader_api = FakeTraderApi()

    def cancel_with_fill(ref):
        cancel_calls.append(ref)
        state.set_order(
            ref,
            FakeOrder(OrderStatus="5", VolumeTraded=2, InstrumentID="rb2405"),
        )
        return True

    trader.cancel_order = cancel_with_fill
    monkeypatch.setattr(mod.utils, "send_fs_msg", lambda *args, **kwargs: None)

    assert trader._cancel_alive_orders_before_submit("rb2405", "open_buy") is False
    assert state.get_alive_orders("rb2405") == []
    assert trader._cancel_alive_orders_before_submit("rb2405", "open_buy") is False
    assert cancel_calls == ["old-filled"]
    assert state.get_order_reconciliation_required("rb2405") == {
        "old-filled": 0
    }


def test_quarantine_auto_clears_terminal_without_volume_delta(monkeypatch):
    """最终累计量等于已入账量时可自动解隔离。"""
    from chanlun.trader import trader_ctp as mod

    state = CTPState()
    state.mark_order_reconciliation_required("rb2405", "q1", 1)
    state.set_order(
        "q1", FakeOrder(OrderStatus="5", VolumeTraded=1, InstrumentID="rb2405")
    )
    trader = mod.CTPTrader.__new__(mod.CTPTrader)
    trader.trader_api = SimpleNamespace(state=state)
    monkeypatch.setattr(mod.utils, "send_fs_msg", lambda *args, **kwargs: None)

    assert trader._cancel_alive_orders_before_submit("rb2405", "open_buy") is True
    assert state.get_order_reconciliation_required("rb2405") == {}


def test_quarantine_keeps_terminal_with_unaccounted_volume_delta(monkeypatch):
    """最终累计量大于已入账量时必须继续隔离。"""
    from chanlun.trader import trader_ctp as mod

    state = CTPState()
    state.mark_order_reconciliation_required("rb2405", "q2", 1)
    state.set_order(
        "q2", FakeOrder(OrderStatus="5", VolumeTraded=2, InstrumentID="rb2405")
    )
    trader = mod.CTPTrader.__new__(mod.CTPTrader)
    trader.trader_api = SimpleNamespace(state=state)
    monkeypatch.setattr(mod.utils, "send_fs_msg", lambda *args, **kwargs: None)

    assert trader._cancel_alive_orders_before_submit("rb2405", "open_buy") is False
    assert state.get_order_reconciliation_required("rb2405") == {"q2": 1}


def test_same_code_submit_lock_closes_register_before_mark_race(monkeypatch):
    """同 code 两线程不能在首单 register 后、mark 前同时穿透并发真单。"""
    from chanlun.trader import trader_ctp as mod

    state = CTPState()
    first_insert_entered = threading.Event()
    allow_first_return = threading.Event()
    second_insert_entered = threading.Event()
    calls_lock = threading.Lock()
    insert_count = 0

    class FakeTraderApi:
        front_id = 1
        session_id = 2

        def __init__(self):
            self.state = state

        def ReqOrderInsert(self, req, request_id):
            nonlocal insert_count
            with calls_lock:
                insert_count += 1
                call_no = insert_count
            if call_no == 1:
                first_insert_entered.set()
                allow_first_return.wait(timeout=2)
            else:
                second_insert_entered.set()
            return 0

        def ReqOrderAction(self, req, request_id):
            return 1

    trader = mod.CTPTrader.__new__(mod.CTPTrader)
    trader.ex = SimpleNamespace(broker_id="9999", user_id="test")
    trader.trader_api = FakeTraderApi()
    monkeypatch.setattr(mod, "_CTP_CALLBACK_TIMEOUT", 0.01)
    monkeypatch.setattr(mod.utils, "send_fs_msg", lambda *args, **kwargs: None)
    results = []

    def submit():
        results.append(
            trader._send_close_leg("rb2405", "sell", 1, "close", 3500)
        )

    first = threading.Thread(target=submit, daemon=True)
    second = threading.Thread(target=submit, daemon=True)
    first.start()
    assert first_insert_entered.wait(timeout=1)
    second.start()
    race_happened = second_insert_entered.wait(timeout=0.2)
    allow_first_return.set()
    first.join(timeout=2)
    second.join(timeout=2)

    assert race_happened is False
    assert not first.is_alive()
    assert not second.is_alive()
    assert insert_count == 1
    assert results == [0, 0]


def test_position_queries_are_serialized_around_shared_completion_event():
    """单个 position event 只能服务一个查询窗口，跨 code 查询必须全局串行。"""
    from chanlun.trader import trader_ctp as mod

    state = CTPState()
    first_query_entered = threading.Event()
    allow_first_finish = threading.Event()
    second_query_entered = threading.Event()
    calls_lock = threading.Lock()
    query_count = 0

    class FakeTraderApi:
        def __init__(self):
            self.state = state

        def ReqQryInvestorPosition(self, req, request_id):
            nonlocal query_count
            with calls_lock:
                query_count += 1
                call_no = query_count
            if call_no == 1:
                first_query_entered.set()
                allow_first_finish.wait(timeout=2)
            else:
                second_query_entered.set()
            state.mark_position_query_done()
            return 0

    trader = mod.CTPTrader.__new__(mod.CTPTrader)
    trader.trader_api = FakeTraderApi()
    results = []

    def query(code):
        results.append(
            trader._execute_position_query(SimpleNamespace(InstrumentID=code))
        )

    first = threading.Thread(target=query, args=("rb2405",), daemon=True)
    second = threading.Thread(target=query, args=("au2406",), daemon=True)
    first.start()
    assert first_query_entered.wait(timeout=1)
    second.start()
    crossed = second_query_entered.wait(timeout=0.2)
    allow_first_finish.set()
    first.join(timeout=2)
    second.join(timeout=2)

    assert crossed is False
    assert not first.is_alive()
    assert not second.is_alive()
    assert query_count == 2
    assert results == [True, True]


def test_scoped_position_query_prunes_stale_same_code_only():
    """单 code 查询无返回表示该 code 已空仓，须剔除旧快照且不碰其它合约。"""
    from chanlun.trader import trader_ctp as mod

    state = CTPState()
    state.set_position("rb2405_2", FakePosInfo("rb2405", "2", 2))
    state.set_position("au2406_2", FakePosInfo("au2406", "2", 1))

    class FakeTraderApi:
        def __init__(self):
            self.state = state

        def ReqQryInvestorPosition(self, req, request_id):
            state.mark_position_query_done(request_id)
            return 0

    trader = mod.CTPTrader.__new__(mod.CTPTrader)
    trader.trader_api = FakeTraderApi()

    assert trader._execute_position_query(
        SimpleNamespace(InstrumentID="rb2405"), reconcile_scope="rb2405"
    )
    snapshot = state.get_positions_snapshot()
    assert "rb2405_2" not in snapshot
    assert "au2406_2" in snapshot


def test_late_timed_out_position_callback_cannot_wake_or_pollute_next_query(
    monkeypatch,
):
    """A 超时后的迟到行/bIsLast 必须按 request_id 隔离，不能污染或唤醒 B。"""
    from chanlun.trader import trader_ctp as mod

    state = CTPState()
    request_ids = []

    class FakeTraderApi:
        def __init__(self):
            self.state = state

        def ReqQryInvestorPosition(self, req, request_id):
            request_ids.append(request_id)
            if len(request_ids) == 1:
                return 0  # A 永不按时完成
            stale_id = request_ids[0]
            state.set_position(
                "late_2", FakePosInfo("late", "2", 9), request_id=stale_id
            )
            state.mark_position_query_done(stale_id)
            state.set_position(
                "au2406_2", FakePosInfo("au2406", "2", 1), request_id=request_id
            )
            state.mark_position_query_done(request_id)
            return 0

    trader = mod.CTPTrader.__new__(mod.CTPTrader)
    trader.trader_api = FakeTraderApi()
    monkeypatch.setattr(mod, "_CTP_CALLBACK_TIMEOUT", 0.01)

    assert trader._execute_position_query(SimpleNamespace(InstrumentID="rb2405")) is False
    assert trader._execute_position_query(SimpleNamespace(InstrumentID="au2406")) is True
    snapshot = state.get_positions_snapshot()
    assert "late_2" not in snapshot
    assert snapshot["au2406_2"].Position == 1


def test_position_query_error_does_not_prune_stale_scope_as_empty():
    """错误响应不是权威空仓；必须返回失败并保留旧快照。"""
    from chanlun.trader import trader_ctp as mod

    callback = mod.MyTraderCallback(SimpleNamespace())
    callback.state.set_position(
        "rb2405_2", FakePosInfo("rb2405", "2", 2)
    )

    def reject_query(req, request_id):
        callback.OnRspQryInvestorPosition(
            None,
            SimpleNamespace(ErrorID=7, ErrorMsg="query rejected"),
            request_id,
            True,
        )
        return 0

    callback.ReqQryInvestorPosition = reject_query
    trader = mod.CTPTrader.__new__(mod.CTPTrader)
    trader.trader_api = callback

    assert (
        trader._execute_position_query(
            SimpleNamespace(InstrumentID="rb2405"), reconcile_scope="rb2405"
        )
        is False
    )
    assert callback.state.get_positions_snapshot()["rb2405_2"].Position == 2
