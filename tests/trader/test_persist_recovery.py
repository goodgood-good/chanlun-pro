"""H3: order_ref 持久化恢复 (H3-c) + WAL 意图写入/清除 (H3-b)。

用内存 dict 替身 fdb 的 pkl 读写, 不触碰真实缓存目录。
"""

import chanlun.trading.backtest_trader as mod
from chanlun.trading.backtest_trader import BackTestTrader
from chanlun.trading.base import Operation


def _mem_fdb(monkeypatch):
    """把 fdb.cache_pkl_to_file / from_file 换成内存 dict。"""
    store = {}
    monkeypatch.setattr(mod.fdb, "cache_pkl_to_file", lambda k, v: store.__setitem__(k, v))
    monkeypatch.setattr(mod.fdb, "cache_pkl_from_file", lambda k: store.get(k))
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


def test_wal_write_failure_does_not_raise(monkeypatch):
    """WAL 写盘异常时退化, 不抛断下单。"""
    tr = _online_trader()
    tr._pkl_key = "trader_x"

    def _boom(*a, **k):
        raise RuntimeError("磁盘满")

    monkeypatch.setattr(mod.fdb, "cache_pkl_from_file", lambda k: {})
    monkeypatch.setattr(mod.fdb, "cache_pkl_to_file", _boom)
    opt = Operation("rb2405", "open", "1buy", key="k1")
    # 不抛
    tr.wal_write_intent("rb2405", opt)


def test_pkl_key_set_on_save_and_load(monkeypatch):
    _mem_fdb(monkeypatch)
    tr = _online_trader()
    tr.save_to_pkl("trader_save")
    assert tr._pkl_key == "trader_save"

    tr2 = _online_trader()
    tr2.load_from_pkl("trader_save")
    assert tr2._pkl_key == "trader_save"
