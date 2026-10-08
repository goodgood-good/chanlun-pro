"""Saved native-center signals require matching frozen proof."""

import json
from pathlib import Path

import pandas as pd
import pytest

from chanlun.screening.runner import _review_native_center_signals, _save_evidence, snapshot_for
from cl_app.services.screening import ScreeningManager
from cl_app.services import screening


def test_frozen_native_center_signal_is_visible_as_a_confirmed_selection(tmp_path):
    run_dir = tmp_path / ("a" * 32)
    run_dir.mkdir()
    frame = pd.read_parquet(Path("tests/fixtures/native_30m/SH.600183_30m.parquet"))
    frame.attrs.update(structure_price_quantum="0.01", price_basis_revision="recorded-raw")
    snapshot = snapshot_for(frame, "SH.600183", "30m")
    point, = snapshot["levels"][0]["points"]
    frame = frame.loc[frame.date <= pd.Timestamp(point["available_at"], unit="s", tz="UTC")].copy()
    snapshot = snapshot_for(frame, "SH.600183", "30m")
    point, = snapshot["levels"][0]["points"]
    center = next(item for item in snapshot["levels"][0]["centers"]
                  if item["center_id"] == point["center_id"])
    manifest = _save_evidence(run_dir, "SH.600183", "30m", frame, snapshot)
    candidate = {
        "point": point, "center": center,
        "reasons": [],
        "audit": {"cold_rebuild": True, "confirmation_replay": True,
                  "confirmation_boundary": True},
    }
    row = {
        "code": "SH.600183", "market": "a", "name": "fixture",
        "frequency": "30m", "source_closed_at": snapshot["source_closed_at"],
        "selected": [candidate], "observations": [], "recent_rejections": [],
        "reason_counts": {}, "evidence": manifest,
    }
    record = {"code": "SH.600183", "market": "a", "rows": [row]}
    (run_dir / "results.jsonl").write_text(json.dumps(record) + "\n", encoding="utf-8")
    result = ScreeningManager(tmp_path)._read_results(
        run_dir, {"completed": 1, "settings": {"strategy": "direct_structural"}},
    )
    assert result["observations"] == []
    assert len(result["selected"]) == 1
    assert result["selected"][0]["point"] == point
    assert result["selected"][0]["evidence_verified"] is True
    plan = result["selected"][0]["exit_plan"]
    assert plan["evidence_scope"] == "native_center_signal"
    assert plan["stop_loss"]["price"] == point["invalidation_price"]
    assert plan["stop_loss"]["trigger"] == "lt"
    assert plan["take_profit"]["status"] == "waiting"
    assert plan["take_profit"]["price"] is None

    cutoff = row["source_closed_at"]
    for wrong_cutoff in (cutoff + 60, cutoff - 60, None):
        row["source_closed_at"] = wrong_cutoff
        (run_dir / "results.jsonl").write_text(json.dumps(record) + "\n", encoding="utf-8")
        mismatched_clock = ScreeningManager(tmp_path)._read_results(
            run_dir, {"completed": 1, "settings": {"strategy": "direct_structural"}},
        )
        assert not mismatched_clock["selected"]
        assert mismatched_clock["errors"][0]["reasons"] == ["EVIDENCE_MISSING"]
    row["source_closed_at"] = cutoff

    row["recent_rejections"] = [{"point": {**point, "point_id": "closed-historical-point"},
                                 "reasons": ["LATER_SELL"]}]
    row["reason_counts"] = {"LATER_SELL": 1}
    (run_dir / "results.jsonl").write_text(json.dumps(record) + "\n", encoding="utf-8")
    with_diagnostics = ScreeningManager(tmp_path)._read_results(
        run_dir, {"completed": 1, "settings": {"strategy": "direct_structural"}},
    )
    assert with_diagnostics["rejection_counts"]["LATER_SELL"] == 1
    assert with_diagnostics["recent_rejections"][0]["reasons"] == ["LATER_SELL"]
    assert len(with_diagnostics["selected"]) == 1
    assert not with_diagnostics["observations"]

    row["selected"].append({**candidate, "point": {**point, "point_id": "forged-after-valid"}})
    (run_dir / "results.jsonl").write_text(json.dumps(record) + "\n", encoding="utf-8")
    partial = ScreeningManager(tmp_path)._read_results(
        run_dir, {"completed": 1, "settings": {"strategy": "direct_structural"}},
    )
    assert not partial["selected"]
    assert partial["errors"][0]["reasons"] == ["EVIDENCE_MISSING"]
    row["selected"].pop()

    record["rows"][0]["selected"][0]["point"] = {**point, "point_id": "forged"}
    (run_dir / "results.jsonl").write_text(json.dumps(record) + "\n", encoding="utf-8")
    damaged = ScreeningManager(tmp_path)._read_results(
        run_dir, {"completed": 1, "settings": {"strategy": "direct_structural"}},
    )
    assert damaged["selected"] == []
    assert damaged["errors"][0]["reasons"] == ["EVIDENCE_MISSING"]

    # A matching file checksum and stored replay flags cannot substitute for
    # source evidence that was actually known when the point was confirmed.
    record["rows"][0]["selected"][0]["point"] = point
    leave_id = point["related_unit_ids"][0]
    source = next(unit for unit in snapshot["screening_segments"] if unit["unit_id"] == leave_id)
    source["available_at"] = point["available_at"] + 1
    record["rows"][0]["evidence"] = _save_evidence(run_dir, "SH.600183", "30m", frame, snapshot)
    (run_dir / "results.jsonl").write_text(json.dumps(record) + "\n", encoding="utf-8")
    future_source = ScreeningManager(tmp_path)._read_results(
        run_dir, {"completed": 1, "settings": {"strategy": "direct_structural"}},
    )
    assert not future_source["selected"]
    assert future_source["errors"][0]["reasons"] == ["EVIDENCE_MISSING"]


@pytest.mark.parametrize("revision", ["old-recursive", None])
def test_stale_recursive_results_are_not_republished_or_parsed(tmp_path, revision):
    run_dir = tmp_path / ("b" * 32)
    run_dir.mkdir()
    (run_dir / "results.jsonl").write_text("old recursive results", encoding="utf-8")
    result = ScreeningManager(tmp_path)._read_results(
        run_dir, {"source_current": False, "source_revision": revision,
                  "status": "completed", "completed": 1},
    )
    assert result["selected"] == result["observations"] == []
    assert result["results_through"] == 0


def test_verified_result_cache_cannot_republish_a_run_after_its_algorithm_expires(
    tmp_path, monkeypatch,
):
    manager = ScreeningManager(tmp_path)
    run_dir = tmp_path / ("c" * 32)
    run_dir.mkdir()
    state = {"run_id": run_dir.name, "status": "completed", "completed": 1,
             "source_revision": "saved-algorithm", "source_current": True}
    monkeypatch.setattr(manager, "_directory", lambda: run_dir)
    monkeypatch.setattr(manager, "_status", lambda _: dict(state))
    read = manager._read_results

    def results(directory, status):
        if status["source_current"]:
            return {**status, "selected": [{"point": {"point_id": "previously-verified"}}]}
        return read(directory, status)

    monkeypatch.setattr(manager, "_read_results", results)
    assert manager.results(persist=True)["selected"]
    state["source_current"] = False
    expired = manager.results()
    assert not expired["selected"]
    assert expired["source_current"] is False


def test_recorded_six_signal_classes_pass_cold_and_first_available_replay():
    frame = pd.read_parquet(Path("tests/fixtures/SZ.002299_1m.parquet"))
    frame.attrs.update(structure_price_quantum="0.01", price_basis_revision="recorded-raw")
    snapshot = snapshot_for(frame, "SZ.002299", "1m")
    centers = {item["center_id"]: item for item in snapshot["levels"][0]["centers"]}
    candidates = []
    for kind in ("1buy", "2buy", "3buy", "1sell", "2sell", "3sell"):
        point = next(item for item in reversed(snapshot["levels"][0]["points"])
                     if item["point_type"] == kind)
        candidates.append({"point": point, "center": centers[point["center_id"]], "reasons": []})
    selected, rejected = _review_native_center_signals(
        snapshot, frame, candidates, "SZ.002299", "1m",
    )
    assert not rejected, rejected
    assert len(selected) == 6
    for item in selected:
        assert item["audit"] == {
            "cold_rebuild": True, "confirmation_replay": True,
            "confirmation_boundary": True,
        }


@pytest.fixture
def frozen_native_run(tmp_path):
    directory = tmp_path / ("d" * 32)
    frame = pd.read_parquet(Path("tests/fixtures/native_30m/SH.600183_30m.parquet"))
    frame.attrs.update(structure_price_quantum="0.01", price_basis_revision="recorded-raw")
    snapshot = snapshot_for(frame, "SH.600183", "30m")
    point, = snapshot["levels"][0]["points"]
    frame = frame.loc[frame.date <= pd.Timestamp(point["available_at"], unit="s", tz="UTC")].copy()
    snapshot = snapshot_for(frame, "SH.600183", "30m")
    point, = snapshot["levels"][0]["points"]
    center = next(item for item in snapshot["levels"][0]["centers"] if item["center_id"] == point["center_id"])
    manifest = _save_evidence(directory, "SH.600183", "30m", frame, snapshot)
    candidate = {"point": point, "center": center, "reasons": [],
                 "audit": {"cold_rebuild": True, "confirmation_replay": True, "confirmation_boundary": True}}
    record = {"market": "a", "code": "SH.600183", "rows": [{
        "market": "a", "code": "SH.600183", "frequency": "30m",
        "source_closed_at": snapshot["source_closed_at"], "selected": [candidate],
        "observations": [], "recent_rejections": [], "reason_counts": {}, "evidence": manifest,
    }]}
    (directory / "results.jsonl").write_text(json.dumps(record) + "\n", encoding="utf-8")
    screening.write_json(tmp_path / "latest.json", {"run_id": directory.name})
    screening.write_json(directory / "status.json", {
        "run_id": directory.name, "status": "completed", "completed": 1, "total": 1,
        "source_revision": "0" * 64,
        "settings": {"strategy": "direct_structural", "alert_mode": "native_center_signals"},
    })
    return ScreeningManager(tmp_path), directory, record


def test_old_native_source_remains_reviewable_but_not_current_or_monitorable(frozen_native_run):
    manager, directory, record = frozen_native_run
    result = manager.results(persist=True)
    assert result["source_current"] is False
    assert result["frozen_native_review"] is True
    assert result["result_reader_revision"] == screening._RESULT_READER_REVISION
    assert len(result["selected"]) == 1
    assert result["selected"][0]["evidence_verified"] is True
    assert result["observations"] == []
    monitored = manager.committed_results()
    assert monitored["selected"] == [] and monitored["processed_symbols"] == []
    assert monitored["source_current"] is False
    point = record["rows"][0]["selected"][0]["point"]
    evidence = manager.evidence(directory.name, "SH.600183", "30m", point["point_id"])
    assert evidence["metadata"]["verified"] is True
    assert evidence["metadata"]["source_current"] is False


def test_native_candidate_and_saved_evidence_chart_publish_identical_exit_references(frozen_native_run):
    manager, directory, record = frozen_native_run
    selected, = manager.results()["selected"]
    point = selected["point"]
    chart = manager.evidence_history(directory.name, "SH.600183", "30m", point["point_id"],
                                     first=True, start=0, end=selected["source_closed_at"] + 1800)
    assert chart["strict_structure"]["point_exit_plans"][point["point_id"]] == selected["exit_plan"]
    assert selected["exit_plan"]["take_profit"]["status"] == "waiting"
    assert selected["exit_plan"]["profit_observation"] is None
    assert chart["strict_structure"]["levels"][0]["points"][0] == record["rows"][0]["selected"][0]["point"]


def test_historical_native_review_still_rejects_partial_or_corrupt_proof(frozen_native_run):
    manager, directory, record = frozen_native_run
    candidate = record["rows"][0]["selected"][0]
    record["rows"][0]["selected"].append({**candidate, "point": {**candidate["point"], "point_id": "forged"}})
    (directory / "results.jsonl").write_text(json.dumps(record) + "\n", encoding="utf-8")
    result = manager.results()
    assert result["source_current"] is False and result["frozen_native_review"] is True
    assert result["selected"] == []
    assert result["errors"][0]["reasons"] == ["EVIDENCE_MISSING"]


@pytest.mark.parametrize("field,value", [("evidence_scope", "recursive_point"), ("rule_basis", "old_native_rule")])
def test_historical_native_reader_never_falls_back_to_incompatible_point_rules(frozen_native_run, field, value):
    manager, directory, record = frozen_native_run
    record["rows"][0]["selected"][0]["point"][field] = value
    (directory / "results.jsonl").write_text(json.dumps(record) + "\n", encoding="utf-8")
    result = manager.results()
    assert result["selected"] == result["observations"] == []
    assert result["errors"][0]["reasons"] == ["STRUCTURE_MISMATCH"]


@pytest.mark.parametrize("update", [
    {"source_revision": None}, {"source_revision": "unverified-old-version"},
    {"settings": {"strategy": "direct_structural"}}, {"status": "running"},
])
def test_unidentified_or_incomplete_run_is_not_opened_for_historical_review(frozen_native_run, update):
    manager, directory, record = frozen_native_run
    state = screening.read_state_json(directory / "status.json")
    screening.write_json(directory / "status.json", {**state, **update})
    result = manager.results()
    assert result["frozen_native_review"] is False
    assert result["selected"] == result["observations"] == []


def test_historical_review_cache_revalidates_after_the_current_source_changes(frozen_native_run, monkeypatch):
    from unittest.mock import Mock
    manager, directory, record = frozen_native_run
    read = Mock(wraps=manager._read_results)
    monkeypatch.setattr(manager, "_read_results", read)
    first = manager.results(persist=True)
    assert first["selected"] and read.call_count == 1
    monkeypatch.setattr(screening, "source_revision", lambda: "1" * 64)
    manager._revision_checked = None
    changed = manager.results()
    assert changed["selected"] and changed["source_current"] is False
    assert changed["current_source_revision"] == "1" * 64
    assert read.call_count == 2
