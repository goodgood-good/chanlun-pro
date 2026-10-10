"""Screening continuation must remain identical to independent cold evidence."""

from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from pathlib import Path
import threading
import time

import pandas as pd
import pytest

from chanlun.screening.cache import calculation_key, input_fingerprint, read_append_proofs, REPLAY_CHECKS, write_calculation
from chanlun.screening import runner, runtime_cache


FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture(autouse=True)
def isolated_runtime():
    runtime_cache.clear_runtime_cache()
    yield
    runtime_cache.clear_runtime_cache()


def recorded(relative, market="a"):
    frame = pd.read_parquet(FIXTURES / relative)
    frame.attrs.setdefault("structure_price_quantum", "0.01")
    frame.attrs.setdefault("price_basis_revision", "recorded-raw")
    frame.attrs.update(screening_market=market, bar_time_label="end")
    return frame


def calculate(frame, code, frequency, namespace, revision="test", **kwargs):
    with runtime_cache.incremental_snapshot_scope(namespace, revision) as metrics:
        snapshot = runner.snapshot_for(frame, code, frequency, **kwargs)
    return snapshot, metrics


@pytest.mark.parametrize("relative,code,frequency,market", [
    ("SH.600519_5m.parquet", "SH.600519", "5m", "a"),
    ("native_30m/SH.600183_30m.parquet", "SH.600183", "30m", "a"),
    ("segment_normalized/BABA_US_5m.parquet", "BABA.US", "5m", "us"),
    ("QQQ.US_30m.parquet", "QQQ.US", "30m", "us"),
])
def test_recorded_append_matches_all_cold_snapshot_fields(tmp_path, relative, code, frequency, market):
    frame = recorded(relative, market)
    for count in (len(frame) - 12, len(frame) - 11, len(frame) - 5, len(frame)):
        prefix = frame.iloc[:count].copy()
        actual, metrics = calculate(prefix, code, frequency, tmp_path, include_consolidation=True)
        # This compares timestamps, identities, FX/BI/XD geometry, all centers,
        # points, observations, consolidation evidence and both MACD layers.
        expected = runner.snapshot_for(prefix, code, frequency, include_consolidation=True)
        assert actual == expected
        assert metrics["mode"] == ("cold" if count == len(frame) - 12 else "append")
    unchanged, metrics = calculate(frame, code, frequency, tmp_path, include_consolidation=True)
    assert unchanged == expected and metrics["mode"] == "unchanged"


def progress(frame, tick):
    frame.attrs.update(canonical_source_progress=f"progress-{tick}", canonical_minute_cutoff=tick,
                       source_minute_count=tick * 30)
    frame.attrs["canonical_history_coverage"] = {
        "source_time_contract": "native-minute-v1", "requested_to": tick,
        "available_rows": len(frame), "progress_token": f"progress-{tick}",
        "raw_minute_rows": tick * 30, "complete": True,
        "diagnostics": {"missing_minute_count": 0, "expected_cutoff": tick},
    }


def test_progress_does_not_invalidate_same_input_or_verified_append_proofs(tmp_path):
    frame = recorded("QQQ.US_30m.parquet", "us")
    progress(frame, 1)
    old = frame.iloc[:-1].copy()
    original_attrs = deepcopy(frame.attrs)
    snapshot, _ = calculate(old, "QQQ.US", "30m", tmp_path)
    key = calculation_key(old, "QQQ.US", "30m", "test")
    path = tmp_path / "QQQ.US_30m.json.gz"
    proofs = {"formal": dict.fromkeys(REPLAY_CHECKS, True)}
    write_calculation(path, key, {"reason_counts": {}}, snapshot, proofs,
                      source_frame=old, calculation_revision="test")
    source_bytes = {p: p.read_bytes() for p in (tmp_path / "inputs").iterdir()}
    progress(frame, 2)
    same = frame.iloc[:-1].copy()
    assert calculation_key(same, "QQQ.US", "30m", "test") == key
    assert read_append_proofs(path, frame, "QQQ.US", "30m", "test") == proofs
    _, metrics = calculate(frame, "QQQ.US", "30m", tmp_path)
    assert metrics["mode"] == "append"
    assert old.attrs == original_attrs
    assert all(p.read_bytes() == value for p, value in source_bytes.items())


@pytest.mark.parametrize("change", ["volume", "history", "rolling", "basis", "quantum", "time", "contract", "unknown", "revision", "namespace"])
def test_corrections_windows_contracts_and_revision_require_cold_rebuild(tmp_path, change):
    frame = recorded("QQQ.US_30m.parquet", "us")
    old = frame.iloc[:-1].copy()
    calculate(old, "QQQ.US", "30m", tmp_path)
    revision, namespace = "test", tmp_path
    if change == "volume":
        frame.loc[0, "volume"] += 1
    elif change == "history":
        frame.loc[0, "high"] += .1
    elif change == "rolling":
        frame = frame.iloc[1:].copy()
    elif change == "basis":
        frame.attrs["price_basis_revision"] += "-revised"
    elif change == "quantum":
        frame.attrs["structure_price_quantum"] = "0.001"
    elif change == "time":
        frame.attrs["bar_time_label"] = "start"
    elif change == "contract":
        frame.attrs["source_time_contract"] = "another-contract"
    elif change == "unknown":
        frame.attrs["new_semantic_contract"] = {"version": 1}
    elif change == "revision":
        revision = "new-code"
    elif change == "namespace":
        namespace = tmp_path / "another"
    actual, metrics = calculate(frame, "QQQ.US", "30m", namespace, revision)
    assert metrics["mode"] == "cold"
    assert actual == runner.snapshot_for(frame, "QQQ.US", "30m")


def test_nested_unknown_coverage_and_time_contracts_remain_fingerprinted():
    frame = recorded("QQQ.US_30m.parquet", "us")
    progress(frame, 1)
    original = input_fingerprint(frame, "QQQ.US", "30m")
    for key in ("source_time_contract", "unknown_new_contract"):
        changed = frame.copy()
        changed.attrs = deepcopy(frame.attrs)
        changed.attrs["canonical_history_coverage"][key] = "changed"
        assert input_fingerprint(changed, "QQQ.US", "30m") != original


def test_scan_revalidates_eligibility_but_does_not_recompute_unchanged_closed_bars(tmp_path, monkeypatch):
    frame = recorded("QQQ.US_30m.parquet", "us")
    progress(frame, 1)
    monkeypatch.setattr(runner, "fetch_frame", lambda *_: frame)
    validations = []
    monkeypatch.setattr(runner, "frame_quality", lambda *_: {"errors": []})
    def audit(*args):
        validations.append(True)
        return {"data_errors": [], "selected": [], "rejected": [], "observations": [],
                "confirmed_buys": 0, "unconfirmed_buys": 0, "confirmed_sells": 0, "unconfirmed_sells": 0}
    monkeypatch.setattr(runner, "audit_native_center_signals", audit)
    def scan():
        return runner.scan_symbol({"code": "QQQ.US", "market": "us", "name": "fixture"},
            {"frequencies": ["30m"], "point_types": []}, {"30m": {}}, tmp_path / "run",
            cache_root=tmp_path / "cache", revision="test")["rows"][0]
    first = scan()
    assert not first["data_errors"] and first["structure_calculation"]["mode"] == "cold"
    def forbidden(*args, **kwargs):
        pytest.fail("unchanged closed input must use saved evidence")
    monkeypatch.setattr(runner, "snapshot_for", forbidden)
    progress(frame, 2)
    second = scan()
    assert not second["data_errors"] and second["structure_calculation"]["mode"] == "snapshot"
    assert second["input_fingerprint"] == first["input_fingerprint"] and len(validations) == 2


def test_replays_force_rebuild_and_other_processes_do_not_continue_state(tmp_path, monkeypatch):
    frame = recorded("QQQ.US_30m.parquet", "us")
    calculate(frame.iloc[:-1].copy(), "QQQ.US", "30m", tmp_path)
    from chanlun.cl_utils import strict_chart_runtime
    original = strict_chart_runtime.build_strict_chart_cd
    builds = []
    def build(**kwargs):
        builds.append(len(kwargs["frame"]))
        return original(**kwargs)
    monkeypatch.setattr(strict_chart_runtime, "build_strict_chart_cd", build)
    runner.snapshot_for(frame, "QQQ.US", "30m")
    with runtime_cache.incremental_snapshot_scope(tmp_path, "test", enabled=False):
        runner.snapshot_for(frame, "QQQ.US", "30m")
    monkeypatch.setattr(runtime_cache, "_pid", -1)
    _, metrics = calculate(frame, "QQQ.US", "30m", tmp_path)
    assert builds == [len(frame)] * 3 and metrics["mode"] == "cold"


def test_same_runtime_processing_and_rendering_are_serialized(tmp_path, monkeypatch):
    frame = recorded("QQQ.US_30m.parquet", "us")
    original = runner._snapshot_from_runtime
    active = 0
    guard = threading.Lock()
    def render(*args, **kwargs):
        nonlocal active
        with guard:
            active += 1
            assert active == 1
        try:
            time.sleep(.02)
            return original(*args, **kwargs)
        finally:
            with guard:
                active -= 1
    monkeypatch.setattr(runner, "_snapshot_from_runtime", render)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: calculate(frame, "QQQ.US", "30m", tmp_path), range(2)))
    assert results[0][0] == results[1][0]
    assert sorted(item[1]["mode"] for item in results) == ["cold", "unchanged"]


def test_failed_render_is_discarded_and_lru_source_bar_budget_is_enforced(tmp_path, monkeypatch):
    frame = recorded("QQQ.US_30m.parquet", "us")
    original = runner._snapshot_from_runtime
    def broken(*args, **kwargs):
        raise ValueError("render failed")
    monkeypatch.setattr(runner, "_snapshot_from_runtime", broken)
    with pytest.raises(ValueError, match="render failed"):
        calculate(frame, "QQQ.US", "30m", tmp_path)
    assert not runtime_cache._entries
    monkeypatch.setattr(runner, "_snapshot_from_runtime", original)
    monkeypatch.setattr(runtime_cache, "MAX_SOURCE_BARS", len(frame))
    calculate(frame, "QQQ.US", "30m", tmp_path)
    calculate(frame, "QQQ.US", "30m", tmp_path, revision="next")
    assert len(runtime_cache._entries) == 1
    _, metrics = calculate(frame, "QQQ.US", "30m", tmp_path)
    assert metrics["mode"] == "cold"


@pytest.mark.parametrize('frequency', ['1m', '5m', '30m'])
def test_held_period_priority_survives_other_symbols_but_keeps_hard_budget(tmp_path, monkeypatch, frequency):
    frame = recorded("QQQ.US_30m.parquet", "us")
    monkeypatch.setattr(runtime_cache, "MAX_ENTRIES", 2)
    with runtime_cache.incremental_snapshot_scope(tmp_path, "held", priority=True):
        runner.snapshot_for(frame, "QQQ.US", frequency)
    calculate(frame, "QQQ.US", frequency, tmp_path, revision="ordinary-1")
    calculate(frame, "QQQ.US", frequency, tmp_path, revision="ordinary-2")
    assert len(runtime_cache._entries) == 2
    with runtime_cache.incremental_snapshot_scope(tmp_path, "held", priority=True) as metrics:
        runner.snapshot_for(frame, "QQQ.US", frequency)
    assert metrics["mode"] == "unchanged"
    monkeypatch.setattr(runtime_cache, "MAX_ENTRIES", 1)
    with runtime_cache.incremental_snapshot_scope(tmp_path, "new-held", priority=True):
        runner.snapshot_for(frame, "QQQ.US", frequency)
    assert len(runtime_cache._entries) == 1
