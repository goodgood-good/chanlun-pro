import pytest

from cl_app.services import stock_list


LKG = [{"code": "SZ.000001", "name": "Ping An", "type": "stock_cn"}]
RAW = [{"code": "SH.600000", "name": "Pufa", "type": "stock_cn"}]


class _FakeExchange:
    def __init__(self, stocks=None, *, init_failed=False, error=None):
        self.stocks = stocks
        self.init_failed = init_failed
        self.error = error

    def all_stocks(self, _market=None):
        if self.error is not None:
            raise self.error
        return self.stocks


@pytest.fixture(autouse=True)
def _reset_symbol_state():
    with stock_list._stock_cache_lock:
        stock_list.stock_cache.clear()
        getattr(stock_list, "_symbol_states", {}).clear()
    yield
    with stock_list._stock_cache_lock:
        stock_list.stock_cache.clear()
        getattr(stock_list, "_symbol_states", {}).clear()


def test_empty_preload_refresh_preserves_lkg_and_marks_degraded(monkeypatch):
    with stock_list._stock_cache_lock:
        stock_list.stock_cache["a"] = list(LKG)
    monkeypatch.setattr(
        stock_list, "get_exchange", lambda _market: _FakeExchange([])
    )
    monkeypatch.setattr(stock_list, "_save_stocks_to_disk", lambda *_args: None)

    stock_list._preload_single_exchange("a")

    with stock_list._stock_cache_lock:
        assert stock_list.stock_cache["a"] == LKG
    snapshot = stock_list.get_symbol_readiness("a")
    assert snapshot["ready"] is True
    assert snapshot["status"] == "degraded"
    assert snapshot["count"] == 1
    assert snapshot["last_error"]


@pytest.mark.parametrize("mode", ["init_failed", "exception"])
def test_preload_failures_keep_lkg_and_mark_degraded(monkeypatch, mode):
    with stock_list._stock_cache_lock:
        stock_list.stock_cache["a"] = list(LKG)
    fake = (
        _FakeExchange(init_failed=True)
        if mode == "init_failed"
        else _FakeExchange(error=RuntimeError("symbols unavailable"))
    )
    monkeypatch.setattr(stock_list, "get_exchange", lambda _market: fake)

    stock_list._preload_single_exchange("a")

    with stock_list._stock_cache_lock:
        assert stock_list.stock_cache["a"] == LKG
    snapshot = stock_list.get_symbol_readiness("a")
    assert snapshot["ready"] is True
    assert snapshot["status"] == "degraded"
    assert snapshot["last_error"]


def test_empty_preload_without_lkg_is_not_ready(monkeypatch):
    monkeypatch.setattr(
        stock_list, "get_exchange", lambda _market: _FakeExchange([])
    )
    monkeypatch.setattr(stock_list, "_save_stocks_to_disk", lambda *_args: None)

    stock_list._preload_single_exchange("a")

    snapshot = stock_list.get_symbol_readiness("a")
    assert snapshot == {
        "market": "a",
        "ready": False,
        "status": "degraded",
        "count": 0,
        "last_error": "empty symbol list",
    }


def test_disk_restore_marks_symbols_ready(monkeypatch):
    monkeypatch.setattr(stock_list, "PRELOAD_EXCHANGES", ["a"])
    monkeypatch.setattr(
        stock_list, "_load_stocks_from_disk", lambda _market: list(RAW)
    )

    stock_list._warm_cache_from_disk()

    snapshot = stock_list.get_symbol_readiness("a")
    assert snapshot == {
        "market": "a",
        "ready": True,
        "status": "ready",
        "count": 1,
        "last_error": None,
    }


def test_sync_empty_result_cannot_overwrite_concurrent_lkg(monkeypatch):
    def _get_exchange_with_concurrent_lkg(_market):
        with stock_list._stock_cache_lock:
            stock_list.stock_cache["a"] = list(LKG)
        return _FakeExchange([])

    monkeypatch.setattr(stock_list, "get_exchange", _get_exchange_with_concurrent_lkg)
    monkeypatch.setattr(stock_list, "_save_stocks_to_disk", lambda *_args: None)

    result = stock_list.get_cached_processed_stocks(
        "a", allow_sync_fallback=True
    )

    assert result == LKG
    with stock_list._stock_cache_lock:
        assert stock_list.stock_cache["a"] == LKG
    snapshot = stock_list.get_symbol_readiness("a")
    assert snapshot["ready"] is True
    assert snapshot["status"] == "degraded"
    assert snapshot["last_error"] == "empty symbol list"


def test_lkg_survives_refresh_ttl_after_failures(monkeypatch):
    now = [0.0]
    timer = getattr(stock_list.stock_cache, "timer", None)
    if timer is not None:
        monkeypatch.setattr(timer, "_Timer__timer", lambda: now[0])

    with stock_list._stock_cache_lock:
        stock_list.stock_cache["a"] = list(LKG)
        stock_list._symbol_states["a"] = {
            "status": "degraded",
            "last_error": "symbols unavailable",
        }

    now[0] = 7201.0
    snapshot = stock_list.get_symbol_readiness("a")

    assert snapshot["ready"] is True
    assert snapshot["status"] == "degraded"
    assert snapshot["count"] == 1
    assert stock_list._cached_symbols_or_empty("a") == LKG


def test_first_preload_round_refreshes_disk_warmed_symbols(monkeypatch):
    calls = []

    def record_refresh(exchange, skip_if_disk_warm=False):
        calls.append((exchange, skip_if_disk_warm))

    def stop_after_first_round(_seconds):
        raise RuntimeError("stop after first refresh")

    monkeypatch.setattr(stock_list, "PRELOAD_EXCHANGES", ["a"])
    monkeypatch.setattr(stock_list, "PRELOAD_PARALLEL_WORKERS", 1)
    monkeypatch.setattr(stock_list, "PRELOAD_STARTUP_DELAY_SECONDS", 0)
    monkeypatch.setattr(stock_list, "_preload_single_exchange", record_refresh)
    monkeypatch.setattr(stock_list.time, "sleep", stop_after_first_round)

    with pytest.raises(RuntimeError, match="stop after first refresh"):
        stock_list.preload_symbols()

    assert calls == [("a", False)]


def test_disabled_preload_is_ready_without_symbol_cache(monkeypatch):
    monkeypatch.setattr(stock_list, "PRELOAD_EXCHANGES", [])

    snapshot = stock_list.get_symbol_readiness("a")

    assert snapshot == {
        "market": "a",
        "ready": True,
        "status": "disabled",
        "count": 0,
        "last_error": None,
    }