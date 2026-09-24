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
    screening.write_json(directory/"status.json", {"run_id":run_id,"status":"completed"})
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
    screening.write_json(directory / "status.json", {"run_id": run_id, "status": "completed", "completed": 1})
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
    screening.write_json(directory / "status.json", {"run_id": run_id, "status": "completed", "completed": 1})
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
    assert screening.ScreeningManager(tmp_path).status()["source_current"] is False


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
    runner.write_json(directory / "status.json", {"run_id": run_id, "status": "completed", "completed": 1, "cutoffs": {"5m": cutoff}})
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
    screening.write_json(tmp_path / run_id / "status.json", {"run_id": run_id, "status": "completed"})
    row = {"code": "SH.600000", "rows": [{"code": "SH.600000", "frequency": "5m",
           "selected": [{"point": {"available_at": 100}}], "recent_rejections": [], "reason_counts": {}, "data_errors": errors}]}
    (tmp_path / run_id / "results.jsonl").write_text(json.dumps(row) + "\n", encoding="utf-8")
    result = screening.ScreeningManager(tmp_path).results()
    assert not result["selected"]
    assert result["errors"][0]["reasons"] == (errors or ["EVIDENCE_MISSING"])
