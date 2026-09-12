import time
from threading import Event

import pandas as pd
import pytest
from flask import Flask

from cl_app.blueprints import tv
from cl_app.services import chart_cache, chart_compute, chart_initial_build, kline_recompute


@pytest.fixture
def opened_chart(monkeypatch):
    chart_initial_build.start_initial_build_runtime()
    kline_recompute.reset_cl_pool()
    released = Event()
    entered = Event()
    calls = []
    writes = []
    frame = pd.DataFrame({
        "date": pd.date_range("2026-01-05 15:31", periods=3, freq="min", tz="UTC"),
        "open": [10., 11., 12.], "high": [11., 12., 13.], "low": [9., 10., 11.],
        "close": [10.5, 11.5, 12.5], "volume": [100., 120., 130.],
    })
    frame.attrs.update(structure_price_quantum="0.01", price_basis_revision="initial-test")

    class Exchange:
        kline_time_label = "end"

        def klines(self, *_args, **_kwargs):
            calls.append("fetch")
            return frame.copy(deep=True)

    def serialize(**kwargs):
        calls.append("serialize")
        entered.set()
        assert released.wait(3), "test did not release the structure calculation"
        assert kwargs["display_klines"].equals(frame)
        return {"t": [int(v.timestamp()) for v in frame.date],
                "c": frame.close.tolist(), "bar_time_label": "end",
                "strict_structure_mode": "replace", "strict_structure": {
                    "schema": "chanlun-chart-structure",
                    "source_closed_at": int(frame.date.iloc[-1].timestamp()),
                }}

    monkeypatch.setattr(chart_compute, "get_exchange", lambda _market: Exchange())
    monkeypatch.setattr(chart_compute, "serialize_chart_data_with_strict_runtime", serialize)
    monkeypatch.setattr(chart_cache, "_persist_chart_cache_async", lambda *args: writes.append(args))
    monkeypatch.setattr(tv, "_build_cache_key", lambda *_args: "test-initial")
    monkeypatch.setattr(tv, "query_cl_chart_config", lambda *_args: {})
    app = Flask(__name__)
    app.config["LOGIN_DISABLED"] = True
    app.register_blueprint(tv.tv_bp)
    yield frame, released, entered, calls, writes, app.test_client()
    released.set()
    deadline = time.monotonic() + 3
    while chart_initial_build.is_initial_build_active("test-initial") and time.monotonic() < deadline:
        time.sleep(.01)
    chart_initial_build.shutdown_initial_build_runtime()
    kline_recompute.reset_cl_pool()
    with chart_cache.cache_lock:
        chart_cache.chart_data_cache.pop("test-initial", None)


def publish():
    # Mirror the production route's per-chart calculation critical section.
    with chart_compute.chart_calc_locks.get("test-initial"):
        result = chart_compute.fetch_klines_and_compute_cl_data(
            "us", "AAPL.US", "1m", {}, {}, False, "cache_empty", "test-initial", 0,
            progressive=True,
        )
        return chart_cache._set_chart_cache_entry(
            "test-initial", result["cl_chart_data"], is_full_snapshot=True,
        )


def wait_complete():
    deadline = time.monotonic() + 3
    while chart_initial_build.is_initial_build_active("test-initial") and time.monotonic() < deadline:
        time.sleep(.01)
    assert not chart_initial_build.is_initial_build_active("test-initial")


def test_candles_arrive_before_blocked_structure_and_pending_reads_do_not_rebuild(opened_chart):
    frame, released, entered, calls, writes, client = opened_chart
    entry = publish()
    assert entered.wait(1)
    assert entry["data"]["c"] == frame.close.tolist()
    assert entry["data"]["strict_structure_error"]["code"] == chart_initial_build.PENDING_CODE
    assert entry["data"]["xds"] == []
    assert writes == []  # Pending evidence cannot survive a process restart.
    old_entry = {**entry, "validated_at": time.time() - 600}
    for _ in range(3):
        hit, data, _reason, refresh = chart_cache.evaluate_cache_for_tv_history(
            old_entry, 0, 0, False,
        )
        assert hit and data is entry["data"] and not refresh
        response = client.get("/tv/structure-status?symbol=us:AAPL.US&resolution=1")
        assert response.get_json() == {"state": "pending"}
    assert calls == ["fetch", "serialize"]
    released.set()
    wait_complete()
    final = chart_cache._get_chart_cache_entry_ram_only("test-initial")["data"]
    assert final["strict_structure_mode"] == "replace"
    assert final["c"] == frame.close.tolist()
    assert len(writes) == 1
    assert client.get("/tv/structure-status?symbol=us:AAPL.US&resolution=1").get_json()["state"] == "complete"


def test_late_worker_cannot_overwrite_a_newer_snapshot(opened_chart):
    _frame, _released, _entered, calls, _writes, _client = opened_chart
    replacement = {"t": [1], "c": [99.]}
    with chart_compute.chart_calc_locks.get("test-initial"):
        publish()
        chart_cache._set_chart_cache_entry("test-initial", replacement, is_full_snapshot=True)
    wait_complete()
    assert chart_cache._get_chart_cache_entry_ram_only("test-initial")["data"] == replacement
    assert calls == ["fetch"]


def test_structure_failure_preserves_candles_and_ends_pending_state(opened_chart, monkeypatch):
    frame, *_rest = opened_chart

    def fail(**_kwargs):
        raise ValueError("invalid evidence")

    monkeypatch.setattr(chart_compute, "serialize_chart_data_with_strict_runtime", fail)
    publish()
    wait_complete()
    data = chart_cache._get_chart_cache_entry_ram_only("test-initial")["data"]
    assert data["c"] == frame.close.tolist()
    assert data["strict_structure_error"]["code"] == "strict_initial_build_failed"
    assert "_initial_structure_build_id" not in data


def test_reading_missing_status_does_not_fetch_or_build(opened_chart):
    _frame, _released, _entered, calls, _writes, client = opened_chart
    assert client.get("/tv/structure-status?symbol=us:AAPL.US&resolution=1").get_json() == {"state": "missing"}
    assert client.get("/tv/structure-status?symbol=broken&resolution=1").status_code == 400
    assert calls == []


def test_completed_build_patch_is_bound_to_the_preview_and_omits_candles(opened_chart):
    frame, released, entered, calls, _writes, client = opened_chart
    preview = publish()["data"]
    assert entered.wait(1)
    build_id = preview["_initial_structure_build_id"]
    released.set()
    wait_complete()
    packet = client.get(
        f"/tv/structure-status?symbol=us:AAPL.US&resolution=1&build_id={build_id}"
    ).get_json()
    assert packet["state"] == "complete"
    patch = packet["patch"]
    assert patch["build_id"] == build_id
    assert patch["source_count"] == len(frame)
    assert patch["source_last"] == int(frame.date.iloc[-1].timestamp())
    assert not ({"t", "o", "h", "l", "c", "v"} & patch.keys())
    assert client.get(
        "/tv/structure-status?symbol=us:AAPL.US&resolution=1&build_id=old"
    ).get_json() == {"state": "superseded"}
    # The initial worker handed the real CL to the existing incremental pool.
    assert kline_recompute._cl_pool["test-initial"]["n"] == len(frame)
    assert calls == ["fetch", "serialize"]


def test_abandoned_pending_snapshot_is_retried(opened_chart):
    data = {"t": [1], "_initial_structure_build_id": "abandoned",
            "_initial_structure_cache_key": "not-running"}
    entry = chart_cache._build_chart_cache_entry(data, is_full_snapshot=True)
    assert chart_cache.evaluate_cache_for_tv_history(entry, 0, 0, False) == (
        False, None, "cache_pending_abandoned", False,
    )


def test_automatic_repair_shares_recent_result_but_manual_reload_and_stale_sources_refresh():
    data = {"t": [1], "strict_structure_mode": "replace"}
    entry = chart_cache._build_chart_cache_entry(data, is_full_snapshot=True)
    assert chart_cache.evaluate_cache_for_tv_history(entry, 0, 0, False, refresh_if_stale=True) == (
        True, data, None, False,
    )
    assert chart_cache.evaluate_cache_for_tv_history(entry, 0, 0, False, force_refresh=True)[0] is False
    stale = {**entry, "validated_at": time.time() - 31}
    assert chart_cache.evaluate_cache_for_tv_history(stale, 0, 0, False, refresh_if_stale=True) == (
        False, None, "cache_force_refresh", False,
    )
