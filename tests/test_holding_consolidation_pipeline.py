"""Recorded holdings comparisons reach frozen proof without changing point classes."""

import json
from pathlib import Path
from unittest.mock import Mock

import pandas as pd
import pytest

from chanlun.screening import runner
from chanlun.screening.rules import POINT_TYPES, trading_context
from cl_app.services.screening import ScreeningManager


@pytest.fixture
def holding_run(tmp_path, monkeypatch):
    frame = pd.read_parquet(Path("tests/fixtures/native_30m/SH.600183_30m.parquet"))
    frame.attrs.update(structure_price_quantum="0.01", price_basis_revision="recorded-raw")
    context = trading_context(frame.date.iloc[-1].to_pydatetime(), "30m", 20, 60)
    monkeypatch.setattr(runner, "fetch_frame", lambda *_args: frame.copy())
    directory = tmp_path / ("e" * 32)
    settings = {"strategy": "direct_structural", "frequencies": ["30m"], "point_types": list(POINT_TYPES),
                "max_anchor_gain_pct": 100}
    stock = {"market": "a", "code": "SH.600183", "name": "fixture", "holding": True}
    result = runner.scan_symbol(stock, settings, {"30m": context}, directory, cache_root=tmp_path / "cache", revision="test")
    row, = result["rows"]
    assert not row["data_errors"], row
    assert row["consolidation_alerts"]
    (directory / "results.jsonl").write_text(json.dumps(result) + "\n", encoding="utf-8")
    return ScreeningManager(tmp_path), directory, result, frame, stock, settings, context


def test_holding_comparison_and_existing_buy_sell_points_both_pass_saved_reader(holding_run):
    manager, directory, record, *_ = holding_run
    result = manager._read_results(directory, {"source_current": True, "completed": 1, "settings": {"strategy": "direct_structural"}})
    assert result["selected"] == record["rows"][0]["selected"]
    assert result["consolidation_alerts"]
    assert not result["observations"]
    assert all(candidate["audit"] == {"cold_rebuild": True, "confirmation_replay": True, "confirmation_boundary": True}
               for candidate in result["consolidation_alerts"])
    historical = manager._read_results(directory, {"source_current": False, "frozen_native_review": True,
                                                   "completed": 1, "settings": {"strategy": "direct_structural"}})
    assert historical["consolidation_alerts"] == []


def test_partial_consolidation_proof_failure_keeps_verified_buy_sell_points(holding_run):
    manager, directory, record, *_ = holding_run
    candidate = record["rows"][0]["consolidation_alerts"][0]
    record["rows"][0]["consolidation_alerts"].append({**candidate, "alert": {**candidate["alert"], "alert_id": "forged"}})
    (directory / "results.jsonl").write_text(json.dumps(record) + "\n", encoding="utf-8")
    result = manager._read_results(directory, {"source_current": True, "completed": 1, "settings": {"strategy": "direct_structural"}})
    assert result["selected"] == record["rows"][0]["selected"] and result["consolidation_alerts"] == []
    assert result["errors"][0]["signal_type"] == "consolidation_divergence"


def test_verified_holding_comparison_reuses_cached_replay(holding_run, monkeypatch):
    manager, directory, record, frame, stock, settings, context = holding_run
    review = Mock(side_effect=AssertionError("unchanged holding input must reuse replay"))
    monkeypatch.setattr(runner, "_review_consolidation_alerts", review)
    result = runner.scan_symbol(stock, settings, {"30m": context}, directory, cache_root=manager.root / "cache", revision="test")
    row, = result["rows"]
    assert row["calculation_reused"] and row["consolidation_alerts"]
    assert not row.get("consolidation_error")
    review.assert_not_called()


def test_consolidation_error_does_not_discard_normal_point_results(holding_run, monkeypatch):
    manager, directory, record, frame, stock, settings, context = holding_run
    monkeypatch.setattr(runner, "_select_consolidation_alerts", Mock(side_effect=ValueError("bad extra comparison")))
    snapshot = runner.snapshot_for(frame, stock["code"], "30m")
    cutoff = snapshot["levels"][0]["points"][0]["available_at"]
    prefix = frame.loc[frame.date <= pd.Timestamp(cutoff, unit="s", tz="UTC")].copy()
    monkeypatch.setattr(runner, "fetch_frame", lambda *_args: prefix.copy())
    context = trading_context(prefix.date.iloc[-1].to_pydatetime(), "30m", 20, 60)
    result = runner.scan_symbol(stock, settings, {"30m": context}, directory, revision="test")
    row, = result["rows"]
    assert row["selected"] and not row["data_errors"]
    assert row["consolidation_alerts"] == [] and row["consolidation_error"]


def test_non_holding_scan_does_not_run_additional_comparisons(holding_run, monkeypatch):
    manager, directory, record, frame, stock, settings, context = holding_run
    extra = Mock(side_effect=AssertionError("non-holdings must not calculate extra alerts"))
    monkeypatch.setattr(runner, "_select_consolidation_alerts", extra)
    snapshot = runner.snapshot_for(frame, stock["code"], "30m")
    cutoff = snapshot["levels"][0]["points"][0]["available_at"]
    prefix = frame.loc[frame.date <= pd.Timestamp(cutoff, unit="s", tz="UTC")].copy()
    monkeypatch.setattr(runner, "fetch_frame", lambda *_args: prefix.copy())
    context = trading_context(prefix.date.iloc[-1].to_pydatetime(), "30m", 20, 60)
    row, = runner.scan_symbol({key: value for key, value in stock.items() if key != "holding"},
                              settings, {"30m": context}, directory, revision="test")["rows"]
    assert row["selected"] and not row["data_errors"]
    assert not row.get("consolidation_alerts")
    extra.assert_not_called()


def test_catalog_holding_failure_is_explicit_and_does_not_discard_normal_scan(tmp_path,monkeypatch):
    monkeypatch.setattr('chanlun.screening.markets.holding_symbols',Mock(side_effect=OSError('unavailable')))
    connection=Mock()
    runner._catalog_worker(connection,{'scope':'symbols','exclude_st':False,'symbols':[
        {'market':'a','code':'SH.600183','name':'held'}]},tmp_path)
    result=connection.send.call_args.args[0]
    assert not result.get('error') and result['stocks'][0]['holding_lookup_failed'] is True


def test_preview_computation_failure_preserves_the_normal_buy_sell_snapshot(holding_run, monkeypatch):
    _, _, _, frame, stock, _, _ = holding_run
    ordinary = runner.snapshot_for(frame, stock["code"], "30m")
    monkeypatch.setattr("chanlun.core.strict_structure.native_consolidation_preview.build_native_consolidation_previews",
                        Mock(side_effect=ValueError("broken preview-only evidence")))
    extra = runner.snapshot_for(frame, stock["code"], "30m", include_consolidation=True)
    assert extra["levels"] == ordinary["levels"]
    assert extra["native_consolidation_error"] == "Preview evidence unavailable: ValueError"
