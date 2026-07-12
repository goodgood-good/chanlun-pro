import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

import chanlun.trading.backtest_trader as backtest_mod
from chanlun.trader._ctp_state import CTPState
from chanlun.trading.backtest_trader import BackTestTrader
from chanlun.trading.base import POSITION, Operation


def _bare_trader():
    from chanlun.trader.trader_ctp import CTPTrader

    trader = CTPTrader.__new__(CTPTrader)
    BackTestTrader.__init__(trader, "ctp", mode="online", market="futures")
    trader.ex = SimpleNamespace(broker_id="9999", user_id="test")
    trader.trader_api = SimpleNamespace(
        state=CTPState(),
        front_id=1,
        session_id=2,
    )
    return trader


def _order(ref="8", status="3", volume=0):
    return SimpleNamespace(
        OrderRef=ref,
        InstrumentID="rb2405",
        OrderStatus=status,
        VolumeTraded=volume,
        ExchangeID="SHFE",
        OrderSysID=f"sys-{ref}",
        FrontID=7,
        SessionID=9,
    )


def _trade():
    return SimpleNamespace(
        TradeID="trade-1",
        ExchangeID="SHFE",
        OrderSysID="sys-8",
        OrderRef="8",
        InstrumentID="rb2405",
        Price=3500.0,
        Volume=1,
    )


def test_ctp_save_and_load_round_trips_durable_order_state(monkeypatch):
    store = {}
    monkeypatch.setattr(
        backtest_mod.fdb,
        "cache_pkl_to_file",
        lambda key, value, *, wait=False: store.__setitem__(key, value),
    )
    monkeypatch.setattr(
        backtest_mod.fdb, "cache_pkl_from_file", lambda key: store.get(key)
    )
    trader = _bare_trader()
    trader.trader_api.state.restore_order_ref(8)
    trader.trader_api.state.set_order("8", _order())
    trader.trader_api.state.set_trade(_trade())
    trader.trader_api.state.mark_order_reconciliation_required("rb2405", "8", 1)

    trader.save_to_pkl("ctp-state")
    assert store["ctp-state"]["ctp_state"]["orders"]["8"]["OrderSysID"] == "sys-8"

    restored = _bare_trader()
    assert restored.load_from_pkl("ctp-state") is True
    assert restored.trader_api.state.get_order("8").OrderSysID == "sys-8"
    assert len(restored.trader_api.state.get_trades_snapshot()) == 1
    assert restored.trader_api.state.get_order_reconciliation_required("rb2405") == {
        "8": 1
    }
    assert restored.trader_api.state.is_reconciliation_ready() is False


def test_reconciliation_barrier_blocks_ctp_order_entry():
    trader = _bare_trader()
    trader.trader_api.state.require_reconciliation("restart")

    result = trader.open_buy(
        "rb2405", Operation("rb2405", "buy", "1buy", key="k1")
    )

    assert result is False


def test_disconnect_requires_new_reconciliation():
    from chanlun.trader.trader_ctp import MyTraderCallback

    callback = MyTraderCallback(SimpleNamespace())
    callback.connected = True
    callback.logged_in = True

    callback.OnFrontDisconnected(0x1001)

    assert callback.connected is False
    assert callback.logged_in is False
    assert callback.state.is_reconciliation_ready() is False


def test_order_and_trade_callbacks_persist_and_trade_replay_is_idempotent():
    from chanlun.trader.trader_ctp import MyTraderCallback

    persisted = []
    owner = SimpleNamespace(_persist_ctp_state=lambda: persisted.append("saved"))
    callback = MyTraderCallback(owner)

    callback.OnRtnOrder(_order())
    callback.OnRtnTrade(_trade())
    callback.OnRtnTrade(_trade())

    assert callback.state.get_order("8").OrderSysID == "sys-8"
    assert len(callback.state.get_trades_snapshot()) == 1
    assert persisted == ["saved", "saved"]


def test_query_orders_and_trades_wait_for_authoritative_callbacks():
    from chanlun.trader.trader_ctp import MyTraderCallback

    callback = MyTraderCallback(SimpleNamespace())
    trader = _bare_trader()
    trader.trader_api = callback
    callback.trader = trader

    def query_order(req, request_id):
        callback.OnRspQryOrder(_order(), None, request_id, True)
        return 0

    def query_trade(req, request_id):
        callback.OnRspQryTrade(_trade(), None, request_id, True)
        return 0

    callback.ReqQryOrder = query_order
    callback.ReqQryTrade = query_trade

    assert trader.query_orders() is True
    assert trader.query_trades() is True
    assert callback.state.get_order("8").OrderSysID == "sys-8"
    assert len(callback.state.get_trades_snapshot()) == 1


def test_cancel_after_restart_uses_exchange_and_order_system_identity():
    trader = _bare_trader()
    state = trader.trader_api.state
    state.set_order("8", _order())
    sent = []

    def cancel(req, request_id):
        sent.append(req)
        state.set_order("8", _order(status="5"))
        return 0

    trader.trader_api.ReqOrderAction = cancel

    assert trader.cancel_order("8") is True
    assert sent[0].ExchangeID == "SHFE"
    assert sent[0].OrderSysID == "sys-8"


def test_reconcile_recovery_clears_barrier_only_when_positions_match():
    trader = _bare_trader()
    state = trader.trader_api.state
    state.require_reconciliation("restart")
    local = POSITION(code="rb2405", mmd="1buy", amount=2)
    trader.positions["rb2405:1buy"] = local
    state.set_position(
        "rb2405_2",
        SimpleNamespace(InstrumentID="rb2405", PosiDirection="2", Position=2),
    )
    trader.query_orders = lambda code="": True
    trader.query_trades = lambda code="": True
    trader._execute_position_query = lambda req, reconcile_scope=None: True

    assert trader.reconcile_recovery() is True
    assert state.is_reconciliation_ready() is True


def test_reconcile_recovery_keeps_barrier_on_position_mismatch():
    trader = _bare_trader()
    state = trader.trader_api.state
    state.require_reconciliation("restart")
    local = POSITION(code="rb2405", mmd="1buy", amount=2)
    trader.positions["rb2405:1buy"] = local
    state.set_position(
        "rb2405_2",
        SimpleNamespace(InstrumentID="rb2405", PosiDirection="2", Position=1),
    )
    trader.query_orders = lambda code="": True
    trader.query_trades = lambda code="": True
    trader._execute_position_query = lambda req, reconcile_scope=None: True

    assert trader.reconcile_recovery() is False
    assert state.is_reconciliation_ready() is False


def test_reconcile_recovery_keeps_quarantine_for_unaccounted_trade_delta():
    trader = _bare_trader()
    state = trader.trader_api.state
    state.require_reconciliation("restart")
    state.set_order("8", _order(status="5", volume=2))
    state.mark_order_reconciliation_required("rb2405", "8", 1)
    trader.query_orders = lambda code="": True

    def query_trades(code=""):
        state.set_trade(
            SimpleNamespace(
                TradeID="late-trade",
                ExchangeID="SHFE",
                OrderRef="8",
                InstrumentID="rb2405",
                Price=3501.0,
                Volume=2,
            )
        )
        return True

    trader.query_trades = query_trades
    trader._execute_position_query = lambda req, reconcile_scope=None: True

    assert trader.reconcile_recovery() is False
    assert state.get_order_reconciliation_required("rb2405") == {"8": 1}
    assert "unaccounted" in state.get_reconciliation_reason()


def test_reconcile_recovery_blocks_new_trade_id_even_when_net_position_matches():
    trader = _bare_trader()
    state = trader.trader_api.state
    state.require_reconciliation("restart")
    trader.query_orders = lambda code="": True

    def query_trades(code=""):
        state.set_trade(
            SimpleNamespace(
                TradeID="previously-unseen",
                ExchangeID="SHFE",
                OrderRef="99",
                InstrumentID="rb2405",
                Price=3500.0,
                Volume=1,
            )
        )
        return True

    trader.query_trades = query_trades
    trader._execute_position_query = lambda req, reconcile_scope=None: True

    assert trader.reconcile_recovery() is False
    assert state.is_reconciliation_ready() is False
    assert "new trade" in state.get_reconciliation_reason()


def test_load_merges_callbacks_that_arrived_before_snapshot_import(monkeypatch):
    store = {}
    monkeypatch.setattr(
        backtest_mod.fdb,
        "cache_pkl_to_file",
        lambda key, value, *, wait=False: store.__setitem__(key, value),
    )
    monkeypatch.setattr(
        backtest_mod.fdb, "cache_pkl_from_file", lambda key: store.get(key)
    )
    saved = _bare_trader()
    saved.trader_api.state.restore_order_ref(8)
    saved.trader_api.state.set_order("8", _order(status="3", volume=1))
    saved.trader_api.state.set_trade(_trade())
    saved.save_to_pkl("ctp-state")

    restored = _bare_trader()
    restored.trader_api.state.restore_order_ref(20)
    restored.trader_api.state.set_order("8", _order(status="5", volume=2))
    restored.trader_api.state.set_trade(
        SimpleNamespace(
            TradeID="trade-live",
            ExchangeID="SHFE",
            OrderRef="8",
            InstrumentID="rb2405",
            Price=3502.0,
            Volume=1,
        )
    )

    assert restored.load_from_pkl("ctp-state") is True
    assert restored.trader_api.state.order_ref >= 20
    assert restored.trader_api.state.get_order("8").OrderStatus == "5"
    assert restored.trader_api.state.get_order("8").VolumeTraded == 2
    assert set(restored.trader_api.state.get_trades_snapshot()) == {
        "SHFE:trade-1",
        "SHFE:trade-live",
    }


def test_ensure_recovery_ready_is_fail_closed():
    trader = _bare_trader()
    trader.reconcile_recovery = lambda: False

    with pytest.raises(RuntimeError, match="恢复对账失败"):
        trader.ensure_recovery_ready()

    trader.reconcile_recovery = lambda: True
    assert trader.ensure_recovery_ready() is True


def test_reconnect_reconciliation_worker_is_single_flight_and_bounded():
    trader = _bare_trader()
    entered = threading.Event()
    release = threading.Event()
    calls = 0

    def reconcile():
        nonlocal calls
        calls += 1
        entered.set()
        release.wait(timeout=2)
        return False

    trader.reconcile_recovery = reconcile
    trader._reconcile_retry_delay = 0

    assert trader.schedule_reconcile_recovery(max_attempts=2) is True
    assert entered.wait(timeout=1)
    assert trader.schedule_reconcile_recovery(max_attempts=2) is False
    release.set()
    trader._reconcile_thread.join(timeout=2)

    assert not trader._reconcile_thread.is_alive()
    assert calls == 2
    assert trader.trader_api.state.is_reconciliation_ready() is False


def test_reconnect_reconciliation_worker_can_release_barrier_asynchronously():
    trader = _bare_trader()
    state = trader.trader_api.state
    state.require_reconciliation("reconnect")

    def reconcile():
        state.complete_reconciliation()
        return True

    trader.reconcile_recovery = reconcile
    assert trader.schedule_reconcile_recovery(max_attempts=1) is True
    trader._reconcile_thread.join(timeout=2)

    assert not trader._reconcile_thread.is_alive()
    assert state.is_reconciliation_ready() is True


def test_reconnect_login_schedules_reconciliation_off_callback_thread():
    from chanlun.trader.trader_ctp import MyTraderCallback

    scheduled = []
    owner = SimpleNamespace(schedule_reconcile_recovery=lambda: scheduled.append(True))
    callback = MyTraderCallback(owner)
    login = SimpleNamespace(FrontID=1, SessionID=2)
    ok = SimpleNamespace(ErrorID=0, ErrorMsg="")

    callback.OnRspUserLogin(login, ok, 1, True)
    assert scheduled == []
    callback.OnFrontDisconnected(0x1001)
    callback.OnRspUserLogin(login, ok, 2, True)

    assert scheduled == [True]


def test_production_startup_script_uses_fail_closed_recovery_gate():
    root = Path(__file__).resolve().parents[2]
    source = (root / "script" / "trader" / "reboot_trader_ctp.py").read_text(
        encoding="utf-8"
    )

    assert source.index("TR.load_from_pkl") < source.index("TR.ensure_recovery_ready()")
