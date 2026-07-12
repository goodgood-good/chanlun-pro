"""H3: order_ref 持久化恢复 (H3-c) + WAL 意图写入/清除 (H3-b)。

用内存 dict 替身 fdb 的 pkl 读写, 不触碰真实缓存目录。
"""

import copy
import threading

import pytest

import chanlun.trading.backtest_trader as mod
from chanlun.trading.backtest_trader import BackTestTrader
from chanlun.trading.base import Operation


def _mem_fdb(monkeypatch):
    """把 fdb.cache_pkl_to_file / from_file 换成内存 dict。"""
    store = {}
    monkeypatch.setattr(
        mod.fdb,
        "cache_pkl_to_file",
        lambda k, v, *, wait=False: store.__setitem__(k, v),
    )
    monkeypatch.setattr(
        mod.fdb,
        "cache_pkl_from_file",
        lambda k, *, strict=False: store.get(k),
    )
    return store


def _online_trader():
    tr = BackTestTrader("t", mode="online", market="futures")
    tr.log = lambda *a, **k: None
    return tr


# ---------- H3-c order_ref 进出 pkl ----------
def test_save_includes_ctp_order_ref(monkeypatch):
    store = _mem_fdb(monkeypatch)
    tr = _online_trader()
    tr._ctp_order_ref_snapshot = 42
    tr.save_to_pkl("trader_x")
    assert store["trader_x"]["ctp_order_ref"] == 42


def test_load_restores_ctp_order_ref_snapshot(monkeypatch):
    _mem_fdb(monkeypatch)
    tr = _online_trader()
    tr._ctp_order_ref_snapshot = 7
    tr.save_to_pkl("trader_x")

    tr2 = _online_trader()
    tr2.load_from_pkl("trader_x")
    assert tr2._ctp_order_ref_snapshot == 7


def test_load_old_pkl_without_key_defaults_none(monkeypatch):
    """旧 pkl 无 ctp_order_ref 键时 → None (向后兼容不 KeyError)。"""
    store = _mem_fdb(monkeypatch)
    tr = _online_trader()
    tr.save_to_pkl("trader_x")
    # 模拟旧 pkl: 删掉新键
    del store["trader_x"]["ctp_order_ref"]

    tr2 = _online_trader()
    tr2.load_from_pkl("trader_x")
    assert tr2._ctp_order_ref_snapshot is None


# ---------- H3-b WAL ----------
def test_wal_write_and_clear_intent(monkeypatch):
    store = _mem_fdb(monkeypatch)
    tr = _online_trader()
    tr._pkl_key = "trader_x"
    opt = Operation("rb2405", "open", "1buy", key="k1", open_uid="rb2405:1buy")

    tr.wal_write_intent("rb2405", opt, order_ref="9")
    wal = store["trader_x__wal"]
    assert "rb2405:rb2405:1buy:k1" in wal
    assert wal["rb2405:rb2405:1buy:k1"]["order_ref"] == "9"

    tr.wal_clear_intent("rb2405", opt)
    assert "rb2405:rb2405:1buy:k1" not in store["trader_x__wal"]


def test_wal_signal_mode_noop(monkeypatch):
    """signal 回测模式不落 WAL。"""
    store = _mem_fdb(monkeypatch)
    tr = BackTestTrader("t", mode="signal", market="futures")
    tr._pkl_key = "trader_x"
    opt = Operation("rb2405", "open", "1buy", key="k1")
    tr.wal_write_intent("rb2405", opt)
    assert "trader_x__wal" not in store


def test_wal_key_none_when_no_pkl_key(monkeypatch):
    _mem_fdb(monkeypatch)
    tr = _online_trader()
    # 未设 _pkl_key
    assert tr._wal_key() is None


def test_wal_write_failure_blocks_order_submission(monkeypatch):
    """WAL 写盘异常必须向调用方冒泡，禁止在无恢复凭据时继续下单。"""
    tr = _online_trader()
    tr._pkl_key = "trader_x"

    def _boom(*a, **k):
        raise RuntimeError("磁盘满")

    monkeypatch.setattr(
        mod.fdb, "cache_pkl_from_file", lambda k, *, strict=False: {}
    )
    monkeypatch.setattr(mod.fdb, "cache_pkl_to_file", _boom)
    opt = Operation("rb2405", "open", "1buy", key="k1")
    with pytest.raises(RuntimeError, match="磁盘满"):
        tr.wal_write_intent("rb2405", opt)


def test_trader_snapshot_and_wal_writes_are_synchronous(monkeypatch):
    writes = []
    store = {}

    def write(key, value, *, wait=False):
        writes.append((key, wait))
        store[key] = value

    monkeypatch.setattr(mod.fdb, "cache_pkl_to_file", write)
    monkeypatch.setattr(
        mod.fdb,
        "cache_pkl_from_file",
        lambda key, *, strict=False: store.get(key),
    )
    tr = _online_trader()
    tr.save_to_pkl("trader_x")
    opt = Operation("rb2405", "open", "1buy", key="k1")
    tr.wal_write_intent("rb2405", opt)
    tr.wal_clear_intent("rb2405", opt)

    assert writes == [
        ("trader_x", True),
        ("trader_x__wal", True),
        ("trader_x__wal", True),
    ]


def test_execute_persists_wal_before_broker_and_snapshot_before_clear(monkeypatch):
    events = []
    store = {}

    def write(key, value, *, wait=False):
        events.append(f"write:{key}:{wait}")
        store[key] = value

    monkeypatch.setattr(mod.fdb, "cache_pkl_to_file", write)
    monkeypatch.setattr(
        mod.fdb,
        "cache_pkl_from_file",
        lambda key, *, strict=False: store.get(key),
    )
    tr = _online_trader()
    tr.market = "us"
    tr._pkl_key = "trader_x"

    def broker_open(code, opt):
        events.append("broker")
        assert events[-2] == "write:trader_x__wal:True"
        return {"price": 10.0, "amount": 1.0}

    tr.open_buy = broker_open
    opt = Operation("rb2405", "buy", "1buy", key="k1", open_uid="rb2405:1buy")

    assert tr.execute("rb2405", opt) is True
    assert events == [
        "write:trader_x__wal:True",
        "broker",
        "write:trader_x:True",
        "write:trader_x__wal:True",
    ]
    assert store["trader_x__wal"] == {}


def test_execute_does_not_call_broker_when_wal_persistence_fails(monkeypatch):
    tr = _online_trader()
    tr._pkl_key = "trader_x"
    broker_called = False

    monkeypatch.setattr(
        mod.fdb, "cache_pkl_from_file", lambda key, *, strict=False: {}
    )

    def fail_write(key, value, *, wait=False):
        raise OSError("wal disk full")

    def broker_open(code, opt):
        nonlocal broker_called
        broker_called = True
        return {"price": 10.0, "amount": 1.0}

    monkeypatch.setattr(mod.fdb, "cache_pkl_to_file", fail_write)
    tr.open_buy = broker_open
    opt = Operation("rb2405", "buy", "1buy", key="k1", open_uid="rb2405:1buy")

    with pytest.raises(OSError, match="wal disk full"):
        tr.execute("rb2405", opt)
    assert broker_called is False


def test_online_execute_without_persistence_key_is_fail_closed():
    tr = _online_trader()
    broker_called = False

    def broker_open(code, opt):
        nonlocal broker_called
        broker_called = True
        return {"price": 10.0, "amount": 1.0}

    tr.open_buy = broker_open
    opt = Operation("rb2405", "buy", "1buy", key="k1", open_uid="rb2405:1buy")

    with pytest.raises(RuntimeError, match="未配置持久化 key"):
        tr.execute("rb2405", opt)
    assert broker_called is False


def test_wal_read_modify_write_is_serialized_across_codes(monkeypatch):
    tr = _online_trader()
    tr._pkl_key = "trader_x"
    store = {"trader_x__wal": {}}
    first_read = threading.Event()
    release_first = threading.Event()
    second_read = threading.Event()
    read_count = 0

    def read(key, *, strict=False):
        nonlocal read_count
        read_count += 1
        if read_count == 1:
            first_read.set()
            release_first.wait(timeout=2)
        else:
            second_read.set()
        return copy.deepcopy(store.get(key))

    def write(key, value, *, wait=False):
        store[key] = copy.deepcopy(value)

    monkeypatch.setattr(mod.fdb, "cache_pkl_from_file", read)
    monkeypatch.setattr(mod.fdb, "cache_pkl_to_file", write)
    first_opt = Operation("rb2405", "open", "1buy", key="k1", open_uid="rb:1buy")
    second_opt = Operation("au2406", "open", "1buy", key="k2", open_uid="au:1buy")

    first = threading.Thread(target=tr.wal_write_intent, args=("rb2405", first_opt))
    second = threading.Thread(target=tr.wal_write_intent, args=("au2406", second_opt))
    first.start()
    assert first_read.wait(timeout=1)
    second.start()
    crossed = second_read.wait(timeout=0.2)
    release_first.set()
    first.join(timeout=2)
    second.join(timeout=2)

    assert crossed is False
    assert not first.is_alive()
    assert not second.is_alive()
    assert set(store["trader_x__wal"]) == {"rb2405:rb:1buy:k1", "au2406:au:1buy:k2"}


def test_execute_clears_wal_after_confirmed_zero_fill(monkeypatch):
    store = {}
    writes = []

    def write(key, value, *, wait=False):
        writes.append(key)
        store[key] = copy.deepcopy(value)

    monkeypatch.setattr(mod.fdb, "cache_pkl_to_file", write)
    monkeypatch.setattr(
        mod.fdb,
        "cache_pkl_from_file",
        lambda key, *, strict=False: store.get(key),
    )
    tr = _online_trader()
    tr._pkl_key = "trader_x"
    tr.open_buy = lambda code, opt: {"price": 10.0, "amount": 0.0}
    opt = Operation("rb2405", "buy", "1buy", key="k1", open_uid="rb2405:1buy")

    assert tr.execute("rb2405", opt) is False
    assert writes == ["trader_x__wal", "trader_x__wal"]
    assert store["trader_x__wal"] == {}


def test_pkl_key_set_on_save_and_load(monkeypatch):
    _mem_fdb(monkeypatch)
    tr = _online_trader()
    tr.save_to_pkl("trader_save")
    assert tr._pkl_key == "trader_save"

    tr2 = _online_trader()
    tr2.load_from_pkl("trader_save")
    assert tr2._pkl_key == "trader_save"
