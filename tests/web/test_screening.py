"""Manual job validation, persistence and authenticated routes."""

import json
import gzip
from datetime import datetime
from pathlib import Path
from unittest.mock import Mock

import pytest

from cl_app import create_app
from cl_app.services import screening


@pytest.mark.parametrize("body", [None, [], {}, {"scope":"all_a","codes":["SH.600000"]},
    {"scope":"codes","codes":[]}, {"scope":"codes","codes":["all"]},
    {"scope":"all_a","workers":True}, {"scope":"all_a","workers":99},
    {"scope":"all_a","frequencies":["5m","5m"]}, {"scope":"all_a","point_types":[]},
    {"scope":"all_a","max_anchor_gain_pct":True}, {"scope":"all_a","max_anchor_gain_pct":float("inf")},
    {"scope":"all_a","recent_sessions":0}, {"scope":"all_a","extra":True}])
def test_invalid_screen_scope_is_rejected_before_process_launch(body):
    with pytest.raises(ValueError):
        screening.validate_settings(body)


def test_screening_allows_twelve_workers_on_sixteen_logical_processors():
    assert screening.validate_settings({"scope": "all_a"})["workers"] == 12
    assert screening.validate_settings({"scope": "all_a", "workers": 12})["workers"] == 12
    with pytest.raises(ValueError, match="workers"):
        screening.validate_settings({"scope": "all_a", "workers": 13})


def test_direct_center_alerts_use_only_the_physical_period():
    settings = screening.validate_settings({
        "scope": "codes", "codes": ["SZ.001309"],
        "strategy": "direct_structural", "frequencies": ["1m"],
        "point_types": ["3sell"], "structural_levels": [0],
        "minimum_operating_frequency": "1m",
    })
    assert settings["strategy"] == "direct_structural"
    assert settings["frequencies"] == ["1m"]
    assert settings["structural_levels"] == [0]
    assert "confirmation_frequency" not in settings
    assert screening.validate_settings({"scope": "all_a"})["structural_levels"] == [0]
    assert screening.validate_settings({"scope": "all_a"})["strategy"] == "direct_structural"
    multiple = screening.validate_settings({
        "scope": "all_a", "strategy": "direct_structural",
        "frequencies": ["1m", "5m", "30m"],
    })
    assert multiple["frequencies"] == ["1m", "5m", "30m"]
    assert multiple["alert_mode"] == "native_center_signals"
    with pytest.raises(ValueError, match="结构级别"):
        screening.validate_settings({"scope": "all_a", "structural_levels": [1]})
    with pytest.raises(ValueError, match="第二周期"):
        screening.validate_settings({
            "scope": "all_a", "strategy": "direct_structural",
            "confirmation_frequency": "1m",
        })


def test_minimum_operating_period_must_be_explicit_and_have_the_same_frozen_source():
    assert "minimum_operating_frequency" not in screening.validate_settings({"scope": "all_a"})
    for frequency in ("1m", "5m"):
        assert screening.validate_settings({
            "scope": "all_a", "strategy": "direct_structural",
            "frequencies": [frequency], "minimum_operating_frequency": frequency,
        })["minimum_operating_frequency"] == frequency
    assert screening.validate_settings({
        "scope": "codes", "codes": ["SH.600000"], "strategy": "direct_structural",
        "frequencies": ["30m"], "minimum_operating_frequency": "30m",
    })["minimum_operating_frequency"] == "30m"
    for body in (
        {"scope": "all_a", "minimum_operating_frequency": "30m"},
        {"scope": "all_a", "minimum_operating_frequency": True},
        {"scope": "codes", "codes": ["SH.600000"], "strategy": "direct_structural",
         "frequencies": ["5m", "30m"], "minimum_operating_frequency": "30m"},
        {"scope": "codes", "codes": ["SH.600000"], "strategy": "direct_structural",
         "frequencies": ["5m"], "minimum_operating_frequency": "1m"},
    ):
        with pytest.raises(ValueError, match="最低操作周期"):
            screening.validate_settings(body)


def test_new_manual_run_is_detached_bounded_and_not_restarted_by_reads(tmp_path, monkeypatch):
    manager = screening.ScreeningManager(tmp_path)
    popen = Mock(return_value=Mock(pid=1234))
    monkeypatch.setattr(screening.subprocess, "Popen", popen)
    assert manager.status()["status"] == "idle"
    first = manager.start({"scope":"all_a"})
    assert first["status"] == "starting"
    with pytest.raises(RuntimeError):
        manager.start({"scope":"all_a"})
    assert popen.call_count == 1
    call = popen.call_args
    assert call.kwargs["cwd"].joinpath("src/chanlun").is_dir()
    assert call.kwargs["env"]["OMP_NUM_THREADS"] == "1"
    assert manager.cancel()["cancel_requested"] is True
    assert popen.call_count == 1


def test_quiet_run_preserves_selection_settings_and_persists_delivery_policy(tmp_path, monkeypatch):
    manager = screening.ScreeningManager(tmp_path)
    monkeypatch.setattr(screening.subprocess, "Popen", Mock(return_value=Mock(pid=1234)))
    launched = manager.start({"scope": "all_a_watchlist"}, external_notifications=False, force_rebuild=True)
    request = json.loads((tmp_path/launched["run_id"]/"request.json").read_text(encoding="utf-8"))
    assert request["external_notifications"] is False
    assert request["force_rebuild"] is True
    assert launched["external_notifications"] is False
    assert "external_notifications" not in request["settings"]
    assert request["settings"] == screening.validate_settings({"scope": "all_a_watchlist"})


def test_results_keep_errors_separate_from_no_signal_and_survive_new_manager(tmp_path):
    run_id = "a" * 32
    directory = tmp_path / run_id
    screening.write_json(tmp_path/"latest.json", {"run_id":run_id})
    screening.write_json(directory/"status.json", {"run_id":run_id,"status":"completed",
                                                 "source_revision": screening.source_revision()})
    row = {"code":"SH.600000","name":"example","rows":[{"code":"SH.600000","name":"example",
           "frequency":"5m","selected":[],"recent_rejections":[],"data_errors":["STALE_DATA"],"reason_counts":{}}]}
    (directory/"results.jsonl").write_bytes((json.dumps(row)+'\n{"partial":"').encode()+b'\xe4\xb8')
    result = screening.ScreeningManager(tmp_path).results()
    assert result["selected"] == []
    assert result["errors"][0]["reasons"] == ["STALE_DATA"]
    assert result["status"] == "completed"


def test_screening_routes_require_login_and_manual_post(monkeypatch):
    app = create_app(test_config={"TESTING":True,"VALIDATE_WEB_SECURITY":False,"WTF_CSRF_ENABLED":False})
    client = app.test_client()
    for route in ("/screening", "/screening/status", "/screening/results", "/screening/evidence", "/screening/evidence/data"):
        assert client.get(route).status_code == 302
    assert client.post('/screening/start',json={"scope":"all_a"}).status_code == 401
    app.config["LOGIN_DISABLED"] = True
    fake = Mock()
    fake.status.return_value = {"status":"idle"}
    fake.start.return_value = {"status":"starting"}
    monkeypatch.setattr('cl_app.blueprints.screening.manager', fake)
    assert client.get('/screening/status').get_json()["status"] == "idle"
    assert client.get('/screening/start').status_code == 405
    assert client.post('/screening/start',json={"scope":"all_a"}).status_code == 202
    assert client.get('/screening').status_code == 200


def test_state_replacement_retries_transient_windows_reader_lock(tmp_path, monkeypatch):
    from chanlun.screening import runner
    original = runner.os.replace
    calls = []
    path = tmp_path / "status.json"
    path.write_text('{"status":"running"}', encoding="utf-8")
    def temporarily_locked(source, target):
        calls.append(target)
        if len(calls) < 3:
            assert json.loads(path.read_text())["status"] == "running"
            raise PermissionError("simulated sharing violation")
        original(source, target)
    monkeypatch.setattr(runner.os, "replace", temporarily_locked)
    monkeypatch.setattr(runner.time, "sleep", lambda _: None)
    runner.write_json(path, {"status":"completed"})
    assert len(calls) == 3
    assert json.loads(path.read_text())["status"] == "completed"


@pytest.mark.parametrize("locked_file", ["latest.json", "status.json"])
def test_status_read_survives_transient_windows_replace_lock(tmp_path, monkeypatch, locked_file):
    run_id = "a" * 32
    screening.write_json(tmp_path / "latest.json", {"run_id": run_id})
    screening.write_json(tmp_path / run_id / "status.json", {"run_id": run_id, "status": "completed"})
    original = Path.read_text
    failures = []

    def briefly_locked(path, *args, **kwargs):
        if path.name == locked_file and len(failures) < 2:
            failures.append(path)
            raise PermissionError("simulated atomic replace sharing violation")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", briefly_locked)
    monkeypatch.setattr(screening.time, "sleep", lambda _: None)
    state = screening.ScreeningManager(tmp_path).status()
    assert state["run_id"] == run_id
    assert state["status"] == "completed"
    assert len(failures) == 2


def test_state_read_does_not_hide_persistent_permission_errors(tmp_path, monkeypatch):
    locked = Mock(side_effect=PermissionError("access denied"))
    monkeypatch.setattr(Path, "read_text", locked)
    monkeypatch.setattr(screening.time, "sleep", lambda _: None)
    with pytest.raises(PermissionError, match="access denied"):
        screening.read_state_json(tmp_path / "status.json")
    assert 1 < locked.call_count <= 15


def test_state_read_reports_malformed_committed_json_without_retry(tmp_path, monkeypatch):
    path = tmp_path / "status.json"
    path.write_text('{"status":', encoding="utf-8")
    sleep = Mock()
    monkeypatch.setattr(screening.time, "sleep", sleep)
    with pytest.raises(json.JSONDecodeError):
        screening.read_state_json(path)
    sleep.assert_not_called()


def test_results_pin_task_identity_during_a_concurrent_start(tmp_path, monkeypatch):
    first, second = tmp_path / ("a" * 32), tmp_path / ("b" * 32)
    for directory, code in ((first, "SH.600000"), (second, "SZ.000001")):
        screening.write_json(directory / "status.json", {
            "run_id": directory.name, "status": "completed", "completed": 1,
            "source_revision": screening.source_revision(),
            "settings": {"codes": [code]},
        })
        row = {"code": code, "rows": [{"code": code, "frequency": "5m",
            "selected": [{"point": {"point_id": "point", "available_at": 100}}],
            "recent_rejections": [], "reason_counts": {}}]}
        (directory / "results.jsonl").write_text(json.dumps(row) + "\n", encoding="utf-8")
        (directory / "evidence").mkdir()
        (directory / "evidence" / f"{code}_5m.parquet").write_bytes(b"fixture")
        snapshot = {"symbol": code, "source_frequency": "5m", "levels": [{
            "points": [row["rows"][0]["selected"][0]["point"]],
        }]}
        (directory / "evidence" / f"{code}_5m.json.gz").write_bytes(gzip.compress(json.dumps(snapshot).encode()))
    manager = screening.ScreeningManager(tmp_path)
    directory_read = Mock(side_effect=[first, second])
    monkeypatch.setattr(manager, "_directory", directory_read)
    result = manager.results()
    assert result["run_id"] == first.name
    assert result["settings"]["codes"] == [s["code"] for s in result["selected"]]
    assert directory_read.call_count == 1


@pytest.fixture
def frozen_run(tmp_path):
    import pandas as pd
    from chanlun.screening.runner import _save_evidence
    run_id = "e" * 32
    directory = tmp_path / run_id
    times = [1789109400, 1789109700, 1789110000]
    frame = pd.DataFrame({"date": pd.to_datetime(times, unit="s", utc=True),
                          "open": [12., 13., 14.], "high": [14., 14., 15.],
                          "low": [11., 12., 13.], "close": [13., 14., 14.], "volume": [100, 200, 300]})
    point = {"point_id": "waiting", "point_type": "3buy", "center_ordinal": 1, "side": "buy", "status": "approaching",
             "structural_level": 0, "source_kind": "segment", "anchor_at": times[0],
             "available_at": times[-1], "confirmed_at": None, "missing_conditions": ["terminal_unit_locked"]}
    geometry = {"fxs": [], "bis": [], "xds": [{"points": [{"time": times[0], "price": 11},
                                                                       {"time": times[-1], "price": 14}]}],
                "macd_dif": [1, 2, 3], "macd_dea": [0, 1, 2], "macd_hist": [2, 2, 2]}
    snapshot = {"schema": "chanlun-chart-structure", "symbol": "SH.600000", "source_frequency": "5m",
                "source_closed_at": times[-1], "levels": [{"structural_level": 0, "points": [point]}], "chart": geometry}
    manifest = _save_evidence(directory, "SH.600000", "5m", frame, snapshot)
    record = {"code": "SH.600000", "rows": [{"code": "SH.600000", "frequency": "5m",
              "source_closed_at": times[-1], "evidence": manifest, "selected": [],
              "observations": [{"point": point, "reasons": ["NOT_CONFIRMED"], "observation_validation": "checked"}],
              "recent_rejections": [], "reason_counts": {}}]}
    screening.write_json(tmp_path / "latest.json", {"run_id": run_id})
    screening.write_json(directory / "status.json", {"run_id": run_id, "status": "completed", "completed": 1,
                                                   "source_revision": screening.source_revision()})
    (directory / "results.jsonl").write_text(json.dumps(record) + "\n", encoding="utf-8")
    return screening.ScreeningManager(tmp_path), run_id, snapshot


def test_frozen_history_uses_saved_geometry_and_causal_candle_coordinates(frozen_run, monkeypatch):
    manager, source, snapshot = frozen_run
    monkeypatch.setattr("chanlun.screening.runner.snapshot_for", Mock(side_effect=AssertionError("must not rebuild")))
    data = manager.evidence_history(source, "SH.600000", "5m", "waiting", first=True, start=0, end=2000000000)
    assert data["s"] == "ok" and data["strict_structure_mode"] == "replace"
    assert data["t"] == [1789109400, 1789109700, 1789110000]
    assert data["xds"] == snapshot["chart"]["xds"]
    assert data["macd_dif"] == [1, 2, 3]
    assert "chart" not in data["strict_structure"]
    assert data["strict_structure"]["source_closed_at"] == data["t"][-1]
    part = manager.evidence_history(source, "SH.600000", "5m", "waiting", first=False, start=1789109100, end=1789109400)
    assert part["t"] == [1789109400] and part["macd_dif"] == [1]
    assert part["strict_structure_mode"] == "unchanged"
    assert manager.evidence_history(source, "SH.600000", "5m", "waiting", first=False, start=0, end=1789109100)["s"] == "no_data"


def test_direct_one_minute_u295_witness_is_visible_only_after_frozen_recheck(frozen_run):
    import pandas as pd

    from chanlun.screening.lower_first_timeline import (
        observe_independent_lower_structural_witnesses,
    )
    from chanlun.screening.runner import _save_evidence

    manager, source, snapshot = frozen_run
    directory = manager.root / source
    frame = pd.read_parquet(directory / "evidence/SH.600000_5m.parquet")
    point = snapshot["levels"][0]["points"][0]
    point.update(
        point_id="lower", status="confirmed", confirmed_at=snapshot["source_closed_at"],
        available_at=snapshot["source_closed_at"], price_basis_revision="basis",
        anchor_tick=12,
    )
    snapshot.update(
        source_frequency="1m", price_basis_revision="basis",
        structure_price_quantum="0.01", snapshot_revision="lower-revision",
    )
    witnesses = list(observe_independent_lower_structural_witnesses(
        snapshot, known_from=point["anchor_at"],
    ))
    assert len(witnesses) == 1
    manifest = _save_evidence(directory, "SH.600000", "1m", frame, snapshot)
    row = {
        "code": "SH.600000", "market": "a", "name": "fixture",
        "frequency": "1m", "source_closed_at": snapshot["source_closed_at"],
        "selected": [], "observations": [], "recent_rejections": [],
        "reason_counts": {}, "evidence": manifest,
        "u295_witness_known_from": point["anchor_at"],
        "u295_lower_structural_witnesses": witnesses,
    }
    record = {"code": "SH.600000", "market": "a", "rows": [row]}
    path = directory / "results.jsonl"
    path.write_text(json.dumps(record) + "\n", encoding="utf-8")
    state = {"run_id": source, "status": "completed", "completed": 1,
             "settings": {"strategy": "direct_structural"}}
    result = manager._read_results(directory, state)
    assert result["u295_lower_structural_witness_count"] == 1
    verified, = result["u295_lower_structural_witnesses"]
    assert verified["point_id"] == "lower"
    assert verified["evidence_verified"] is True
    assert verified["author_interval_entry_certified"] is False
    assert verified["tradable"] is False
    assert not result["errors"]

    row["u295_lower_structural_witnesses"][0]["point_id"] = "borrowed"
    path.write_text(json.dumps(record) + "\n", encoding="utf-8")
    tampered = manager._read_results(directory, state)
    assert tampered["u295_lower_structural_witnesses"] == []
    assert tampered["errors"][0]["reasons"] == ["U295_WITNESS_EVIDENCE_MISMATCH"]


def test_frozen_first_class_result_must_equal_its_verified_comparison_evidence(frozen_run):
    import hashlib
    from copy import deepcopy

    from chanlun.screening.rules import point_semantic_reasons

    manager, source, snapshot = frozen_run
    directory = manager.root / source
    cutoff = snapshot["source_closed_at"]
    center = {
        "center_id": "Z", "structural_level": 0, "source_kind": "segment",
        "price_basis_revision": "raw", "established_at": cutoff - 1200,
        "third_class_confirmed": True, "completion_direction": "down",
        "completion_return_unit_id": "return",
        "boundary_divergence_id": "divergence",
        "boundary_anchor_unit_id": "end",
    }
    point = {
        "point_id": "first", "point_type": "1buy", "side": "buy",
        "status": "confirmed", "confirmed_at": cutoff, "available_at": cutoff,
        "anchor_at": cutoff - 600, "anchor_tick": 11, "invalidation_tick": 11,
        "center_id": "Z", "source_kind": "segment", "structural_level": 0,
        "price_basis_revision": "raw", "evidence_codes": ["two_separated_centers"],
        "divergence": {
            "kind": "trend", "direction": "down", "structural_level": 0,
            "divergence_id": "divergence", "signal_unit_id": "end",
            "source_kind": "segment", "price_basis_revision": "raw",
            "comparison_center_id": "Z",
            "comparison_center_established_at": center["established_at"],
            "comparison_center_known_at": center["established_at"],
            "available_at": cutoff, "signal_leg_unit_ids": ["leave", "return", "end"],
            "metrics": {"is_divergent": True},
        },
    }
    snapshot["levels"][0].update(points=[point], centers=[center])
    snapshot["price_basis_revision"] = "raw"
    packed = gzip.compress(json.dumps(snapshot).encode())
    (directory / "evidence/SH.600000_5m.json.gz").write_bytes(packed)
    record = json.loads((directory / "results.jsonl").read_text(encoding="utf-8"))
    row = record["rows"][0]
    row["selected"] = [{"point": deepcopy(point)}]
    row["observations"] = []
    row["evidence"].update(snapshot_sha256=hashlib.sha256(packed).hexdigest(),
                           snapshot_bytes=len(packed))
    candidate = row["selected"][0]["point"]
    candidate["evidence_codes"].append("candidate_only_extra")
    assert point_semantic_reasons(candidate, {"first": point}, {"Z": center}) == []
    (directory / "results.jsonl").write_text(json.dumps(record) + "\n", encoding="utf-8")

    result = manager.results()
    assert not result["selected"]
    rejected = next(item for item in result["recent_rejections"]
                    if item["point"]["point_id"] == "first")
    assert "STRUCTURE_MISMATCH" in rejected["reasons"]


def test_higher_third_buy_frozen_result_rechecks_exact_entry_evidence(frozen_run, monkeypatch):
    import hashlib
    from copy import deepcopy

    manager, source, snapshot = frozen_run
    directory = manager.root / source
    first, anchor_end, cutoff = 1789109400, 1789109700, 1789110000
    point = {
        "point_id": "third", "point_type": "3buy", "side": "buy", "status": "confirmed",
        "structural_level": 1, "source_kind": "trend_type", "center_id": "center",
        "center_ordinal": 1, "anchor_unit_id": "return", "anchor_at": anchor_end,
        "anchor_tick": 1200, "invalidation_tick": 1100,
        "available_at": cutoff, "confirmed_at": cutoff, "missing_conditions": [],
    }
    precision = {
        "point_id": "third", "point_type": "3buy", "parent_center_id": "center",
        "lower_return_trend_id": "return", "formal_third_point_available_at": cutoff,
        "point_status": "confirmed", "tradable": False,
        "formal_third_point_independent": True,
        "status": "bounded_precision_condition_observed",
        "author_entry_condition_supported": True,
        "precision_branch": "double_subsub_return", "precision_known_at": anchor_end,
        "parent_center_initial_known_at": first - 200,
    }
    def unit(identity, start, end, start_tick, end_tick):
        return {"unit_id": identity, "start_time": start, "end_time": end,
                "start_tick": start_tick, "end_tick": end_tick,
                "low_tick": min(start_tick, end_tick), "high_tick": max(start_tick, end_tick),
                "direction": "up" if end_tick > start_tick else "down",
                "confirmed_at": end, "available_at": end, "locked": True, "forming": False}

    roles = [unit("a", first - 500, first - 400, 900, 1100),
             unit("b", first - 400, first - 300, 1100, 1000),
             unit("c", first - 300, first - 200, 1000, 1200)]
    assert precision["parent_center_initial_known_at"] == max(
        role["available_at"] for role in roles
    )
    body = unit("d", first - 200, first - 100, 1200, 1050)
    leave = unit("leave", first - 100, first, 1050, 1400)
    ret = unit("return", first, anchor_end, 1400, 1200)
    center = {"center_id": "center", "structural_level": 1, "source_kind": "trend_type",
              "formation_rule": "recursive_three", "third_class_confirmed": True,
              "core": {"zd_tick": 1000, "zg_tick": 1100},
              "core_unit_ids": [role["unit_id"] for role in roles],
              "body_unit_ids": [role["unit_id"] for role in [*roles, body]],
              "entry_unit_id": None, "establishment_leave_unit_id": None,
              "establishment_segments": roles,
              "overlap_components": [*roles, body, leave],
              "completion_leave_unit_id": "leave", "completion_return_unit_id": "return",
              "lifecycle_leaving_segment": leave, "completion_return_segment": ret}
    snapshot["levels"] = [{"structural_level": 1, "points": [point], "centers": [center],
                           "third_point_entry_precision_rule_states": [precision]}]
    snapshot["screening_units_by_level"] = {"1": [
        {"unit_id": "return", "start_time": first, "end_time": anchor_end,
            "start_tick": 1400, "end_tick": 1200, "direction": "down", "locked": True,
         "forming": False, "confirmed_at": anchor_end},
        {"unit_id": "next", "start_time": anchor_end, "end_time": cutoff,
         "start_tick": 1200, "end_tick": 1400, "direction": "up", "locked": False,
         "forming": True, "confirmed_at": None},
    ]}
    record_path = directory / "results.jsonl"
    record = json.loads(record_path.read_text(encoding="utf-8"))
    row = record["rows"][0]
    row["selected"] = [{"point": point, "entry_precision_evidence": precision}]
    row["observations"] = []

    def persist():
        raw = gzip.compress(json.dumps(snapshot).encode())
        (directory / "evidence/SH.600000_5m.json.gz").write_bytes(raw)
        row["evidence"].update(snapshot_sha256=hashlib.sha256(raw).hexdigest(),
                               snapshot_bytes=len(raw))
        record_path.write_text(json.dumps(record) + "\n", encoding="utf-8")

    persist()
    monkeypatch.setattr("chanlun.screening.runner.snapshot_for", Mock(side_effect=AssertionError("must not rebuild")))
    result = manager.results()
    assert [s["point"]["point_id"] for s in result["selected"]] == ["third"]
    assert manager.evidence(source, "SH.600000", "5m", "third")["candidate"]["entry_precision_evidence"] == precision

    missing_clock = {**precision, "parent_center_initial_known_at": None}
    snapshot["levels"][0]["third_point_entry_precision_rule_states"] = [missing_clock]
    row["selected"][0]["entry_precision_evidence"] = missing_clock
    persist()
    result = manager.results()
    assert not result["selected"]
    assert "THIRD_ENTRY_PRECISION_PENDING" in result["recent_rejections"][-1]["reasons"]
    snapshot["levels"][0]["third_point_entry_precision_rule_states"] = [precision]
    row["selected"][0]["entry_precision_evidence"] = precision
    persist()
    assert [s["point"]["point_id"] for s in manager.results()["selected"]] == ["third"]

    intact_center = deepcopy(center)
    center["completion_return_segment"]["low_tick"] = 1099
    persist()
    result = manager.results()
    assert not result["selected"]
    assert "STRUCTURE_MISMATCH" in result["recent_rejections"][-1]["reasons"]
    with pytest.raises(ValueError):
        manager.evidence(source, "SH.600000", "5m", "third")
    snapshot["levels"][0]["centers"][0] = intact_center
    persist()
    assert [s["point"]["point_id"] for s in manager.results()["selected"]] == ["third"]

    row["selected"][0]["entry_precision_evidence"] = {**precision, "precision_known_at": cutoff + 300}
    persist()
    result = manager.results()
    assert not result["selected"]
    assert "THIRD_ENTRY_PRECISION_EVIDENCE_MISMATCH" in result["recent_rejections"][-1]["reasons"]
    with pytest.raises(ValueError):
        manager.evidence(source, "SH.600000", "5m", "third")

    snapshot["levels"][0]["third_point_entry_precision_rule_states"] = []
    row["selected"][0]["entry_precision_evidence"] = None
    persist()
    result = manager.results()
    assert not result["selected"]
    assert "THIRD_ENTRY_PRECISION_PENDING" in result["recent_rejections"][-1]["reasons"]

    row["selected"] = []
    row["recent_rejections"] = [{"point": point, "entry_precision_evidence": None,
                                 "reasons": ["THIRD_ENTRY_PRECISION_PENDING"]}]
    row["reason_counts"] = {"THIRD_ENTRY_PRECISION_PENDING": 1}
    persist()
    result = manager.results()
    assert not result["selected"] and not result["observations"]
    pending, = result["entry_waiting"]
    assert pending["selection_status"] == "entry_pending"
    assert pending["point"]["status"] == "confirmed"
    assert pending["decision_reasons"] == ["THIRD_ENTRY_PRECISION_PENDING"]
    assert manager.evidence(source, "SH.600000", "5m", "third")["candidate"]["entry_waiting"]


def test_frozen_chart_route_pins_run_point_and_period_and_never_falls_back(frozen_run, monkeypatch):
    manager, source, _ = frozen_run
    monkeypatch.setattr(screening, "manager", manager)
    monkeypatch.setattr("cl_app.blueprints.screening.manager", manager)
    app = create_app(test_config={"TESTING": True, "VALIDATE_WEB_SECURITY": False, "LOGIN_DISABLED": True})
    client = app.test_client()
    response = client.get("/screening/evidence", query_string={"source": source, "code": "SH.600000", "frequency": "5m", "point": "waiting"})
    assert response.status_code == 302 and response.location.startswith("/?")
    assert "screening_source=" + source in response.location
    query = {"symbol": "a:SH.600000", "resolution": "5", "firstDataRequest": "true", "to": "2000000000",
             "screening_source": source, "screening_point": "waiting"}
    response = client.get("/tv/history", query_string=query)
    assert response.status_code == 200 and response.get_json()["strict_structure_mode"] == "replace"
    assert response.headers["Cache-Control"] == "private, no-store"
    for changed in ({"screening_source": "a" * 32}, {"screening_point": "unknown"}, {"resolution": "30"}, {"symbol": "us:AAPL"}):
        denied = client.get("/tv/history", query_string={**query, **changed})
        assert denied.status_code == 409 and denied.get_json()["s"] == "error"
    # A damaged proof is unavailable even though the observation is still in the ledger.
    (manager.root / source / "evidence/SH.600000_5m.json.gz").write_bytes(b"broken")
    assert not manager.results()["observations"]
    assert client.get("/tv/history", query_string=query).status_code == 409


def test_frozen_evidence_rejects_unsupported_structure_with_json_error(frozen_run, monkeypatch):
    import hashlib
    manager, source, snapshot = frozen_run
    directory = manager.root / source
    raw = gzip.compress(json.dumps({**snapshot, "levels": [None]}).encode())
    (directory / "evidence/SH.600000_5m.json.gz").write_bytes(raw)
    records = directory / "results.jsonl"
    record = json.loads(records.read_text(encoding="utf-8"))
    # Valid checksums prove the bytes match the producer, not that an older
    # producer's structure format can be consumed by the current reader.
    record["rows"][0]["evidence"].update(snapshot_sha256=hashlib.sha256(raw).hexdigest(), snapshot_bytes=len(raw))
    records.write_text(json.dumps(record) + "\n", encoding="utf-8")
    monkeypatch.setattr("cl_app.blueprints.screening.manager", manager)
    monkeypatch.setattr("chanlun.screening.runner.snapshot_for", Mock(side_effect=AssertionError("read must not rebuild")))
    app = create_app(test_config={"TESTING": True, "LOGIN_DISABLED": True, "VALIDATE_WEB_SECURITY": False})
    try:
        response = app.test_client().get("/screening/evidence/data", query_string={
            "source": source, "code": "SH.600000", "frequency": "5m", "point": "waiting",
        })
    finally:
        app.extensions["shutdown_runtime_services"]()
    assert response.status_code == 409
    assert "结构" in response.get_json()["error"]


def test_rows_appended_after_progress_snapshot_wait_for_the_next_read(tmp_path):
    run_id = "a" * 32
    directory = tmp_path / run_id
    screening.write_json(tmp_path / "latest.json", {"run_id": run_id})
    screening.write_json(directory / "status.json", {"run_id": run_id, "status": "completed", "completed": 1,
                                                   "source_revision": screening.source_revision()})
    def record(code):
        (directory / "evidence").mkdir(exist_ok=True)
        (directory / "evidence" / f"{code}_5m.parquet").write_bytes(b"fixture")
        snapshot = {"symbol": code, "source_frequency": "5m", "levels": [{
            "points": [{"point_id": "point", "available_at": 100}],
        }]}
        (directory / "evidence" / f"{code}_5m.json.gz").write_bytes(gzip.compress(json.dumps(snapshot).encode()))
        return json.dumps({"code": code, "rows": [{"code": code, "frequency": "5m",
            "selected": [{"point": {"point_id": "point", "available_at": 100}}], "recent_rejections": [], "reason_counts": {}}]})
    (directory / "results.jsonl").write_text(record("SH.600000") + "\n" + record("SZ.000001") + "\n", encoding="utf-8")
    result = screening.ScreeningManager(tmp_path).results()
    assert result["completed"] == result["selected_symbols"] == 1
    assert result["selected"][0]["code"] == "SH.600000"


@pytest.mark.parametrize("observed,expected", [
    (datetime(2026, 9, 13, 12, tzinfo=screening.CN), "current"),
    (datetime(2026, 9, 14, 9, 34, tzinfo=screening.CN), "current"),
    (datetime(2026, 9, 14, 9, 35, tzinfo=screening.CN), "stale"),
    (datetime(2027, 1, 4, 10, tzinfo=screening.CN), "unknown"),
])
def test_results_expire_on_new_closed_bars_and_fail_closed_without_calendar(tmp_path, observed, expected):
    run_id = "a" * 32
    cutoff = int(datetime(2026, 9, 11, 15, tzinfo=screening.CN).timestamp())
    screening.write_json(tmp_path / "latest.json", {"run_id": run_id})
    screening.write_json(tmp_path / run_id / "status.json", {
        "run_id": run_id, "status": "completed", "cutoffs": {"5m": cutoff, "30m": cutoff},
        "source_revision": screening.source_revision(),
    })
    result = screening.ScreeningManager(tmp_path, now=lambda: observed).status()
    assert result["freshness_state"] == ("cutoff_current" if expected == "current" else expected)
    assert result["cutoff_current"] is (None if expected == "unknown" else expected == "current")
    assert result["data_current"] is (False if expected == "stale" else None)
    assert result["input_revision_state"] == "not_rechecked"
    assert result["source_current"] is True


def test_unknown_algorithm_revision_is_never_current(tmp_path):
    run_id = "a" * 32
    screening.write_json(tmp_path / "latest.json", {"run_id": run_id})
    screening.write_json(tmp_path / run_id / "status.json", {"run_id": run_id, "status": "completed"})
    state = screening.ScreeningManager(tmp_path).status()
    assert state["source_current"] is False
    assert state["current_source_revision"] == screening.source_revision()


@pytest.mark.parametrize("advance_minutes,current", [(0, True), (5, False)])
def test_cross_market_freshness_uses_each_market_clock(tmp_path, advance_minutes, current):
    from datetime import timedelta
    from chanlun.screening.markets import closed_bar_cutoff, market_context
    observed = datetime(2026, 9, 17, 23, 10, tzinfo=screening.CN)
    a_cutoff = closed_bar_cutoff("a", "SH.600088", observed, "5m")
    us_cutoff = market_context("us", "TSLA.US", observed, "5m", 1, 1)["cutoff"]
    state = {"cutoffs": {"5m": a_cutoff},
             "market_cutoffs": {"a": {"5m": a_cutoff}, "us": {"5m": us_cutoff}},
             "market_clock_symbols": {"a": "SH.600088", "us": "TSLA.US"}}
    now = observed + timedelta(minutes=advance_minutes)
    result = screening.ScreeningManager(tmp_path, now=lambda: now)._freshness(state)
    assert result["cutoff_current"] is current
    assert result["expected_market_cutoffs"]["a"]["5m"] == a_cutoff
    assert result["expected_market_cutoffs"]["us"]["5m"] == us_cutoff + advance_minutes * 60


def test_market_clock_is_pending_until_every_selected_period_has_a_cutoff(tmp_path):
    result = screening.ScreeningManager(tmp_path)._freshness({
        "cutoffs": {"1m": 1000}, "market_cutoffs": {"us": {"1m": 1000, "30m": None}},
    })
    assert result["cutoff_current"] is None
    assert result["freshness_state"] == "pending"


@pytest.mark.parametrize("advance,expected", [(0, "partial"), (300, "stale")])
def test_missing_markets_do_not_hide_valid_market_freshness(tmp_path, monkeypatch, advance, expected):
    calls = []

    def cutoff(market, code, now, frequency):
        calls.append((market, frequency))
        return {"a": 1000, "us": 2000 + advance, "currency_spot": 3000}[market]

    monkeypatch.setattr("chanlun.screening.markets.closed_bar_cutoff", cutoff)
    result = screening.ScreeningManager(tmp_path)._freshness({
        "status": "completed",
        "market_cutoffs": {"a": {"5m": 1000}, "fx": {"5m": None}, "ny_futures": {"5m": None},
                           "us": {"5m": 2000}, "currency_spot": {"5m": 3000}},
        "market_clock_symbols": {"us": "AAPL.US", "currency_spot": "BTC/USDT"},
    })
    assert result["freshness_state"] == expected
    assert result["cutoff_current"] is (False if advance else None)
    assert result["data_current"] is (False if advance else None)
    assert result["market_freshness"]["a"]["5m"]["state"] == "cutoff_current"
    assert result["market_freshness"]["fx"]["5m"]["state"] == "unknown"
    assert result["market_freshness"]["ny_futures"]["5m"]["state"] == "unknown"
    assert len(calls) == 3
    assert result["expected_market_cutoffs"]["us"]["5m"] == 2000 + advance


@pytest.mark.parametrize("status,expected", [("running", "pending"), ("completed", "partial")])
def test_missing_period_is_pending_only_while_its_run_can_still_produce_data(tmp_path, monkeypatch, status, expected):
    monkeypatch.setattr("chanlun.screening.markets.closed_bar_cutoff", lambda *_: 1000)
    result = screening.ScreeningManager(tmp_path)._freshness({
        "status": status, "market_cutoffs": {"us": {"1m": 1000, "30m": None}},
        "market_clock_symbols": {"us": "AAPL.US"},
    })
    assert result["freshness_state"] == expected
    assert result["market_freshness"]["us"]["1m"]["state"] == "cutoff_current"
    assert result["market_freshness"]["us"]["30m"]["state"] == ("pending" if status == "running" else "unknown")


def test_one_calendar_failure_and_future_saved_clock_do_not_stop_other_markets(tmp_path, monkeypatch):
    def cutoff(market, *_):
        if market == "us":
            raise OSError("calendar unavailable")
        return 1000

    monkeypatch.setattr("chanlun.screening.markets.closed_bar_cutoff", cutoff)
    result = screening.ScreeningManager(tmp_path)._freshness({
        "status": "completed", "market_cutoffs": {"us": {"5m": 1000}, "a": {"5m": 1000}, "currency_spot": {"5m": 2000}},
        "market_clock_symbols": {"us": "AAPL.US", "currency_spot": "BTC/USDT"},
    })
    assert result["freshness_state"] == "partial"
    assert result["market_freshness"]["a"]["5m"]["state"] == "cutoff_current"
    assert result["market_freshness"]["us"]["5m"]["message"] == "calendar unavailable"
    assert result["market_freshness"]["currency_spot"]["5m"]["state"] == "unknown"
    assert result["cutoff_current"] is None


def test_evidence_is_frozen_pinned_and_detects_same_size_corruption(tmp_path, monkeypatch):
    import pandas as pd
    from chanlun.screening import runner
    run_id = "e" * 32
    manager = screening.ScreeningManager(tmp_path)
    directory = tmp_path / run_id
    dates = pd.date_range("2026-09-11 14:55", periods=2, freq="5min", tz=screening.CN)
    frame = pd.DataFrame({"date": dates, "code": "SH.600000", "open": 12., "high": 13., "low": 11., "close": 12., "volume": 100})
    cutoff = int(dates[-1].timestamp())
    point = {"point_id": "P", "available_at": cutoff}
    snapshot = {"symbol": "SH.600000", "source_frequency": "5m", "source_closed_at": cutoff,
                "levels": [{"structural_level": 0, "points": [point]}]}
    manifest = runner._save_evidence(directory, "SH.600000", "5m", frame, snapshot)
    runner.write_json(tmp_path / "latest.json", {"run_id": run_id})
    runner.write_json(directory / "status.json", {"run_id": run_id, "status": "completed", "completed": 1,
                                                "cutoffs": {"5m": cutoff}, "source_revision": runner.source_revision()})
    record = {"code": "SH.600000", "rows": [{"code": "SH.600000", "frequency": "5m", "source_closed_at": cutoff,
        "selected": [{"point": point}], "evidence": manifest, "recent_rejections": [], "reason_counts": {}}]}
    (directory / "results.jsonl").write_text(json.dumps(record) + "\n", encoding="utf-8")
    monkeypatch.setattr(runner, "snapshot_for", Mock(side_effect=AssertionError("Evidence view recomputed structure")))
    data = manager.evidence(run_id, "SH.600000", "5m", "P")
    assert data["snapshot"] == snapshot and data["metadata"]["verified"]
    assert data["bars"][-1] == [cutoff, 12., 13., 11., 12., 100.]
    for identity in (("f" * 32, "SH.600000", "5m", "P"), (run_id, "SZ.000001", "5m", "P"), (run_id, "SH.600000", "30m", "P")):
        with pytest.raises(ValueError):
            manager.evidence(*identity)
    version = manager.status()["evidence_version"]
    path = directory / "evidence/SH.600000_5m.json.gz"
    damaged = bytearray(path.read_bytes())
    damaged[len(damaged)//2] ^= 1
    path.write_bytes(damaged)
    assert manager.status()["evidence_version"] != version
    result = manager.results()
    assert not result["selected"] and result["errors"][0]["reasons"] == ["EVIDENCE_MISSING"]
    with pytest.raises(ValueError):
        manager.evidence(run_id, "SH.600000", "5m", "P")


@pytest.mark.parametrize("errors", [[], ["ENGINE_ERROR"]])
def test_reader_does_not_serve_candidates_with_errors_or_missing_evidence(tmp_path, errors):
    run_id = "a" * 32
    screening.write_json(tmp_path / "latest.json", {"run_id": run_id})
    screening.write_json(tmp_path / run_id / "status.json", {"run_id": run_id, "status": "completed",
                                                          "source_revision": screening.source_revision()})
    row = {"code": "SH.600000", "rows": [{"code": "SH.600000", "frequency": "5m",
           "selected": [{"point": {"available_at": 100}}], "recent_rejections": [], "reason_counts": {}, "data_errors": errors}]}
    (tmp_path / run_id / "results.jsonl").write_text(json.dumps(row) + "\n", encoding="utf-8")
    result = screening.ScreeningManager(tmp_path).results()
    assert not result["selected"]
    assert result["errors"][0]["reasons"] == (errors or ["EVIDENCE_MISSING"])
