from pathlib import Path

import pytest

from cl_app.services import stock_list


LKG = [{"code": "SZ.000001", "name": "Ping An", "type": "stock_cn"}]
RAW = [{"code": "SH.600000", "name": "Pufa", "type": "stock_cn"}]
ROOT = Path(__file__).resolve().parents[2]


class _FakeExchange:
    stock_info_query_scope = "SINGLE_SYMBOL_STOCK_INFO"

    def __init__(self, stocks=None, *, init_failed=False, error=None):
        self.stocks = stocks
        self.init_failed = init_failed
        self.error = error
        self.all_stocks_calls = 0
        self.stock_info_calls = []

    def all_stocks(self, _market=None):
        self.all_stocks_calls += 1
        if self.error is not None:
            raise self.error
        return self.stocks

    def stock_info(self, code):
        self.stock_info_calls.append(code)
        if self.error is not None:
            raise self.error
        return next(
            (
                stock
                for stock in (self.stocks or ())
                if stock.get("code") == code
            ),
            None,
        )


@pytest.fixture(autouse=True)
def _reset_symbol_state():
    # ``test_runtime_boundedness`` intentionally closes the process-wide symbol
    # runtime.  These unit tests exercise the refresh primitives directly, so
    # they must not inherit that lifecycle state from an earlier test module.
    with stock_list._preload_handle_lock:
        previous_runtime_closed = stock_list._symbol_runtime_closed
        stock_list._symbol_runtime_closed = False
    previous_mode, previous_codes, previous_full_authorized = (
        stock_list._catalog_scope_snapshot("a")
    )
    stock_list.configure_symbol_catalog(
        validation_codes=("SH.600000", "SZ.000001"),
        full_catalog_authorized=False,
    )
    with stock_list._stock_cache_lock:
        stock_list.stock_cache.clear()
        getattr(stock_list, "_symbol_states", {}).clear()
        getattr(stock_list, "_disk_cache_metadata", {}).clear()
    yield
    with stock_list._stock_cache_lock:
        stock_list.stock_cache.clear()
        getattr(stock_list, "_symbol_states", {}).clear()
        getattr(stock_list, "_disk_cache_metadata", {}).clear()
    with stock_list._preload_handle_lock:
        stock_list._symbol_runtime_closed = previous_runtime_closed
    stock_list.configure_symbol_catalog(
        validation_codes=previous_codes,
        full_catalog_authorized=(
            previous_mode == stock_list.FULL_IDENTITY_CATALOG
            and previous_full_authorized
        ),
    )


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
        "catalog_mode": stock_list.BOUNDED_VALIDATION_CATALOG,
        "admitted_count": 2,
        "full_catalog_authorized": False,
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
        "catalog_mode": stock_list.BOUNDED_VALIDATION_CATALOG,
        "admitted_count": 2,
        "full_catalog_authorized": False,
    }


def test_disk_warmed_first_round_does_not_open_exchange(monkeypatch):
    stock_list.configure_symbol_catalog(
        validation_codes=("SZ.000001",),
        full_catalog_authorized=False,
    )
    with stock_list._stock_cache_lock:
        stock_list.stock_cache["a"] = list(LKG)

    def fail_if_exchange_is_opened(_market):
        raise AssertionError("disk-warmed startup must not access the exchange")

    monkeypatch.setattr(stock_list, "get_exchange", fail_if_exchange_is_opened)

    stock_list._preload_single_exchange("a", skip_if_disk_warm=True)

    assert stock_list.get_symbol_readiness("a") == {
        "market": "a",
        "ready": True,
        "status": "ready",
        "count": 1,
        "last_error": None,
        "catalog_mode": stock_list.BOUNDED_VALIDATION_CATALOG,
        "admitted_count": 1,
        "full_catalog_authorized": False,
    }


def test_full_catalog_first_round_replaces_bounded_disk_warm_cache(monkeypatch):
    stock_list.configure_symbol_catalog(
        validation_codes=("SZ.000001",),
        full_catalog_authorized=True,
    )
    with stock_list._stock_cache_lock:
        stock_list.stock_cache["a"] = list(LKG)
    fake = _FakeExchange(list(RAW))
    monkeypatch.setattr(stock_list, "get_exchange", lambda _market: fake)
    monkeypatch.setattr(stock_list, "_save_stocks_to_disk", lambda *_args: None)

    stock_list._preload_single_exchange("a", skip_if_disk_warm=True)

    assert fake.all_stocks_calls == 1
    assert fake.stock_info_calls == []
    assert [
        (row["code"], row["name"], row["type"])
        for row in stock_list._cached_symbols_or_empty("a")
    ] == [("SH.600000", "Pufa", "stock_cn")]
    assert stock_list.get_symbol_readiness("a") == {
        "market": "a",
        "ready": True,
        "status": "ready",
        "count": 1,
        "last_error": None,
        "catalog_mode": stock_list.FULL_IDENTITY_CATALOG,
        "admitted_count": 1,
        "full_catalog_authorized": True,
    }


def test_recent_verified_full_catalog_disk_cache_skips_first_refresh(
    tmp_path, monkeypatch
):
    cache_file = tmp_path / "a.json"
    monkeypatch.setattr(
        stock_list, "_stocks_cache_file", lambda _market: str(cache_file)
    )
    monkeypatch.setattr(stock_list, "PRELOAD_EXCHANGES", ["a"])
    monkeypatch.setattr(stock_list.time, "time", lambda: 10_000)
    stock_list.configure_symbol_catalog(
        validation_codes=("SH.600000",),
        full_catalog_authorized=True,
    )
    stock_list._save_stocks_to_disk("a", list(RAW))
    with stock_list._stock_cache_lock:
        stock_list.stock_cache.clear()
        stock_list._disk_cache_metadata.clear()

    stock_list._warm_cache_from_disk()

    def fail_if_exchange_is_opened(_market):
        raise AssertionError("recent verified full cache must skip startup enumeration")

    monkeypatch.setattr(stock_list, "get_exchange", fail_if_exchange_is_opened)
    stock_list._preload_single_exchange("a", skip_if_disk_warm=True)

    assert [
        row["code"] for row in stock_list._cached_symbols_or_empty("a")
    ] == ["SH.600000"]
    assert stock_list.get_symbol_readiness("a")["status"] == "ready"


def test_stale_verified_full_catalog_disk_cache_refreshes(monkeypatch):
    stock_list.configure_symbol_catalog(
        validation_codes=("SH.600000",),
        full_catalog_authorized=True,
    )
    with stock_list._stock_cache_lock:
        stock_list.stock_cache["a"] = stock_list._process_stock_list(list(RAW))
        stock_list._disk_cache_metadata["a"] = {
            "verified": True,
            "catalog_mode": stock_list.FULL_IDENTITY_CATALOG,
            "updated_at": 1_000,
            "count": len(RAW),
            "scope_codes": (),
        }
    monkeypatch.setattr(
        stock_list.time,
        "time",
        lambda: 1_000 + stock_list.PRELOAD_INTERVAL_SECONDS + 1,
    )
    fake = _FakeExchange(list(RAW))
    monkeypatch.setattr(stock_list, "get_exchange", lambda _market: fake)
    monkeypatch.setattr(stock_list, "_save_stocks_to_disk", lambda *_args: None)

    stock_list._preload_single_exchange("a", skip_if_disk_warm=True)

    assert fake.all_stocks_calls == 1


def test_cold_validation_preload_never_enumerates_all_stocks(monkeypatch):
    codes = stock_list.DEFAULT_VALIDATION_SYMBOL_CODES
    fake = _FakeExchange(
        [
            {"code": code, "name": f"name-{index}", "type": "stock_cn"}
            for index, code in enumerate(codes)
        ]
    )
    stock_list.configure_symbol_catalog(
        validation_codes=codes,
        full_catalog_authorized=False,
    )
    monkeypatch.setattr(stock_list, "get_exchange", lambda _market: fake)
    monkeypatch.setattr(stock_list, "_load_stocks_from_disk", lambda _market: None)
    monkeypatch.setattr(stock_list, "_save_stocks_to_disk", lambda *_args: None)

    stock_list._warm_cache_from_disk()
    stock_list._preload_single_exchange("a")

    assert fake.all_stocks_calls == 0
    assert fake.stock_info_calls == list(codes)
    assert len(fake.stock_info_calls) == 12
    snapshot = stock_list.get_symbol_readiness("a")
    assert snapshot["catalog_mode"] == stock_list.BOUNDED_VALIDATION_CATALOG
    assert snapshot["admitted_count"] == 12
    assert snapshot["count"] == 12


def test_runtime_validation_symbols_match_preregistered_backtest_profile() -> None:
    profile = ROOT / "config/research_backtest_validation_12.txt"
    expected = tuple(
        line.strip()
        for line in profile.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    )
    joined = ",".join(expected)

    assert stock_list.DEFAULT_VALIDATION_SYMBOL_CODES == expected
    assert joined in (ROOT / "ops/restart_web.ps1").read_text(encoding="utf-8-sig")
    production_launcher = (ROOT / "windows_run.bat").read_text(encoding="utf-8-sig")
    assert "-EnableFullSymbolCatalog" in production_launcher
    assert joined not in production_launcher


def test_preload_unknown_adapter_uses_code_without_stock_info(monkeypatch):
    calls = {"stock_info": 0, "all_stocks": 0, "basicinfo": 0}

    class _CatalogExpandingExchange:
        def all_stocks(self):
            calls["all_stocks"] += 1
            return [{"code": "SH.600000", "name": "Pufa"}]

        def stock_info(self, code):
            calls["stock_info"] += 1
            calls["basicinfo"] += 1
            return next(
                row for row in self.all_stocks() if row["code"] == code
            )

    stock_list.configure_symbol_catalog(
        validation_codes=("SH.600000",),
        full_catalog_authorized=False,
    )
    monkeypatch.setattr(
        stock_list,
        "get_exchange",
        lambda _market: _CatalogExpandingExchange(),
    )
    monkeypatch.setattr(stock_list, "_save_stocks_to_disk", lambda *_args: None)

    stock_list._preload_single_exchange("a")

    cached = stock_list._cached_symbols_or_empty("a")
    assert [(row["code"], row["name"]) for row in cached] == [
        ("SH.600000", "SH.600000")
    ]
    assert calls == {"stock_info": 0, "all_stocks": 0, "basicinfo": 0}


def test_periodic_validation_refresh_stays_bounded(monkeypatch):
    codes = ("SH.600000", "SZ.000001")
    fake = _FakeExchange(
        [
            {"code": "SH.600000", "name": "Pufa", "type": "stock_cn"},
            {"code": "SZ.000001", "name": "Ping An", "type": "stock_cn"},
        ]
    )
    stock_list.configure_symbol_catalog(
        validation_codes=codes,
        full_catalog_authorized=False,
    )
    monkeypatch.setattr(stock_list, "get_exchange", lambda _market: fake)
    monkeypatch.setattr(stock_list, "_save_stocks_to_disk", lambda *_args: None)

    stock_list._preload_single_exchange("a")
    first_round = tuple(fake.stock_info_calls)
    fake.stock_info_calls.clear()
    stock_list._preload_single_exchange("a")

    assert fake.all_stocks_calls == 0
    assert first_round == codes
    assert tuple(fake.stock_info_calls) == codes
    assert len(fake.stock_info_calls) <= 12


def test_sync_fallback_stays_in_validation_catalog(monkeypatch):
    code = "SH.600000"
    fake = _FakeExchange(
        [{"code": code, "name": "Pufa", "type": "stock_cn"}]
    )
    stock_list.configure_symbol_catalog(
        validation_codes=(code,),
        full_catalog_authorized=False,
    )
    monkeypatch.setattr(stock_list, "get_exchange", lambda _market: fake)
    monkeypatch.setattr(stock_list, "_save_stocks_to_disk", lambda *_args: None)

    result = stock_list.get_cached_processed_stocks(
        "a", allow_sync_fallback=True
    )

    assert [row["code"] for row in result] == [code]
    assert fake.all_stocks_calls == 0
    assert fake.stock_info_calls == [code]


def test_disk_restore_projects_legacy_catalog_to_admitted_codes(monkeypatch):
    stock_list.configure_symbol_catalog(
        validation_codes=("SH.600000",),
        full_catalog_authorized=False,
    )
    monkeypatch.setattr(stock_list, "PRELOAD_EXCHANGES", ["a"])
    monkeypatch.setattr(
        stock_list,
        "_load_stocks_from_disk",
        lambda _market: [
            {"code": "SH.600000", "name": "Pufa", "type": "stock_cn"},
            {"code": "SZ.000001", "name": "out", "type": "stock_cn"},
        ],
    )
    persisted = []
    monkeypatch.setattr(
        stock_list,
        "_save_stocks_to_disk",
        lambda _market, rows: persisted.extend(rows),
    )

    stock_list._warm_cache_from_disk()

    assert [row["code"] for row in stock_list._cached_symbols_or_empty("a")] == [
        "SH.600000"
    ]
    assert [row["code"] for row in persisted] == ["SH.600000"]
    snapshot = stock_list.get_symbol_readiness("a")
    assert snapshot["catalog_mode"] == stock_list.BOUNDED_VALIDATION_CATALOG
    assert snapshot["admitted_count"] == 1


def test_full_catalog_enumeration_requires_independent_authorization(monkeypatch):
    fake = _FakeExchange(list(RAW))
    stock_list.configure_symbol_catalog(
        validation_codes=("SH.600000",),
        full_catalog_authorized=True,
    )
    monkeypatch.setattr(stock_list, "get_exchange", lambda _market: fake)
    monkeypatch.setattr(stock_list, "_save_stocks_to_disk", lambda *_args: None)

    stock_list._preload_single_exchange("a")

    assert fake.all_stocks_calls == 1
    assert fake.stock_info_calls == []
    snapshot = stock_list.get_symbol_readiness("a")
    assert snapshot["catalog_mode"] == stock_list.FULL_IDENTITY_CATALOG
    assert snapshot["full_catalog_authorized"] is True


def test_full_catalog_qmt_capability_receives_exact_authorization():
    calls = []

    class _AuthorizationAwareExchange:
        all_stocks_requires_explicit_authorization = True

        def all_stocks(self, *, full_market_authorized=False):
            calls.append(full_market_authorized)
            return list(RAW)

    stock_list.configure_symbol_catalog(
        validation_codes=("SH.600000",),
        full_catalog_authorized=True,
    )

    rows = stock_list._authorized_full_catalog_rows(
        _AuthorizationAwareExchange(),
        "a",
    )

    assert rows == RAW
    assert calls == [True]


def test_full_catalog_legacy_adapter_keeps_no_arg_contract():
    calls = []

    class _LegacyExchange:
        def all_stocks(self):
            calls.append(())
            return list(RAW)

    stock_list.configure_symbol_catalog(
        validation_codes=("SH.600000",),
        full_catalog_authorized=True,
    )

    rows = stock_list._authorized_full_catalog_rows(_LegacyExchange(), "a")

    assert rows == RAW
    assert calls == [()]


def test_full_catalog_primitive_fails_closed_without_authorization():
    fake = _FakeExchange(list(RAW))
    stock_list.configure_symbol_catalog(
        validation_codes=("SH.600000",),
        full_catalog_authorized=False,
    )

    with pytest.raises(PermissionError, match="independently authorized"):
        stock_list._authorized_full_catalog_rows(fake, "a")

    assert fake.all_stocks_calls == 0


def test_catalog_authorization_rejects_truthy_non_boolean_values():
    with pytest.raises(TypeError, match="exact bool"):
        stock_list.configure_symbol_catalog(
            validation_codes=("SH.600000",),
            full_catalog_authorized="0",
        )


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


def test_first_preload_round_skips_refresh_for_disk_warmed_symbols(monkeypatch):
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

    assert calls == [("a", True)]


def test_later_preload_rounds_refresh_disk_warmed_symbols(monkeypatch):
    calls = []
    sleep_calls = 0

    def record_refresh(exchange, skip_if_disk_warm=False):
        calls.append((exchange, skip_if_disk_warm))

    def stop_after_second_round(_seconds):
        nonlocal sleep_calls
        sleep_calls += 1
        if sleep_calls == 2:
            raise RuntimeError("stop after second refresh")

    monkeypatch.setattr(stock_list, "PRELOAD_EXCHANGES", ["a"])
    monkeypatch.setattr(stock_list, "PRELOAD_PARALLEL_WORKERS", 1)
    monkeypatch.setattr(stock_list, "PRELOAD_STARTUP_DELAY_SECONDS", 0)
    monkeypatch.setattr(stock_list, "_preload_single_exchange", record_refresh)
    monkeypatch.setattr(stock_list.time, "sleep", stop_after_second_round)

    with pytest.raises(RuntimeError, match="stop after second refresh"):
        stock_list.preload_symbols()

    assert calls == [("a", True), ("a", False)]


def test_disabled_preload_is_ready_without_symbol_cache(monkeypatch):
    monkeypatch.setattr(stock_list, "PRELOAD_EXCHANGES", [])

    snapshot = stock_list.get_symbol_readiness("a")

    assert snapshot == {
        "market": "a",
        "ready": True,
        "status": "disabled",
        "count": 0,
        "last_error": None,
        "catalog_mode": stock_list.BOUNDED_VALIDATION_CATALOG,
        "admitted_count": 2,
        "full_catalog_authorized": False,
    }


def test_cached_a_instrument_types_use_only_restored_memory_catalog(monkeypatch):
    with stock_list._stock_cache_lock:
        stock_list.stock_cache["a"] = [
            {"code": "SH.000001", "name": "上证指数", "type": "index_cn"},
            {"code": "SH.510300", "name": "沪深300ETF", "type": "etf_cn"},
            {"code": "SH.600000", "name": "浦发银行", "type": "stock_cn"},
        ]

    monkeypatch.setattr(
        stock_list,
        "get_exchange",
        lambda _market: (_ for _ in ()).throw(
            AssertionError("证券类型读取不得访问交易所")
        ),
    )
    monkeypatch.setattr(
        stock_list,
        "_load_stocks_from_disk",
        lambda _market: (_ for _ in ()).throw(
            AssertionError("证券类型读取不得访问磁盘")
        ),
    )

    assert stock_list.get_cached_a_instrument_types(
        ("SH.000001", "SH.510300", "SH.600000", "SZ.000001")
    ) == {
        "SH.000001": "index_cn",
        "SH.510300": "etf_cn",
        "SH.600000": "stock_cn",
        "SZ.000001": "unresolved_cn",
    }


def test_cached_a_instrument_types_fail_closed_on_conflict_or_unknown_type():
    with stock_list._stock_cache_lock:
        stock_list.stock_cache["a"] = [
            {"code": "SH.600000", "name": "浦发银行", "type": "stock_cn"},
            {"code": "SH.600000", "name": "冲突记录", "type": "index_cn"},
            {"code": "SZ.000001", "name": "平安银行", "type": "legacy_stock"},
        ]

    assert stock_list.get_cached_a_instrument_types(
        ("SH.600000", "SZ.000001")
    ) == {
        "SH.600000": "unresolved_cn",
        "SZ.000001": "unresolved_cn",
    }


def test_cached_a_symbol_names_use_only_restored_memory_catalog(monkeypatch):
    with stock_list._stock_cache_lock:
        stock_list.stock_cache["a"] = [
            {"code": "SH.513100", "name": " 纳指ETF ", "type": "etf_cn"},
            {"code": "SZ.000001", "name": "平安银行", "type": "stock_cn"},
        ]
    monkeypatch.setattr(
        stock_list,
        "_stocks_cache_file",
        lambda _market: (_ for _ in ()).throw(
            AssertionError("证券名称读取不得访问磁盘")
        ),
    )

    assert stock_list.get_cached_a_symbol_names(
        ("SH.513100", "SZ.000001", "SZ.300001")
    ) == {
        "SH.513100": "纳指ETF",
        "SZ.000001": "平安银行",
        "SZ.300001": None,
    }


def test_cached_a_symbol_names_fail_closed_on_conflicting_rows():
    with stock_list._stock_cache_lock:
        stock_list.stock_cache["a"] = [
            {"code": "SH.513100", "name": "纳指ETF", "type": "etf_cn"},
            {"code": "SH.513100", "name": "冲突名称", "type": "etf_cn"},
        ]

    assert stock_list.get_cached_a_symbol_names(("SH.513100",)) == {
        "SH.513100": None
    }


@pytest.mark.parametrize(
    ("codes", "error"),
    [
        (["SH.600000"], TypeError),
        (("600000",), TypeError),
        (("SH.600000", "SH.600000"), ValueError),
        (("SZ.000001", "SH.600000"), ValueError),
    ],
)
def test_cached_a_instrument_types_require_exact_sorted_identity(codes, error):
    with pytest.raises(error):
        stock_list.get_cached_a_instrument_types(codes)
