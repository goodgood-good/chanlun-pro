"""Restore the workbench without restarting scans or mixing old evidence."""

import json
import gzip
import time
from unittest.mock import Mock

import pytest

from cl_app import create_app
from cl_app.services import screening, screening_workbench as workbench


@pytest.fixture
def fixture_run(tmp_path, monkeypatch):
    run_id = "a" * 32
    manager = screening.ScreeningManager(tmp_path / "screening")
    monkeypatch.setattr(workbench, "manager", manager)
    monkeypatch.setattr(workbench, "_archive_root", lambda: tmp_path / "archive")
    directory = manager.root / run_id
    screening.write_json(manager.root / "latest.json", {"run_id": run_id})
    screening.write_json(directory / "status.json", {
        "run_id": run_id, "status": "completed", "completed": 1, "total": 1,
    })
    item = {"code": "SH.600088", "rows": [{
        "code": "SH.600088", "name": "样例", "frequency": "5m",
        "selected": [{"point": {"point_id": "point", "point_type": "3buy", "center_ordinal": 1, "available_at": 100,
                                  "anchor_price": 10, "anchor_tick": 10, "anchor_at": 90,
                                  "anchor_unit_id": "return", "side": "buy", "status": "confirmed",
                                  "confirmed_at": 100, "structural_level": 0, "source_kind": "segment"}, "center": None}],
        "recent_rejections": [], "reason_counts": {},
    }]}
    (directory / "results.jsonl").write_text(json.dumps(item) + "\n", encoding="utf-8")
    (directory / "evidence").mkdir()
    (directory / "evidence/SH.600088_5m.parquet").write_bytes(b"fixture")
    snapshot = {"symbol": "SH.600088", "source_frequency": "5m", "source_closed_at": 130,
                "structure_price_quantum": "1", "levels": [{"structural_level": 0,
                    "points": [item["rows"][0]["selected"][0]["point"]], "centers": [],
                }], "chart": {"xds": [
                    {"points": [{"time": 80, "price": 12}, {"time": 90, "price": 10}], "locked": True, "state": "locked"},
                    {"points": [{"time": 90, "price": 10}, {"time": 120, "price": 12}], "locked": False, "state": "forming"},
                ]}}
    (directory / "evidence/SH.600088_5m.json.gz").write_bytes(gzip.compress(json.dumps(snapshot).encode()))
    return manager, run_id, tmp_path / "archive"


@pytest.mark.parametrize("point_type", ["1buy", "3buy"])
@pytest.mark.parametrize("damage", ["truncated", "invalid_deflate", "invalid_shape"])
def test_one_damaged_legacy_snapshot_does_not_break_workbench(fixture_run, monkeypatch, point_type, damage):
    from copy import deepcopy
    manager, source, _ = fixture_run
    directory = manager.root / source
    records = directory / "results.jsonl"
    good = json.loads(records.read_text(encoding="utf-8"))
    broken = deepcopy(good)
    broken["code"] = "SZ.000001"
    row = broken["rows"][0]
    row["code"] = broken["code"]
    row["selected"][0]["point"].update(point_id="broken", point_type=point_type, divergence={"kind": "trend"})
    snapshot = {"symbol": broken["code"], "source_frequency": "5m", "levels": []}
    raw = gzip.compress(json.dumps(snapshot).encode())
    if damage == "truncated":
        raw = raw[:-4]
    elif damage == "invalid_deflate":
        raw = b"\x1f\x8b\x08\x00" + b"\x00" * 6 + b"\x07" + b"\x00" * 8
    else:
        snapshot["levels"] = [None]
        raw = gzip.compress(json.dumps(snapshot).encode())
    (directory / "evidence/SZ.000001_5m.parquet").write_bytes(b"fixture")
    packed = directory / "evidence/SZ.000001_5m.json.gz"
    packed.write_bytes(raw)
    # Old evidence stored file sizes, without a checksum. Both point paths
    # must reject damaged gzip/structure, not crash or publish the bad stock.
    row["evidence"] = {"snapshot_bytes": len(raw), "parquet_bytes": 7}
    records.write_text(json.dumps(good) + "\n" + json.dumps(broken) + "\n", encoding="utf-8")
    screening.write_json(directory / "status.json", {"run_id": source, "status": "completed", "completed": 2, "total": 2})
    manager.start = Mock(side_effect=AssertionError("read must not scan"))
    monkeypatch.setattr("chanlun.screening.runner.snapshot_for", Mock(side_effect=AssertionError("read must not calculate")))
    app = create_app(test_config={"TESTING": True, "LOGIN_DISABLED": True, "VALIDATE_WEB_SECURITY": False})
    try:
        response = app.test_client().get("/screening/workbench")
        if response.status_code == 202:
            manager.results()
            response = app.test_client().get("/screening/workbench")
    finally:
        app.extensions["shutdown_runtime_services"]()
    assert response.status_code == 200
    result = response.get_json()
    assert {p["code"] for p in result["candidates"]} == {"SH.600088"}
    assert (result["diagnostics"]["errors"] or result["status"]["semantic_exclusion_count"]
            or result["status"]["lifetime_exclusion_count"])
    assert packed.read_bytes() == raw
    manager.start.assert_not_called()


@pytest.mark.parametrize("ordinal,expected", [(1, 1), (2, 0), (None, 0)])
def test_saved_selection_applies_first_center_preference_without_changing_confirmation(fixture_run, ordinal, expected):
    manager, source, _ = fixture_run
    path = manager.root / source / "results.jsonl"
    record = json.loads(path.read_text(encoding="utf-8"))
    point = record["rows"][0]["selected"][0]["point"]
    point["center_ordinal"] = ordinal
    path.write_text(json.dumps(record) + "\n", encoding="utf-8")
    packed = manager.root / source / "evidence/SH.600088_5m.json.gz"
    snapshot = json.loads(gzip.decompress(packed.read_bytes()))
    snapshot["levels"][0]["points"][0]["center_ordinal"] = ordinal
    packed.write_bytes(gzip.compress(json.dumps(snapshot).encode()))
    result = manager.results()
    assert len(result["selected"]) == expected
    assert result["selection_policy_exclusion_count"] == 1 - expected
    if not expected:
        assert result["recent_rejections"][0]["point"]["status"] == "confirmed"
    assert json.loads(path.read_text(encoding="utf-8"))["rows"][0]["selected"][0]["point"]["status"] == "confirmed"


def test_dashboard_reads_current_run_without_starting_scan_or_promoting_sector_history(fixture_run):
    manager, run_id, archive = fixture_run
    screening.write_json(archive / "trading_screening_sector_snapshot.json", {"payload": {
        "captured_at": "2026-09-07T12:00:00+08:00", "snapshot": {
            "members": {"sector": ["SH.600088"]}, "assessments": {"assessments": [{
                "sector_id": "sector", "sector_name": "示例板块", "regime": "up", "horizontal_rank": 1,
            }]},
        },
    }})
    manager.start = Mock(side_effect=AssertionError("read launched scan"))
    before = {p: p.stat().st_mtime_ns for p in manager.root.rglob("*") if p.is_file()}
    data = workbench.dashboard()
    assert data["source"] == run_id
    assert len(data["candidates"]) == 1
    assert data["candidates"][0]["sectors"] == ["sector"]
    assert data["sectors"][0]["signal_count"] == 1
    assert data["sectors"][0]["historical_regime"] == "up"
    assert data["sector_catalog_at"].startswith("2026-09-07")
    assert "current_regime" not in data["sectors"][0]
    assert before == {p: p.stat().st_mtime_ns for p in manager.root.rglob("*") if p.is_file()}
    manager.start.assert_not_called()


def test_workbench_keeps_point_age_available_for_user_filters():
    rows = workbench._current_rows({"selected": [{"code": "SH.600088", "market": "a", "frequency": "5m",
        "point": {"point_id": "point", "point_type": "3buy", "status": "confirmed", "available_at": 200},
        "anchor_age_sessions": 3, "confirmation_age_sessions": 1,
        "distance_from_anchor_pct": 2.5}]}, "run")
    assert rows[0]["anchor_age_sessions"] == 3
    assert rows[0]["confirmation_age_sessions"] == 1
    assert rows[0]["gain_pct"] == 2.5


@pytest.mark.parametrize("state,expected", [("forming", 1), ("formed", 1), ("locked", 0)])
def test_saved_results_apply_confirmation_lifetime_without_scanning(fixture_run, monkeypatch, state, expected):
    manager, source, _ = fixture_run
    packed = manager.root / source / "evidence/SH.600088_5m.json.gz"
    # Prime the reader to also verify that a changed file invalidates its cache.
    assert len(manager.results()["selected"]) == 1
    snapshot = json.loads(gzip.decompress(packed.read_bytes()))
    snapshot["chart"]["xds"][-1].update(state=state, locked=state == "locked", linestyle="0")
    raw = gzip.compress(json.dumps(snapshot).encode())
    packed.write_bytes(raw)
    manager.start = Mock(side_effect=AssertionError("read must not scan"))
    monkeypatch.setattr("chanlun.screening.runner.snapshot_for", Mock(side_effect=AssertionError("read must not calculate")))
    data = workbench.dashboard()
    assert len(data["candidates"]) == expected
    assert data["status"]["lifetime_exclusion_count"] == 1 - expected
    if not expected:
        result = manager.results()
        assert result["rejection_counts"]["CONFIRMING_SEGMENT_COMPLETED"] == 1
        assert result["recent_rejections"][0]["point"]["status"] == "confirmed"
    assert packed.read_bytes() == raw
    manager.start.assert_not_called()


def test_saved_result_missing_confirmation_proof_is_not_published(fixture_run):
    manager, source, _ = fixture_run
    packed = manager.root / source / "evidence/SH.600088_5m.json.gz"
    snapshot = json.loads(gzip.decompress(packed.read_bytes()))
    snapshot.pop("chart")
    packed.write_bytes(gzip.compress(json.dumps(snapshot).encode()))
    result = manager.results()
    assert not result["selected"]
    assert result["rejection_counts"]["CONFIRMING_SEGMENT_MISSING"] == 1


def test_legacy_results_recheck_divergence_and_parent_meaning_without_rewriting(fixture_run):
    import gzip
    from copy import deepcopy
    manager, run_id, _ = fixture_run
    directory = manager.root / run_id
    path = directory / "results.jsonl"
    stock = json.loads(path.read_text(encoding="utf-8"))
    row = stock["rows"][0]
    first = {"point_id": "first", "point_type": "1buy", "side": "buy", "status": "confirmed",
             "confirmed_at": 100, "anchor_at": 90, "available_at": 100, "anchor_tick": 10,
             "structural_level": 0, "source_kind": "segment", "price_basis_revision": "basis", "center_id": "Z",
             "anchor_unit_id": "first-signal",
             "evidence_codes": ["two_separated_centers"],
             "divergence": {"kind": "trend", "structural_level": 0, "price_basis_revision": "basis",
                            "direction": "down", "signal_leg_unit_ids": ["leave", "return", "end"],
                            "metrics": {"is_divergent": True}}}
    weak = {**first, "point_id": "weak-second", "point_type": "2buy", "parent_point_id": "first",
            "variant": "weak_divergence", "anchor_at": 110, "anchor_tick": 9, "available_at": 120,
            "confirmed_at": 120, "anchor_unit_id": "pullback",
            "divergence": {"kind": "consolidation", "metrics": {"is_divergent": True}}}
    bad_first = deepcopy(first)
    bad_first.update(point_id="consolidation-first")
    bad_first["divergence"]["kind"] = "consolidation"
    bad_second = {**weak, "point_id": "consolidation-derived-second", "parent_point_id": bad_first["point_id"]}
    waiting = {**weak, "point_id": "waiting", "parent_point_id": "absent", "status": "approaching",
               "confirmed_at": None, "missing_conditions": ["terminal_unit_locked"]}
    row["selected"].extend({"point": p} for p in (first, weak, bad_first, bad_second))
    row["recent_rejections"] = [{"point": waiting, "reasons": ["NOT_CONFIRMED"]}]
    center = {"center_id": "Z", "third_class_confirmed": True,
              "completion_direction": "down", "completion_return_unit_id": "return"}
    snapshot = json.loads(gzip.decompress((directory / "evidence/SH.600088_5m.json.gz").read_bytes()))
    snapshot["levels"][0].update(points=[row["selected"][0]["point"], first, weak, bad_first, bad_second], centers=[center])
    snapshot["chart"]["xds"] = [
        {"points": [{"time": 80, "price": 12}, {"time": 90, "price": 10}], "locked": True, "state": "locked"},
        {"points": [{"time": 90, "price": 10}, {"time": 105, "price": 12}], "locked": True, "state": "locked"},
        {"points": [{"time": 105, "price": 12}, {"time": 110, "price": 9}], "locked": True, "state": "locked"},
        {"points": [{"time": 110, "price": 9}, {"time": 125, "price": 11}], "locked": False, "state": "forming"},
    ]
    packed = directory / "evidence" / "SH.600088_5m.json.gz"
    packed.write_bytes(gzip.compress(json.dumps(snapshot).encode()))
    path.write_text(json.dumps(stock) + "\n", encoding="utf-8")
    before = {p: p.read_bytes() for p in (path, packed)}
    manager.start = Mock(side_effect=AssertionError("read must not scan"))
    result = manager.results()
    # The first buy remains valid parent evidence, but its rebound has already
    # completed to form this second buy and cannot remain a current candidate.
    assert {p["point"]["point_id"] for p in result["selected"]} == {"weak-second"}
    assert not result["observations"]
    assert result["semantic_exclusion_count"] == 3
    assert result["rejection_counts"]["SECOND_PARENT_MISSING"] == 1
    assert before == {p: p.read_bytes() for p in before}
    manager.start.assert_not_called()
    # A changed file must invalidate cached lineage, including on the same run.
    first["divergence"]["kind"] = "consolidation"
    packed.write_bytes(gzip.compress(json.dumps(snapshot).encode()))
    assert "weak-second" not in {p["point"]["point_id"] for p in manager.results()["selected"]}


def test_legacy_waiting_records_are_visible_but_not_promoted_or_rebuilt(fixture_run):
    from copy import deepcopy
    manager, run_id, _ = fixture_run
    path = manager.root / run_id / "results.jsonl"
    stock = json.loads(path.read_text(encoding="utf-8"))
    row = stock["rows"][0]
    point = {**row["selected"][0]["point"], "point_id": "waiting", "status": "approaching",
             "confirmed_at": None, "missing_conditions": ["terminal_unit_locked"],
             "source_kind": "segment", "structural_level": 0, "side": "buy"}
    row["recent_rejections"] = [{"point": point, "reasons": ["NOT_CONFIRMED"]}]
    other = deepcopy(row)
    other.update(code="SZ.000001", selected=[], data_errors=["DATA_GAPS"])
    stock["rows"].append(other)
    for reason in ("INVALIDATED", "ANCHOR_BROKEN", "PRICE_TOO_FAR", "OLD_ANCHOR", "LATER_SELL", "STRUCTURE_MISMATCH"):
        row["recent_rejections"].append({"point": {**point, "point_id": reason}, "reasons": ["NOT_CONFIRMED", reason]})
    path.write_text(json.dumps(stock) + "\n", encoding="utf-8")
    manager.start = Mock(side_effect=AssertionError("view must not scan"))
    before = path.read_bytes()
    data = workbench.dashboard()
    assert data["stage_counts"] == {"confirmed": 1, "approaching": 1}
    waiting = data["candidates"][1]
    assert waiting["observation_validation"] == "legacy_not_rechecked"
    assert not waiting["audit"] and not waiting["evidence_chart_available"]
    assert path.read_bytes() == before
    # Observations retain the same run-bound manual review workflow.
    body = {"source": run_id, "candidate_id": waiting["id"],
            "judgements": {k: v[0] for k, v in workbench.REVIEW_FIELDS.items()}, "notes": "等待末段完成"}
    assert workbench.save_review("observer", body)["candidate_id"] == waiting["id"]
    manager.start.assert_not_called()


def test_new_empty_observations_do_not_fall_back_to_old_rejection_records(fixture_run):
    manager, run_id, _ = fixture_run
    path = manager.root / run_id / "results.jsonl"
    stock = json.loads(path.read_text(encoding="utf-8"))
    stock["rows"][0].update(observations=[], recent_rejections=[{"point": {
        "point_id": "waiting", "point_type": "3buy", "side": "buy", "status": "approaching",
        "source_kind": "segment", "structural_level": 0, "missing_conditions": ["lock"],
    }, "reasons": ["NOT_CONFIRMED"]}])
    path.write_text(json.dumps(stock) + "\n", encoding="utf-8")
    assert workbench.dashboard()["stage_counts"] == {"confirmed": 1}


@pytest.mark.parametrize("status", ["starting", "running", "completed", "failed", "cancelled"])
def test_latest_empty_run_never_falls_back_to_older_candidates(fixture_run, status):
    manager, old_id, archive = fixture_run
    latest_id = "b" * 32
    # An obsolete snapshot must not even be parsed on the latest-results path.
    archive.mkdir(parents=True)
    (archive / "trading_screening_snapshot.json").write_text("invalid obsolete data", encoding="utf-8")
    assert len(workbench.dashboard()["candidates"]) == 1
    screening.write_json(manager.root / latest_id / "status.json", {
        "run_id": latest_id, "status": status, "completed": 0, "total": 1,
        "updated_at": time.time(),
    })
    screening.write_json(manager.root / "latest.json", {"run_id": latest_id})
    data = workbench.dashboard()
    assert data["source"] == latest_id
    assert data["status"]["status"] == status
    assert data["candidates"] == []
    assert data["point_counts"] == {}
    assert (manager.root / old_id / "results.jsonl").is_file()


def test_review_survives_reload_is_owned_and_pinned_to_actual_candidate(fixture_run):
    _, run_id, _ = fixture_run
    data = workbench.dashboard()
    body = {"source": run_id, "candidate_id": data["candidates"][0]["id"],
            "judgements": {key: values[0] for key, values in workbench.REVIEW_FIELDS.items()},
            "notes": "<script>仅作为文本</script>\n中枢触边需观察"}
    saved = workbench.save_review("user-one", body)
    assert workbench.reviews("user-one", run_id)["latest"][body["candidate_id"]] == saved
    assert workbench.reviews("user-two", run_id)["history"] == []
    assert workbench.reviews("user-one", "b" * 32)["history"] == []
    with pytest.raises(ValueError, match="候选"):
        workbench.save_review("user-one", {**body, "candidate_id": "unrelated"})
    with pytest.raises(ValueError):
        workbench.save_review("user-one", {**body, "notes": "a" * 4001})
    with pytest.raises(ValueError):
        workbench.save_review("user-one", {**body, "judgements": {}})
    assert len(workbench.reviews("user-one", run_id)["history"]) == 1


def test_new_run_does_not_expose_or_accept_previous_run_reviews(fixture_run):
    manager, old_id, _ = fixture_run
    old = workbench.dashboard()
    body = {"source": old_id, "candidate_id": old["candidates"][0]["id"],
            "judgements": {key: values[0] for key, values in workbench.REVIEW_FIELDS.items()},
            "notes": "旧批次的判断"}
    workbench.save_review("user-one", body)
    latest_id = "b" * 32
    screening.write_json(manager.root / latest_id / "status.json", {
        "run_id": latest_id, "status": "completed", "completed": 1, "total": 1,
    })
    (manager.root / latest_id / "results.jsonl").write_bytes(
        (manager.root / old_id / "results.jsonl").read_bytes()
    )
    screening.write_json(manager.root / "latest.json", {"run_id": latest_id})
    latest = workbench.dashboard()
    # The new run must carry its own complete evidence before it is readable.
    old_evidence = manager.root / old_id / "evidence"
    new_evidence = manager.root / latest["source"] / "evidence"
    new_evidence.mkdir(exist_ok=True)
    for file in old_evidence.iterdir():
        (new_evidence / file.name).write_bytes(file.read_bytes())
    latest = workbench.dashboard()
    assert latest["candidates"][0]["id"] != old["candidates"][0]["id"]
    assert workbench.reviews("user-one", old_id)["history"] == []
    assert workbench.reviews("user-one", latest_id)["history"] == []
    with pytest.raises(ValueError, match="本次快照"):
        workbench.save_review("user-one", body)


def test_restored_routes_require_login_and_writes_keep_csrf(fixture_run):
    app = create_app(test_config={"TESTING": True, "VALIDATE_WEB_SECURITY": False})
    client = app.test_client()
    for route in ("/screening", "/decision-support/early-screening", "/early-screening",
                  "/screening/tasks", "/xuangu/task_list/a", "/screening/workbench",
                  "/screening/reviews", "/screening/notifications"):
        assert client.get(route).status_code == 302, route
    assert client.post("/screening/reviews", json={}).status_code in (400, 401)
    app.config["LOGIN_DISABLED"] = True
    assert client.post("/screening/reviews", json={}).status_code == 400
    html = client.get("/decision-support/early-screening").get_data(as_text=True)
    for module in ("板块结构雷达", "买卖点与关注队列", "人工确认清单", "chart-layout"):
        assert module in html
    assert 'id="run-source"' not in html
    assert "结果来源" not in html
    assert 'data-tab=' not in html
    assert 'id="task-frame"' not in html
    assert 'href="/screening/tasks"' not in html
    assert 'id="screen-form"' in html
    assert 'form="screen-form"' in html
    assert 'href="#review-panel"' in html
    for old_page in ("/screening/tasks", "/xuangu/task_list/a"):
        redirect = client.get(old_page)
        assert redirect.status_code == 302
        assert redirect.headers["Location"] == "/screening"
    assert client.get("/screening/history").status_code == 404
    for source in ("legacy", "a" * 32, "../status"):
        response = client.get("/screening/workbench", query_string={"source": source})
        assert response.status_code == 400
        assert "仅支持最新选股结果" in response.get_json()["error"]
    response = client.get("/screening/workbench?source=latest")
    assert response.status_code in (200, 202)
    fixture_run[0].results()
    assert client.get("/screening/workbench?source=latest").status_code == 200
    assert client.get("/screening/workbench").status_code == 200
    assert client.get("/screening/notifications").get_json()["automatic_notifications"] is False


def test_notification_history_keeps_original_dates_and_does_not_send(fixture_run):
    _, _, archive = fixture_run
    screening.write_json(archive / "trading_notification_state.json", {"event_audit": [{
        "code": "SH.600088", "status": "suppressed", "recorded_at": "2026-09-01T10:00:00+08:00",
    }]})
    result = workbench.notification_history()
    assert result["mode"] == "historical"
    assert result["automatic_notifications"] is False
    assert result["events"][0]["recorded_at"].startswith("2026-09-01")
