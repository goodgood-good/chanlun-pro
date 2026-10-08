"""False-positive boundaries that matter for current, usable screening signals."""

from copy import deepcopy
from datetime import datetime

import pandas as pd
import pytest

from chanlun.screening.rules import CN, _price_ticks, audit_snapshot, trading_context, validate_frame
from chanlun.screening.runner import _review_candidates, is_a_share, source_revision


@pytest.fixture(autouse=True)
def isolate_suspension_feed(monkeypatch):
    # Network/calendar integration has dedicated tests. Rule tests must never
    # consult the user's live feed cache or make anonymous HTTP requests.
    monkeypatch.setattr("chanlun.screening.runner.suspension_evidence", lambda *_: {"intervals": []})


@pytest.fixture
def sample():
    context = trading_context(datetime(2026, 9, 13, 2, tzinfo=CN), "5m")
    times = context["expected_closes"]
    frame = pd.DataFrame({"date": pd.to_datetime(times, unit="s", utc=True).tz_convert(CN),
                          "open": 12.0, "high": 13.0, "low": 12.0, "close": 12.5, "volume": 100})
    prices = [12, 9, 12, 10, 11, 9]
    roles = [{"unit_id": f"S{i}", "start_tick": a, "end_tick": b,
              "start_time": times[-200+i], "end_time": times[-199+i], "low_tick": min(a, b), "high_tick": max(a, b),
              "direction": "up" if b > a else "down", "locked": True, "forming": False,
              "confirmed_at": times[-199+i], "available_at": times[-199+i]}
             for i, (a, b) in enumerate(zip(prices, prices[1:]))]
    leave = {"unit_id": "leave", "start_time": times[-195], "end_time": times[-120],
             "start_tick": 9, "end_tick": 13, "low_tick": 9, "high_tick": 13,
             "direction": "up", "locked": True, "forming": False,
             "confirmed_at": times[-120], "available_at": times[-120]}
    ret = {"unit_id": "return", "start_time": times[-120], "end_time": times[-100],
           "start_tick": 13, "end_tick": 12, "low_tick": 12, "high_tick": 13,
           "direction": "down", "locked": True, "forming": False,
           "confirmed_at": times[-20], "available_at": times[-20]}
    center = {"center_id": "Z", "structural_level": 0, "source_kind": "segment", "formation_rule": "five_role",
              "third_class_confirmed": True, "core": {"zd_tick": 10, "zg_tick": 11},
              "establishment_segments": roles, "entry_unit_id": "S0", "core_unit_ids": ["S1", "S2", "S3"],
              "establishment_leave_unit_id": "S4",
              "completion_leave_unit_id": "leave", "completion_return_unit_id": "return",
              "lifecycle_leaving_segment": leave, "completion_return_segment": ret}
    point = {"point_id": "P", "point_type": "3buy", "side": "buy", "status": "confirmed",
             "confirmed_at": times[-20], "available_at": times[-20], "anchor_at": times[-100],
             "anchor_tick": 12, "invalidation_tick": 11, "center_id": "Z", "anchor_unit_id": "return",
             "dependency_from": times[-200],
             "source_kind": "segment", "structural_level": 0, "price_basis_revision": "basis"}
    snapshot = {"snapshot_revision": "fixture", "source_closed_at": times[-1], "source_frequency": "5m", "symbol": "SH.600000",
                "structure_price_quantum": "1", "price_basis_revision": "basis",
                "levels": [{"structural_level": 0, "centers": [center], "points": [point]}]}
    snapshot["screening_segments"] = [
        {"unit_id": "return", "start_time": times[-120], "end_time": times[-100],
         "start_tick": 13, "end_tick": 12, "direction": "down", "locked": True,
         "forming": False, "confirmed_at": times[-20]},
        {"unit_id": "confirmation", "start_time": times[-100], "end_time": times[-5],
         "start_tick": 12, "end_tick": 13, "direction": "up", "locked": False,
         "forming": True, "confirmed_at": None},
    ]
    return frame, snapshot, context


def first_buy_proof(snapshot):
    """Supply real signal meaning for tests otherwise concerned with prices/age."""
    point = snapshot["levels"][0]["points"][0]
    center = snapshot["levels"][0]["centers"][0]
    center.update(completion_direction="down", completion_return_unit_id="return")
    center.update(boundary_divergence_id="divergence", boundary_anchor_unit_id="end")
    center.setdefault("price_basis_revision", point["price_basis_revision"])
    center.setdefault("established_at", center["establishment_segments"][3]["available_at"])
    point.update(point_type="1buy", variant="standard", evidence_codes=["two_separated_centers"],
                  divergence={"kind": "trend", "direction": "down", "structural_level": 0,
                              "divergence_id": "divergence", "signal_unit_id": "end",
                              "source_kind": point["source_kind"],
                              "price_basis_revision": "basis",
                              "comparison_center_id": center["center_id"],
                              "comparison_center_established_at": center["established_at"],
                              "comparison_center_known_at": center["established_at"],
                              "available_at": point["available_at"],
                              "signal_leg_unit_ids": ["leave", "return", "end"],
                              "metrics": {"is_divergent": True}})
    return point


def second_buy_proof(snapshot, *, weak=False):
    point = first_buy_proof(snapshot)
    parent = deepcopy(point)
    parent.update(point_id="parent", anchor_at=point["anchor_at"] - 300)
    if weak:
        parent["anchor_tick"] = point["anchor_tick"] + 1
    snapshot["levels"][0]["points"].append(parent)
    point.update(point_type="2buy", parent_point_id="parent",
                 variant="weak_divergence" if weak else "strict",
                 divergence={"kind": "consolidation", "metrics": {"is_divergent": True}} if weak else None)
    return point, parent


def test_intact_native_buy_is_selected(sample):
    frame, snapshot, context = sample
    result = audit_snapshot(snapshot, frame, context)
    assert len(result["selected"]) == 1
    assert not result["data_errors"]


@pytest.mark.parametrize("fault", [
    "missing_core_clock", "future_core_clock", "unfinished_leave",
    "future_return_clock", "disconnected_leave", "wrong_return_anchor",
    "member_time_gap", "leave_time_gap", "return_time_gap",
    "stroke_observation_source",
])
def test_legacy_five_role_third_requires_causal_completed_members(sample, fault):
    from chanlun.screening.rules import _valid_center_roles

    frame, snapshot, context = sample
    center = snapshot["levels"][0]["centers"][0]
    point = snapshot["levels"][0]["points"][0]
    assert _valid_center_roles(center, point)
    if fault == "missing_core_clock":
        del center["establishment_segments"][2]["confirmed_at"]
    elif fault == "future_core_clock":
        center["establishment_segments"][2]["available_at"] = point["available_at"] + 300
    elif fault == "unfinished_leave":
        center["lifecycle_leaving_segment"]["forming"] = True
    elif fault == "future_return_clock":
        center["completion_return_segment"]["confirmed_at"] = point["confirmed_at"] + 300
        center["completion_return_segment"]["available_at"] = point["confirmed_at"] + 300
    elif fault == "disconnected_leave":
        center["lifecycle_leaving_segment"]["end_tick"] -= 1
    elif fault == "wrong_return_anchor":
        point["anchor_at"] = center["completion_return_segment"]["start_time"] - 300
    elif fault == "member_time_gap":
        center["establishment_segments"][2]["start_time"] += 60
    elif fault == "leave_time_gap":
        center["lifecycle_leaving_segment"]["start_time"] += 60
    elif fault == "return_time_gap":
        center["completion_return_segment"]["start_time"] += 60
    else:
        point["source_kind"] = center["source_kind"] = "stroke_observation"
    result = audit_snapshot(snapshot, frame, context)
    assert not result["selected"]
    assert "STRUCTURE_MISMATCH" in result["rejected"][0]["reasons"]
    assert not result["data_errors"]


@pytest.mark.parametrize("fault", ["missing_completion_clock", "member_time_gap"])
def test_legacy_five_role_frozen_result_loses_selection_when_member_proof_breaks(sample, tmp_path, fault):
    import gzip
    import hashlib
    import json
    from chanlun.screening.runner import _save_evidence, write_json
    from cl_app.services.screening import ScreeningManager

    frame, snapshot, context = sample
    snapshot["levels"][0]["points"][0]["center_ordinal"] = 1
    selected, = audit_snapshot(snapshot, frame, context)["selected"]
    run_id = "c" * 32
    directory = tmp_path / run_id
    manifest = _save_evidence(directory, "SH.600000", "5m", frame, snapshot)
    record = {"code": "SH.600000", "rows": [{"code": "SH.600000", "frequency": "5m",
              "source_closed_at": context["cutoff"], "selected": [selected],
              "observations": [], "recent_rejections": [], "reason_counts": {},
              "evidence": manifest}]}
    write_json(tmp_path / "latest.json", {"run_id": run_id})
    write_json(directory / "status.json", {"run_id": run_id, "status": "completed", "completed": 1,
                                           "source_revision": source_revision()})
    record_path = directory / "results.jsonl"
    record_path.write_text(json.dumps(record) + "\n", encoding="utf-8")
    manager = ScreeningManager(tmp_path)
    assert [item["point"]["point_id"] for item in manager.results()["selected"]] == ["P"]

    member = snapshot["levels"][0]["centers"][0]["establishment_segments"][1]
    if fault == "missing_completion_clock":
        del member["available_at"]
    else:
        member["start_time"] += 60
    raw = gzip.compress(json.dumps(snapshot).encode())
    (directory / "evidence/SH.600000_5m.json.gz").write_bytes(raw)
    manifest.update(snapshot_sha256=hashlib.sha256(raw).hexdigest(), snapshot_bytes=len(raw))
    record_path.write_text(json.dumps(record) + "\n", encoding="utf-8")
    published = manager.results()
    assert not published["selected"]
    assert "STRUCTURE_MISMATCH" in published["recent_rejections"][-1]["reasons"]


@pytest.mark.parametrize('keep_old_projection', [False, True])
def test_unresolved_tail_cannot_keep_a_confirmed_point_in_the_selection_window(sample, keep_old_projection):
    frame, snapshot, context = sample
    anchor = snapshot['screening_segments'][0]
    snapshot['segment_construction'] = {'status': 'unresolved', 'tail': {
        'start_time': anchor['end_time'], 'start_price': anchor['end_tick'], 'direction': 'up',
        'available_at': snapshot['source_closed_at'], 'observed_through': snapshot['source_closed_at'],
        'pen_count': 283, 'reason': 'initial-extreme-too-short', 'end_confirmed': False}}
    if not keep_old_projection:
        snapshot['screening_segments'] = [anchor]
    result = audit_snapshot(snapshot, frame, context)
    assert result['selected'] == [] and result['observations'] == []
    rejected, = result['rejected']
    assert 'CONFIRMING_SEGMENT_UNRESOLVED' in rejected['reasons']
    assert rejected['confirmation_segment']['state'] == 'unresolved'


def _mirror_signal(value):
    if isinstance(value, list):
        return [_mirror_signal(item) for item in value]
    if not isinstance(value, dict):
        return value
    result = {k: _mirror_signal(v) for k, v in value.items()}
    for key in ("start_tick", "end_tick", "anchor_tick", "invalidation_tick"):
        if key in value:
            result[key] = 30 - value[key]
    for low, high in (("low_tick", "high_tick"), ("zd_tick", "zg_tick")):
        result.pop(low, None)
        result.pop(high, None)
        if low in value:
            result[high] = 30 - value[low]
        if high in value:
            result[low] = 30 - value[high]
    if "direction" in value:
        result["direction"] = {"up": "down", "down": "up"}[value["direction"]]
    if "completion_direction" in value:
        result["completion_direction"] = {"up": "down", "down": "up"}[value["completion_direction"]]
    if "point_type" in value:
        result["point_type"] = value["point_type"].replace("buy", "sell")
    if "side" in value:
        result["side"] = "sell"
    return result


@pytest.mark.parametrize("kind", ["1buy", "2buy", "3buy"])
def test_sells_use_symmetric_price_and_confirmation_lifetime(sample, kind):
    frame, snapshot, context = sample
    if kind == "1buy":
        first_buy_proof(snapshot)
    elif kind == "2buy":
        second_buy_proof(snapshot)
    sell = kind.replace("buy", "sell")
    mirrored = _mirror_signal(snapshot)
    frame[["open", "high", "low", "close"]] = 30 - frame[["open", "low", "high", "close"]].to_numpy()
    assert len(audit_snapshot(mirrored, frame, context, [sell])["selected"]) == 1
    mirrored["screening_segments"][1].update(locked=True, forming=False, confirmed_at=context["cutoff"])
    result = audit_snapshot(mirrored, frame, context, [sell])
    assert not result["selected"]
    assert "CONFIRMING_SEGMENT_COMPLETED" in result["rejected"][0]["reasons"]
    assert len(audit_snapshot(mirrored, frame, context, [sell], as_confirmation=True)["selected"]) == 1
    frame.loc[frame.index[-1], "high"] = 19
    result = audit_snapshot(mirrored, frame, context, [sell], as_confirmation=True)
    assert not result["selected"]
    assert set(result["rejected"][0]["reasons"]) & {"INVALIDATED", "ANCHOR_BROKEN"}


@pytest.fixture
def nested_sample(sample):
    from chanlun.screening.nesting import STRATEGY
    frame, main, context = sample
    # This is an eligible third-buy fixture. The result reader now requires
    # the directional center ordinal; missing metadata is tested separately.
    main["levels"][0]["points"][0]["center_ordinal"] = 1
    main["screening_segments"][0]["start_time"] = context["expected_closes"][-240]
    main["chart"] = {"xds": []}
    lower = deepcopy(main)
    lower["source_frequency"] = "1m"
    lower["snapshot_revision"] = "one-minute"
    point = lower["levels"][0]["points"][0]
    point["point_id"] = "lower"
    point["anchor_at"] -= 120
    lower["screening_segments"][0]["end_time"] = point["anchor_at"]
    lower["screening_segments"][1]["start_time"] = point["anchor_at"]
    # Its following segment has finished; it remains valid historical evidence
    # for the containing 5m turn, while 5m still controls selection expiry.
    lower["screening_segments"][1].update(locked=True, forming=False, confirmed_at=context["cutoff"])
    lower_context = trading_context(datetime.fromtimestamp(context["cutoff"], CN), "1m")
    lower_frame = pd.DataFrame({"date": pd.to_datetime(lower_context["expected_closes"], unit="s", utc=True).tz_convert(CN),
                                "open": 12.0, "high": 13.0, "low": 12.0, "close": 12.5, "volume": 100})
    lower["source_started_at"] = int(lower_frame.date.iloc[0].timestamp())
    settings = {"strategy": STRATEGY, "frequencies": ["5m"], "point_types": ["3buy"],
                "include_weak_second": False, "max_anchor_gain_pct": 10}
    return {"settings": settings, "contexts": {"5m": context, "1m": lower_context},
            "frames": {"5m": frame, "1m": lower_frame}, "snapshots": {"5m": main, "1m": lower}}


def _nested_runtime(monkeypatch, fixture):
    from chanlun.screening import runner
    monkeypatch.setattr(runner, "fetch_frame", lambda _c, f, _ctx: fixture["frames"][f].copy())

    def snapshot(frame, _code, frequency):
        result = deepcopy(fixture["snapshots"][frequency])
        cutoff = int(frame.date.iloc[-1].timestamp())
        result["source_closed_at"] = cutoff
        result["levels"][0]["points"] = [p for p in result["levels"][0]["points"] if p["available_at"] <= cutoff]
        return result

    monkeypatch.setattr(runner, "snapshot_for", snapshot)
    return runner


def test_nested_scan_publishes_only_five_minute_signals_with_two_frozen_inputs(nested_sample, monkeypatch, tmp_path):
    from chanlun.screening.nesting import saved_confirmation_valid
    from chanlun.screening.evidence import evidence_nested_snapshots
    runner = _nested_runtime(monkeypatch, nested_sample)
    result = runner.scan_symbol({"code": "SH.600000", "name": "fixture"}, nested_sample["settings"],
                                nested_sample["contexts"], tmp_path)
    assert [r["frequency"] for r in result["rows"]] == ["5m"]
    row = result["rows"][0]
    assert not row.get("data_errors"), row.get("error")
    assert len(row["selected"]) == 1, row
    selected = row["selected"][0]
    assert selected["point"]["point_id"] == "P"
    assert selected["nested_confirmation"]["point"]["point_id"] == "lower"
    assert selected["nested_confirmation"]["audit"]["confirmation_boundary"]
    assert saved_confirmation_valid(selected, *evidence_nested_snapshots(
        tmp_path, "SH.600000", row["evidence"], row["confirmation_evidence"]))


def test_direct_five_minute_point_does_not_wait_for_thirty_minute_parent(nested_sample, monkeypatch, tmp_path):
    """L107's child-period operation cannot require the eventual parent center."""
    from chanlun.screening.runner import DIRECT_STRATEGY

    runner = _nested_runtime(monkeypatch, nested_sample)
    nested_sample["frames"].pop("1m")
    nested_sample["contexts"].pop("1m")
    settings = {**nested_sample["settings"], "strategy": DIRECT_STRATEGY,
                "frequencies": ["5m"], "structural_levels": [0]}
    row = runner.scan_symbol(
        {"code": "SH.600000", "name": "fixture"}, settings,
        nested_sample["contexts"], tmp_path,
    )["rows"][0]

    assert not row.get("data_errors"), row.get("error")
    assert [candidate["point"]["point_id"] for candidate in row["selected"]] == ["P"]
    assert "30m" not in nested_sample["contexts"]
    assert "nested_confirmation" not in row["selected"][0]


def test_direct_one_minute_scan_exports_lower_witness_without_five_minute_input(
        nested_sample, monkeypatch, tmp_path):
    from chanlun.screening.runner import DIRECT_STRATEGY
    from chanlun.screening.evidence import evidence_u295_lower_structural_witnesses

    runner = _nested_runtime(monkeypatch, nested_sample)
    lower_point = nested_sample["snapshots"]["1m"]["levels"][0]["points"][0]
    nested_sample["frames"].pop("5m")
    nested_sample["contexts"].pop("5m")
    settings = {**nested_sample["settings"], "strategy": DIRECT_STRATEGY,
                "frequencies": ["1m"], "point_types": ["3buy"],
                "structural_levels": [0]}
    row = runner.scan_symbol(
        {"code": "SH.600000", "name": "fixture"}, settings,
        nested_sample["contexts"], tmp_path,
    )["rows"][0]
    assert not row.get("data_errors"), row.get("error")
    assert row["frequency"] == "1m"
    witness, = row["u295_lower_structural_witnesses"]
    assert witness["point_id"] == lower_point["point_id"]
    assert witness["status"] == "lower_formal_buy_point_only"
    assert witness["author_interval_entry_certified"] is False
    assert witness["parent_critical_price_followup_certified"] is False
    assert witness["tradable"] is False
    assert row["evidence"]["status"] == "complete"
    assert list(evidence_u295_lower_structural_witnesses(
        tmp_path, "SH.600000", row["evidence"],
        known_from=row["u295_witness_known_from"],
    )) == row["u295_lower_structural_witnesses"]
    broken = {**row["evidence"], "snapshot_sha256": "0" * 64}
    with pytest.raises(ValueError):
        evidence_u295_lower_structural_witnesses(
            tmp_path, "SH.600000", broken,
            known_from=row["u295_witness_known_from"],
        )


@pytest.mark.parametrize("fault", ["before_interval", "after_interval", "opposite", "stroke", "wrong_point_side"])
def test_unrelated_lower_signal_cannot_confirm_five_minute_point(nested_sample, fault):
    from chanlun.screening.nesting import matching_confirmations, attach_confirmation, is_nested_observation
    main, lower = nested_sample["snapshots"]["5m"], nested_sample["snapshots"]["1m"]
    point, small = main["levels"][0]["points"][0], lower["levels"][0]["points"][0]
    if fault == "before_interval":
        small["anchor_at"] = main["screening_segments"][0]["start_time"] - 300
    elif fault == "after_interval":
        small["anchor_at"] = point["anchor_at"] + 60
    elif fault == "opposite":
        small.update(side="sell", point_type="3sell")
    elif fault == "stroke":
        small["source_kind"] = "stroke_observation"
    else:
        small["point_type"] = "3sell"
    interval, matches = matching_confirmations(main, point, lower, [{"point": small}])
    assert matches == []
    combined = attach_confirmation({"point": point}, interval, None)
    assert combined["selection_status"] == "approaching"
    assert combined["point"]["status"] == "confirmed"
    assert is_nested_observation(combined)


@pytest.mark.parametrize("side", ["buy", "sell"])
@pytest.mark.parametrize("main_class", ["2", "3"])
@pytest.mark.parametrize("lower_class", ["1", "2", "3"])
def test_retest_confirmation_accepts_same_side_points_throughout_interval(
        nested_sample, side, main_class, lower_class):
    from chanlun.screening.nesting import matching_confirmations, attach_confirmation
    main, lower = nested_sample["snapshots"]["5m"], nested_sample["snapshots"]["1m"]
    point, small = main["levels"][0]["points"][0], lower["levels"][0]["points"][0]
    point.update(side=side, point_type=main_class + side)
    small.update(side=side, point_type=lower_class + side,
                 anchor_at=point["anchor_at"] - 19 * 60, anchor_tick=point["anchor_tick"] + 1,
                 dependency_from=main["screening_segments"][0]["start_time"] - 3600)
    interval, matches = matching_confirmations(main, point, lower, [{"point": small}])
    assert len(matches) == 1
    assert attach_confirmation({"point": point}, interval, matches[0])["selection_status"] == "confirmed"


def test_retest_interval_association_survives_cold_replay_and_saved_evidence_reader(nested_sample, monkeypatch, tmp_path):
    from chanlun.screening.evidence import evidence_nested_snapshots
    from chanlun.screening.nesting import saved_confirmation_valid
    main, lower = nested_sample["snapshots"]["5m"], nested_sample["snapshots"]["1m"]
    point, small = main["levels"][0]["points"][0], lower["levels"][0]["points"][0]
    small["anchor_at"] = point["anchor_at"] - 19 * 60
    small["dependency_from"] = main["screening_segments"][0]["start_time"] - 3600
    lower["screening_segments"][0]["end_time"] = small["anchor_at"]
    lower["screening_segments"][1]["start_time"] = small["anchor_at"]
    runner = _nested_runtime(monkeypatch, nested_sample)
    row = runner.scan_symbol({"code": "SH.600000", "name": "fixture"}, nested_sample["settings"],
                             nested_sample["contexts"], tmp_path)["rows"][0]
    assert len(row["selected"]) == 1, row
    assert row["selected"][0]["nested_confirmation"]["point"]["anchor_at"] == small["anchor_at"]
    assert saved_confirmation_valid(row["selected"][0], *evidence_nested_snapshots(
        tmp_path, "SH.600000", row["evidence"], row["confirmation_evidence"]))


@pytest.mark.parametrize("field,value", [("source_closed_at", 9999999999), ("symbol", "SH.600001"),
                                        ("price_basis_revision", "different"), ("source_frequency", "30m"),
                                        ("source_started_at", 9999999999)])
def test_nested_matching_rejects_future_or_mismatched_inputs(nested_sample, field, value):
    from chanlun.screening.nesting import matching_confirmations
    main, lower = nested_sample["snapshots"]["5m"], nested_sample["snapshots"]["1m"]
    lower[field] = value
    with pytest.raises(ValueError):
        matching_confirmations(main, main["levels"][0]["points"][0], lower, [])


def test_confirmed_lower_point_does_not_promote_unfinished_five_minute_point(nested_sample):
    from chanlun.screening.nesting import matching_confirmations, attach_confirmation, is_nested_observation
    main, lower = nested_sample["snapshots"]["5m"], nested_sample["snapshots"]["1m"]
    point = main["levels"][0]["points"][0]
    point.update(status="approaching", confirmed_at=None, missing_conditions=["terminal_unit_locked"])
    small = {"point": lower["levels"][0]["points"][0]}
    interval, matches = matching_confirmations(main, point, lower, [small])
    combined = attach_confirmation({"point": point, "reasons": ["NOT_CONFIRMED"]}, interval, matches[0])
    assert combined["nested_confirmation"]["state"] == "confirmed"
    assert combined["selection_missing_conditions"] == ["terminal_unit_locked"]
    assert is_nested_observation(combined)


def test_bad_lower_data_cannot_be_demoted_to_waiting(nested_sample, monkeypatch, tmp_path):
    runner = _nested_runtime(monkeypatch, nested_sample)
    nested_sample["frames"]["1m"] = nested_sample["frames"]["1m"].iloc[:-1]
    row = runner.scan_symbol({"code": "SH.600000", "name": "fixture"}, nested_sample["settings"],
                             nested_sample["contexts"], tmp_path)["rows"][0]
    assert not row["selected"] and not row["observations"]
    assert row["data_errors"] == ["LOWER_DATA_ERROR"]


def test_no_five_minute_setup_skips_one_minute_calculation(nested_sample, monkeypatch, tmp_path):
    runner = _nested_runtime(monkeypatch, nested_sample)
    nested_sample["snapshots"]["5m"]["levels"][0]["points"] = []
    nested_sample["frames"].pop("1m")
    row = runner.scan_symbol({"code": "SH.600000", "name": "fixture"}, nested_sample["settings"],
                             nested_sample["contexts"], tmp_path)["rows"][0]
    assert not row["selected"] and not row["observations"] and not row.get("data_errors")


@pytest.mark.parametrize("lower_present", [True, False])
def test_nested_result_reader_keeps_joint_state_and_serves_both_evidence_periods(
        nested_sample, monkeypatch, tmp_path, lower_present):
    import json
    from chanlun.core.strict_structure.operational_risk import macd_zero_axis_operational_state
    from cl_app.services.screening import ScreeningManager
    risk_cutoff = nested_sample["contexts"]["1m"]["cutoff"]
    known = datetime.fromtimestamp(risk_cutoff, CN)
    fact = macd_zero_axis_operational_state(
        (known,), (-0.2,), (-0.1,), physical_frequency="1m",
        as_of=known, closed_through=known, bar_known_times=(known,),
    )
    nested_sample["snapshots"]["1m"]["macd_zero_axis_operational_state"] = {
        **fact, "bar_market_at": risk_cutoff, "known_at": risk_cutoff,
        "price_basis_revision": nested_sample["snapshots"]["1m"]["price_basis_revision"],
    }
    nested_sample["settings"]["minimum_operating_frequency"] = "1m"
    runner = _nested_runtime(monkeypatch, nested_sample)
    if not lower_present:
        nested_sample["snapshots"]["1m"]["levels"][0]["points"] = []
    run_id = "f" * 32
    directory = tmp_path / run_id
    directory.mkdir()
    row = runner.scan_symbol({"code": "SH.600000", "name": "fixture"}, nested_sample["settings"],
                             nested_sample["contexts"], directory)
    (directory / "results.jsonl").write_text(json.dumps(row) + "\n", encoding="utf-8")
    runner.write_json(directory / "status.json", {"run_id": run_id, "status": "completed", "completed": 1,
                                                 "source_revision": runner.source_revision(),
                                                 "settings": nested_sample["settings"]})
    runner.write_json(tmp_path / "latest.json", {"run_id": run_id})
    monkeypatch.setattr(runner, "snapshot_for", lambda *_: pytest.fail("reading evidence must not calculate structure"))
    manager = ScreeningManager(tmp_path)
    result = manager.results()
    assert len(result["selected"]) == int(lower_present), result["recent_rejections"]
    assert len(result["observations"]) == int(not lower_present)
    candidate = (result["selected"] or result["observations"])[0]
    assert candidate["operational_macd_risk"]["risk_filter_state"] == "basic_avoid_on_selected_period"
    assert candidate["operational_macd_risk"]["physical_frequency"] == "1m"
    if not lower_present:
        # A confirmed chart point remains confirmed, while combined selection
        # waits. The reader must not promote it back into the confirmed list.
        waiting = result["observations"][0]
        assert waiting["point"]["status"] == "confirmed"
        assert waiting["selection_status"] == "approaching"
    for frequency in ("5m", "1m"):
        evidence = manager.evidence(run_id, "SH.600000", frequency, "P")
        assert evidence["snapshot"]["source_frequency"] == frequency
        chart = manager.evidence_history(run_id, "SH.600000", frequency, "P", first=True, start=0, end=9999999999)
        assert chart["s"] == "ok"
        assert len(chart["t"]) == len(nested_sample["frames"][frequency])
        assert chart["strict_structure"]["screening_operational_macd_risk"]["physical_frequency"] == "1m"
    (directory / "evidence" / "SH.600000_1m.json.gz").unlink()
    broken = manager.results()
    assert not broken["selected"] and not broken["observations"]
    assert broken["rejection_counts"]["NESTED_EVIDENCE_MISSING"] == 1


def test_retired_nested_strategy_cannot_be_started_as_a_new_screening_run():
    from cl_app.services.screening import validate_settings

    with pytest.raises(ValueError, match="已停用"):
        validate_settings({"scope": "codes", "codes": ["SH.600000"],
                           "strategy": "5m_with_1m_confirmation"})


@pytest.mark.parametrize("side", ["buy", "sell"])
def test_nested_first_class_requires_lower_trend_divergence(nested_sample, side):
    from chanlun.screening.nesting import matching_confirmations
    main, lower = nested_sample["snapshots"]["5m"], nested_sample["snapshots"]["1m"]
    point, small = main["levels"][0]["points"][0], lower["levels"][0]["points"][0]
    point.update(side=side, point_type="1" + side, divergence={"signal_leg_unit_ids": ["return"]})
    small.update(side=side, point_type="3" + side)
    assert matching_confirmations(main, point, lower, [{"point": small}])[1] == []
    small.update(point_type="1" + side, divergence={"kind": "consolidation"})
    assert matching_confirmations(main, point, lower, [{"point": small}])[1] == []
    small["divergence"]["kind"] = "trend"
    assert len(matching_confirmations(main, point, lower, [{"point": small}])[1]) == 1


@pytest.mark.parametrize("fault", ["outside_pivot", "different_extreme", "outside_departure"])
def test_first_class_trend_divergence_still_matches_the_terminal_turn(nested_sample, fault):
    from chanlun.screening.nesting import matching_confirmations
    main, lower = nested_sample["snapshots"]["5m"], nested_sample["snapshots"]["1m"]
    point, small = main["levels"][0]["points"][0], lower["levels"][0]["points"][0]
    point.update(point_type="1buy", divergence={"signal_leg_unit_ids": ["return"]})
    small.update(point_type="1buy", divergence={"kind": "trend"})
    if fault == "outside_pivot":
        small["anchor_at"] = point["anchor_at"] - 300
    elif fault == "different_extreme":
        small["anchor_tick"] += 1
    else:
        small["dependency_from"] = main["screening_segments"][0]["start_time"] - 300
    assert matching_confirmations(main, point, lower, [{"point": small}])[1] == []


def test_lower_replay_failure_cannot_be_published_as_waiting(nested_sample, monkeypatch, tmp_path):
    runner = _nested_runtime(monkeypatch, nested_sample)
    original = runner.snapshot_for
    available = nested_sample["snapshots"]["1m"]["levels"][0]["points"][0]["available_at"]

    def snapshot(frame, code, frequency):
        result = original(frame, code, frequency)
        if frequency == "1m" and int(frame.date.iloc[-1].timestamp()) == available:
            result["levels"][0]["points"] = []
        return result

    monkeypatch.setattr(runner, "snapshot_for", snapshot)
    row = runner.scan_symbol({"code": "SH.600000", "name": "fixture"}, nested_sample["settings"],
                             nested_sample["contexts"], tmp_path)["rows"][0]
    assert not row["selected"] and not row["observations"]
    assert row["reason_counts"]["LOWER_REPLAY_FAILED"] == 1


def test_cached_five_minute_result_rechecks_changed_one_minute_input(nested_sample, monkeypatch, tmp_path):
    runner = _nested_runtime(monkeypatch, nested_sample)
    stock = {"code": "SH.600000", "name": "fixture"}
    first = runner.scan_symbol(stock, nested_sample["settings"], nested_sample["contexts"], tmp_path / "first",
                               cache_root=tmp_path / "cache", revision="test")
    assert len(first["rows"][0]["selected"]) == 1
    nested_sample["snapshots"]["1m"]["levels"][0]["points"] = []
    nested_sample["frames"]["1m"].loc[10, "volume"] += 1
    second = runner.scan_symbol(stock, nested_sample["settings"], nested_sample["contexts"], tmp_path / "second",
                                cache_root=tmp_path / "cache", revision="test")["rows"][0]
    assert second["calculation_reused"]
    assert not second["selected"] and len(second["observations"]) == 1
    assert second["observations"][0]["nested_confirmation"]["point"] is None


def test_forced_rerun_bypasses_both_calculation_caches_without_changing_filters(nested_sample, monkeypatch, tmp_path):
    runner = _nested_runtime(monkeypatch, nested_sample)
    stock = {"code": "SH.600000", "name": "fixture"}
    settings = deepcopy(nested_sample["settings"])
    first = runner.scan_symbol(stock, settings, nested_sample["contexts"], tmp_path/"first",
                               cache_root=tmp_path/"cache", revision="test")["rows"][0]
    assert len(first["selected"]) == 1
    def forbidden_read(*_):
        raise AssertionError("forced rerun read a calculation cache")
    monkeypatch.setattr(runner, "read_calculation", forbidden_read)
    second = runner.scan_symbol(stock, settings, nested_sample["contexts"], tmp_path/"second",
                                cache_root=tmp_path/"cache", revision="test", force_rebuild=True)["rows"][0]
    assert not second.get("data_errors") and not second["calculation_reused"]
    assert len(second["selected"]) == 1
    assert second["selected"][0]["point"] == first["selected"][0]["point"]
    assert second["confirmation_evidence"]["status"] == "complete"
    assert settings == nested_sample["settings"]


@pytest.mark.parametrize("kind", ["1buy", "2buy", "3buy"])
def test_completed_confirming_segment_expires_buy_without_erasing_chart(sample, kind):
    frame, snapshot, context = sample
    if kind == "1buy":
        first_buy_proof(snapshot)
    elif kind == "2buy":
        second_buy_proof(snapshot)
    segment = snapshot["screening_segments"][1]
    # A formed solid line is still eligible until its endpoint is locked.
    segment["forming"] = False
    assert len(audit_snapshot(snapshot, frame, context, point_types=[kind])["selected"]) == 1
    segment.update(locked=True, confirmed_at=context["cutoff"])
    before = deepcopy(snapshot)
    result = audit_snapshot(snapshot, frame, context, point_types=[kind])
    assert not result["selected"] and not result["observations"]
    assert result["rejected"][0]["reasons"] == ["CONFIRMING_SEGMENT_COMPLETED"]
    assert result["rejected"][0]["confirmation_segment"]["segment"]["unit_id"] == "confirmation"
    assert snapshot == before


@pytest.mark.parametrize("fault", ["missing", "no_next", "broken_chain", "future_lock", "unknown_lock"])
def test_missing_or_noncausal_confirming_segment_is_not_selected(sample, fault):
    frame, snapshot, context = sample
    segments = snapshot["screening_segments"]
    if fault == "missing":
        snapshot.pop("screening_segments")
    elif fault == "no_next":
        segments.pop()
    elif fault == "broken_chain":
        segments[1]["start_time"] += 1
    elif fault == "future_lock":
        segments[1].update(locked=True, forming=False, confirmed_at=context["cutoff"] + 300)
    else:
        segments[1]["locked"] = None
    result = audit_snapshot(snapshot, frame, context)
    assert not result["selected"]
    assert result["rejected"][0]["reasons"] == ["CONFIRMING_SEGMENT_MISSING"]


def test_new_rally_cannot_reactivate_completed_confirmation_segment(sample):
    frame, snapshot, context = sample
    original = deepcopy(snapshot["screening_segments"][1])
    snapshot["screening_segments"][1].update(locked=True, forming=False, confirmed_at=context["cutoff"])
    snapshot["screening_segments"].append({**original, "unit_id": "later-rally"})
    result = audit_snapshot(snapshot, frame, context)
    assert result["rejected"][0]["reasons"] == ["CONFIRMING_SEGMENT_COMPLETED"]


def test_waiting_buy_does_not_require_a_confirming_segment(sample):
    frame, snapshot, context = sample
    snapshot.pop("screening_segments")
    snapshot["levels"][0]["points"][0].update(
        status="approaching", confirmed_at=None, missing_conditions=["terminal_unit_locked"])
    result = audit_snapshot(snapshot, frame, context)
    assert len(result["observations"]) == 1 and not result["selected"]


def test_confirmation_follows_structural_carrier_when_price_extreme_is_inside_an_earlier_leg(sample):
    from chanlun.screening.confirmation import confirmation_catalog
    _, snapshot, context = sample
    point = first_buy_proof(snapshot)
    point.update(anchor_at=context["expected_closes"][-150], price_anchor_unit_id="earlier-price-leg")
    status = confirmation_catalog(snapshot)[point["point_id"]]
    assert status["state"] == "in_progress"
    assert status["segment"]["unit_id"] == "confirmation"


def test_cached_replay_pass_cannot_keep_an_expired_buy(sample, monkeypatch, tmp_path):
    from chanlun.screening import runner
    from chanlun.screening.cache import calculation_key, candidate_signature, write_calculation
    frame, snapshot, context = sample
    candidate = audit_snapshot(snapshot, frame, context)["selected"][0]
    reviewed = {candidate_signature(candidate): {k: True for k in runner.REPLAY_CHECKS}}
    snapshot["screening_segments"][1].update(locked=True, forming=False, confirmed_at=context["cutoff"])
    key = calculation_key(frame, "SH.600000", "5m", "test")
    write_calculation(tmp_path / "cache/SH.600000_5m.json.gz", key, {"reason_counts": {}}, snapshot, reviewed)
    monkeypatch.setattr(runner, "fetch_frame", lambda *_: frame)
    monkeypatch.setattr(runner, "snapshot_for", lambda *_: pytest.fail("cached exclusion must not rebuild"))
    row = runner.scan_symbol({"code": "SH.600000", "name": "fixture"},
                             {"frequencies": ["5m"], "point_types": ["3buy"]}, {"5m": context},
                             tmp_path / "run", cache_root=tmp_path / "cache", revision="test")["rows"][0]
    assert row["calculation_reused"] and not row["selected"] and not row["observations"]
    assert row["reason_counts"]["CONFIRMING_SEGMENT_COMPLETED"] == 1


def test_cold_replay_must_also_preserve_confirming_segment_lifetime(sample, monkeypatch):
    from chanlun.screening import runner
    frame, snapshot, context = sample
    candidates = audit_snapshot(snapshot, frame, context)["selected"]
    def build(prefix, *_):
        result = deepcopy(snapshot)
        if len(prefix) == len(frame):
            result["screening_segments"][1].update(locked=True, forming=False, confirmed_at=context["cutoff"])
        elif prefix.date.iloc[-1].timestamp() < snapshot["levels"][0]["points"][0]["available_at"]:
            result["levels"][0]["points"] = []
        return result
    monkeypatch.setattr(runner, "snapshot_for", build)
    admitted, rejected = _review_candidates(snapshot, frame, candidates, "SH.600000", "5m")
    assert not admitted and rejected[0]["reasons"] == ["REBUILD_MISMATCH"]


def test_adjusted_half_tick_is_not_rounded_down_onto_an_invalidation_boundary():
    # NumPy's even rounding mapped 11.005 to 1100, while the chart's declared
    # half-up price basis maps it to 1101. Screening must use the same boundary.
    assert _price_ticks([11.005, 11.0049, 11.0, 52.175], .01).tolist() == [1101, 1100, 1100, 5218]


def test_extended_price_is_excluded_without_mutating_historical_buy(sample):
    frame, snapshot, context = sample
    original = deepcopy(snapshot)
    frame.loc[len(frame)-1, ["open", "low", "close", "high"]] = [20, 20, 20, 21]
    result = audit_snapshot(snapshot, frame, context)
    assert "PRICE_TOO_FAR" in result["rejected"][0]["reasons"]
    assert not result["selected"]
    assert snapshot == original
    assert len(audit_snapshot(snapshot, frame, context, max_anchor_gain_pct=80)["selected"]) == 1


@pytest.mark.parametrize("low,reason", [(10, "INVALIDATED"), (11, "ANCHOR_BROKEN")])
def test_third_buy_boundary_cross_or_broken_anchor_stays_excluded(sample, low, reason):
    frame, snapshot, context = sample
    frame.loc[len(frame)-50, "low"] = low
    result = audit_snapshot(snapshot, frame, context)
    assert not result["selected"]
    assert result["rejected"][0]["reasons"] == [reason]


@pytest.mark.parametrize("kind", ["1buy", "2buy"])
def test_first_and_second_buy_allow_equal_low_but_not_a_new_low(sample, kind):
    frame, snapshot, context = sample
    p = snapshot["levels"][0]["points"][0]
    first_buy_proof(snapshot) if kind == "1buy" else second_buy_proof(snapshot)
    p.update(invalidation_tick=12)
    assert len(audit_snapshot(snapshot, frame, context, point_types=[kind])["selected"]) == 1
    frame.loc[len(frame)-50, "low"] = 11
    assert "INVALIDATED" in audit_snapshot(snapshot, frame, context)["rejected"][0]["reasons"]


def test_recent_confirmation_does_not_freshen_old_anchor(sample):
    frame, snapshot, context = sample
    snapshot["levels"][0]["points"][0]["anchor_at"] = context["expected_closes"][0]
    result = audit_snapshot(snapshot, frame, context)
    assert not result["selected"]
    assert "OLD_ANCHOR" in result["rejected"][0]["reasons"]


def test_buy_low_break_excludes_recovered_third_buy_even_above_center(sample):
    frame, snapshot, context = sample
    # Half-unit ticks let the later pullback break 12 while staying above ZG=11.
    snapshot["structure_price_quantum"] = "0.5"
    p = snapshot["levels"][0]["points"][0]
    p["anchor_tick"] *= 2
    p["invalidation_tick"] *= 2
    c = snapshot["levels"][0]["centers"][0]
    for key in ("zd_tick", "zg_tick"):
        c["core"][key] *= 2
    for role in c["establishment_segments"]:
        for key in ("start_tick", "end_tick", "low_tick", "high_tick"):
            role[key] *= 2
    c["completion_return_segment"]["low_tick"] *= 2
    original = deepcopy(snapshot)
    assert len(audit_snapshot(snapshot, frame, context)["selected"]) == 1
    frame.loc[len(frame) - 50, "low"] = 11.5
    result = audit_snapshot(snapshot, frame, context)
    assert not result["selected"]
    assert result["rejected"][0]["reasons"] == ["ANCHOR_BROKEN"]
    assert snapshot == original


def test_unconfirmed_and_recursive_signals_do_not_enter_native_results(sample):
    frame, snapshot, context = sample
    p = snapshot["levels"][0]["points"][0]
    p.update(status="approaching", confirmed_at=None, missing_conditions=["lock"])
    higher = deepcopy(snapshot["levels"][0])
    higher["structural_level"] = 1
    higher["points"][0].update(status="confirmed", confirmed_at=p["available_at"])
    snapshot["levels"].append(higher)
    result = audit_snapshot(snapshot, frame, context)
    assert not result["selected"]
    assert result["unconfirmed_buys"] == 1
    assert len(result["rejected"]) == 1
    assert len(result["observations"]) == 1
    assert result["observations"][0]["observation_validation"] == "checked"


@pytest.mark.parametrize("fault", ["anchor", "dependency", "invalidation", "touch", "old", "gap"])
def test_forming_results_keep_price_age_dependency_and_data_guards(sample, fault):
    frame, snapshot, context = sample
    point = snapshot["levels"][0]["points"][0]
    point.update(status="approaching", confirmed_at=None, missing_conditions=["terminal_unit_locked"])
    if fault == "anchor":
        point["anchor_tick"] = 14
    elif fault == "dependency":
        point.pop("dependency_from")
        snapshot["levels"][0]["centers"] = []
    elif fault == "invalidation":
        frame.loc[len(frame)-1, "low"] = 10
    elif fault == "touch":
        point["anchor_tick"] = point["invalidation_tick"]
    elif fault == "old":
        context["anchor_from"] = "2026-09-12"
    else:
        frame = frame.drop(frame.index[-10]).reset_index(drop=True)
    result = audit_snapshot(snapshot, frame, context)
    assert not result["selected"] and not result["observations"]


def test_unconfirmed_evidence_is_persisted_without_claiming_confirmation_replays(sample, monkeypatch, tmp_path):
    from chanlun.screening import runner
    from chanlun.screening.evidence import load_evidence
    frame, snapshot, context = sample
    point = snapshot["levels"][0]["points"][0]
    point.update(status="approaching", confirmed_at=None, missing_conditions=["terminal_unit_locked"])
    monkeypatch.setattr(runner, "fetch_frame", lambda *_: frame)
    monkeypatch.setattr(runner, "snapshot_for", lambda *_: snapshot)
    row = runner.scan_symbol({"code": "SH.600000", "name": "fixture"},
                             {"frequencies": ["5m"], "point_types": ["3buy"]},
                             {"5m": context}, tmp_path)["rows"][0]
    assert not row["selected"] and not row["data_errors"]
    assert len(row["observations"]) == 1 and not row["observations"][0].get("audit")
    saved_frame, saved_snapshot, health = load_evidence(tmp_path, row["code"], row["frequency"], row["evidence"])
    assert saved_snapshot == snapshot and health["verified"]
    pd.testing.assert_frame_equal(saved_frame.assign(date=saved_frame.date.dt.tz_convert("UTC")),
                                  frame.assign(date=frame.date.dt.tz_convert("UTC")))


def test_invalid_later_sell_cannot_expire_buy(sample):
    frame, snapshot, context = sample
    points = snapshot["levels"][0]["points"]
    points.append({**points[0], "point_id": "invalid-sell", "side": "sell", "point_type": "3sell",
                   "anchor_at": context["expected_closes"][-50]})
    result = audit_snapshot(snapshot, frame, context, ["3buy", "3sell"])
    buy = next(item for item in [*result["selected"], *result["rejected"]]
               if item["point"]["point_id"] == "P")
    assert "LATER_SELL" not in buy["reasons"]
    assert any(item["point"]["point_id"] == "invalid-sell"
               and "STRUCTURE_MISMATCH" in item["reasons"] for item in result["rejected"])


def test_missing_bar_can_hide_stop_crossing_and_is_not_passed(sample):
    frame, snapshot, context = sample
    frame = frame.drop(index=len(frame)-50)
    result = audit_snapshot(snapshot, frame, context)
    assert result["data_errors"] == ["DATA_GAPS"]
    assert not result["selected"]


def test_gap_inside_formation_before_anchor_cannot_be_hidden_by_a_complete_tail(sample):
    frame, snapshot, context = sample
    frame = frame.drop(index=len(frame)-150)
    result = audit_snapshot(snapshot, frame, context)
    assert result["data_errors"] == ["DATA_GAPS"]
    assert result["data_quality"]["missing_bars"] == 1
    assert result["data_quality"]["first_missing_at"] < snapshot["levels"][0]["points"][0]["anchor_at"]


def test_microsecond_datetime_storage_uses_the_same_candle_grid(sample):
    frame, snapshot, context = sample
    frame["date"] = frame.date.astype(pd.DatetimeTZDtype(unit="us", tz=CN))
    assert audit_snapshot(snapshot, frame, context)["selected"]
    missing = audit_snapshot(snapshot, frame.drop(index=len(frame)-50), context)
    assert missing["data_errors"] == ["DATA_GAPS"] and missing["data_quality"]["missing_bars"] == 1


@pytest.mark.parametrize("timestamp", ["2026-09-11 14:59:59", "2026-09-11 12:30:00", "2026-09-12 10:00:00"])
def test_off_grid_lunch_and_weekend_bars_fail_closed(sample, timestamp):
    frame, _, context = sample
    extra = frame.iloc[-1:].copy()
    extra["date"] = pd.Timestamp(timestamp, tz=CN)
    extra["date"] = extra.date.astype(frame.date.dtype)
    assert "INVALID_SESSION" in validate_frame(pd.concat([frame, extra]).sort_values("date"), context)


def test_native_qmt_one_minute_auction_rows_are_preserved_but_not_required():
    context = trading_context(datetime(2026, 9, 13, 2, tzinfo=CN), "1m")
    frame = pd.DataFrame({"date": pd.to_datetime(context["expected_closes"],unit="s",utc=True).tz_convert(CN),
                          "open":12.,"high":13.,"low":12.,"close":12.5,"volume":100})
    for at in ("2026-09-11 09:25:00", "2026-09-11 09:30:00"):
        auction = frame.iloc[-1:].copy()
        auction["date"] = pd.Series([pd.Timestamp(at,tz=CN)],index=auction.index).astype(frame.date.dtype)
        frame = pd.concat([frame,auction],ignore_index=True).sort_values("date")
    frame.attrs["price_basis_provider"] = "qmt"
    assert validate_frame(frame,context) == []
    assert validate_frame(frame, {**context,"frequency":"5m"}) == ["INVALID_SESSION"]


def test_dependency_before_short_screening_calendar_window_is_checked(sample):
    from chanlun.screening.rules import expected_closes_between
    frame, snapshot, context = sample
    earlier = int(datetime.fromisoformat(context["trading_days"][-40]).replace(tzinfo=CN).timestamp())
    timestamps = sorted(expected_closes_between(context, earlier))
    extra = pd.DataFrame({"date": pd.to_datetime(timestamps, unit="s", utc=True).tz_convert(CN),
                          "open": 12., "high": 13., "low": 12., "close": 12.5, "volume": 100})
    snapshot["levels"][0]["points"][0]["dependency_from"] = timestamps[0]
    assert len(audit_snapshot(snapshot, extra, context)["selected"]) == 1
    result = audit_snapshot(snapshot, extra.drop(index=10), context)
    assert result["data_errors"] == ["DATA_GAPS"]
    assert result["data_quality"]["first_missing_at"] < min(context["expected_closes"])


def test_incomplete_input_cannot_become_a_cached_empty_result(sample, monkeypatch, tmp_path):
    from chanlun.screening import runner
    frame, snapshot, context = sample
    frame = frame.drop(index=len(frame)-25)
    snapshot["levels"][0]["points"] = []
    monkeypatch.setattr(runner, "fetch_frame", lambda *_: frame)
    def unexpected_build(*_):
        pytest.fail("unknown data must be stopped before candidate generation")
    monkeypatch.setattr(runner, "snapshot_for", unexpected_build)
    for attempt in (1, 2):
        result = runner.scan_symbol({"code":"SH.600000","name":"fixture"},
                 {"frequencies":["5m"],"point_types":["3buy"]}, {"5m":context},
                 tmp_path / str(attempt), cache_root=tmp_path / "cache", revision="test")
        assert result["rows"][0]["data_errors"] == ["DATA_GAPS"]
        assert result["rows"][0]["selected"] == []
    assert not (tmp_path / "cache/SH.600000_5m.json.gz").exists()


def test_stale_history_refresh_starts_at_actual_tail_not_a_fixed_recent_window(sample, monkeypatch):
    from unittest.mock import Mock
    from chanlun.screening import runner
    frame, _, context = sample
    old = frame.loc[frame.date <= datetime(2026,8,27,15,tzinfo=CN)].copy()
    exchange = Mock()
    exchange.get_start_date_by_frequency.return_value = "20260801"
    exchange.klines.side_effect = [old, frame]
    monkeypatch.setattr(runner, "_exchange", lambda:exchange)
    assert runner.fetch_frame("SH.600000", "5m", context) is frame
    assert exchange.klines.call_args.kwargs["args"] == {"exact_end":True,"download_start_date":"20260827"}


def test_point_subtypes_are_explicit_screening_settings(sample):
    frame, snapshot, context = sample
    p = snapshot["levels"][0]["points"][0]
    p.update(point_type="1buy", divergence={"kind": "consolidation"})
    result = audit_snapshot(snapshot, frame, context)
    assert not result["selected"]
    assert "FIRST_CLASS_NOT_TREND" in result["rejected"][0]["reasons"]
    second_buy_proof(snapshot, weak=True)
    assert "VARIANT_EXCLUDED" in audit_snapshot(snapshot, frame, context, point_types=["2buy"])["rejected"][0]["reasons"]
    assert audit_snapshot(snapshot, frame, context, point_types=["2buy"], include_weak_second=True)["selected"]


def test_completed_second_buy_below_first_does_not_require_consolidation_divergence(sample):
    frame, snapshot, context = sample
    point, parent = second_buy_proof(snapshot, weak=True)
    point.update(
        variant="lower_retest", divergence=None,
        invalidation_tick=point["anchor_tick"],
    )
    assert point["anchor_tick"] < parent["anchor_tick"]
    excluded = audit_snapshot(snapshot, frame, context, point_types=["2buy"])
    assert "VARIANT_EXCLUDED" in excluded["rejected"][0]["reasons"]
    included = audit_snapshot(
        snapshot, frame, context, point_types=["2buy"], include_weak_second=True
    )
    assert included["selected"]
    assert included["selected"][0]["point"]["variant"] == "lower_retest"


def test_higher_third_buy_entry_waits_for_exact_lower_precision_evidence():
    from chanlun.screening.rules import third_buy_entry_precision_reasons

    point = {
        "point_id": "third", "point_type": "3buy", "structural_level": 1,
        "center_id": "center", "anchor_unit_id": "return",
        "available_at": 120, "status": "confirmed",
    }
    assert third_buy_entry_precision_reasons(point, {}, 120) == [
        "THIRD_ENTRY_PRECISION_PENDING"
    ]
    evidence = {
        "point_id": "third", "point_type": "3buy",
        "parent_center_id": "center", "lower_return_trend_id": "return",
        "formal_third_point_available_at": 120,
        "point_status": "confirmed", "tradable": False,
        "formal_third_point_independent": True,
        "status": "bounded_precision_condition_observed",
        "author_entry_condition_supported": True,
        "precision_branch": "double_subsub_return",
        "precision_known_at": 115,
        "parent_center_initial_known_at": 110,
    }
    assert third_buy_entry_precision_reasons(point, {"third": evidence}, 120) == []
    assert third_buy_entry_precision_reasons(
        point, {"third": {**evidence, "parent_center_initial_known_at": None}}, 120
    ) == ["THIRD_ENTRY_PRECISION_PENDING"]
    assert third_buy_entry_precision_reasons(
        point, {"third": {**evidence, "parent_center_initial_known_at": 116}}, 120
    ) == ["STRUCTURE_MISMATCH"]
    assert third_buy_entry_precision_reasons(point, {"third": evidence}, 114) == [
        "THIRD_ENTRY_PRECISION_PENDING"
    ]
    assert third_buy_entry_precision_reasons(
        point, {"third": {**evidence, "lower_return_trend_id": None}}, 120
    ) == ["THIRD_ENTRY_PRECISION_PENDING"]
    assert third_buy_entry_precision_reasons(
        point, {"third": {**evidence, "lower_return_trend_id": "other"}}, 120
    ) == ["STRUCTURE_MISMATCH"]
    assert third_buy_entry_precision_reasons(
        {**point, "structural_level": 0}, {}, 120
    ) == []


def test_pending_higher_third_buy_persists_frozen_chart_for_review(sample, monkeypatch, tmp_path):
    from chanlun.screening import runner
    from chanlun.screening.evidence import load_evidence

    frame, snapshot, context = sample
    point = {**snapshot["levels"][0]["points"][0],
             "point_id": "higher-pending", "point_type": "3buy", "side": "buy",
             "structural_level": 1, "source_kind": "trend_type", "status": "confirmed",
             "confirmed_at": context["cutoff"], "missing_conditions": []}
    snapshot["levels"].append({"structural_level": 1, "points": [point],
                               "third_point_entry_precision_rule_states": []})
    rejected = {"point": point, "center": None,
                "reasons": ["THIRD_ENTRY_PRECISION_PENDING"]}
    result = {"data_errors": [], "data_quality": {"status": "complete"},
              "selected": [], "observations": [], "rejected": [rejected],
              "confirmed_buys": 1, "unconfirmed_buys": 0,
              "confirmed_sells": 0, "unconfirmed_sells": 0}
    monkeypatch.setattr(runner, "fetch_frame", lambda *_: frame)
    monkeypatch.setattr(runner, "snapshot_for", lambda *_: snapshot)
    monkeypatch.setattr(runner, "audit_snapshot", lambda *_args, **_kwargs: result)
    row, = runner.scan_symbol(
        {"code": "SH.600000", "name": "fixture"},
        {"frequencies": ["5m"], "point_types": ["3buy"], "structural_levels": [1]},
        {"5m": context}, tmp_path,
    )["rows"]
    assert not row["selected"] and not row["observations"] and not row["data_errors"]
    assert row["recent_rejections"][0]["reasons"] == ["THIRD_ENTRY_PRECISION_PENDING"]
    _, saved_snapshot, health = load_evidence(tmp_path, "SH.600000", "5m", row["evidence"])
    assert health["verified"] and saved_snapshot == snapshot


@pytest.mark.parametrize("failure", ["consolidation", "missing", "future", "wrong_level"])
def test_second_buy_cannot_inherit_an_invalid_first_buy(sample, failure):
    frame, snapshot, context = sample
    point, parent = second_buy_proof(snapshot)
    if failure == "consolidation":
        parent["divergence"]["kind"] = "consolidation"
    elif failure == "missing":
        point["parent_point_id"] = "unknown"
    elif failure == "future":
        parent["available_at"] = point["available_at"] + 300
    else:
        parent["structural_level"] = 1
    result = audit_snapshot(snapshot, frame, context, point_types=["2buy"])
    assert not result["selected"] and not result["observations"]
    assert any(r.startswith("SECOND_PARENT") for r in result["rejected"][0]["reasons"])


def test_consolidation_first_does_not_enter_waiting_candidates(sample):
    frame, snapshot, context = sample
    point = first_buy_proof(snapshot)
    point.update(status="approaching", confirmed_at=None, missing_conditions=["terminal_unit_locked"])
    point["divergence"]["kind"] = "consolidation"
    result = audit_snapshot(snapshot, frame, context)
    assert not result["selected"] and not result["observations"]


def test_consolidation_top_divergence_does_not_invalidate_a_buy_as_first_sell(sample):
    frame, snapshot, context = sample
    points = snapshot["levels"][0]["points"]
    points.append({**points[0], "point_id": "old-consolidation-sell", "point_type": "1sell",
                   "side": "sell", "anchor_at": points[0]["anchor_at"] + 300,
                   "divergence": {"kind": "consolidation"}})
    assert audit_snapshot(snapshot, frame, context)["selected"]


def test_evidence_failure_never_publishes_candidate(sample, monkeypatch, tmp_path):
    from chanlun.screening import runner
    frame, snapshot, context = sample
    monkeypatch.setattr(runner, "fetch_frame", lambda *_: frame)
    monkeypatch.setattr(runner, "snapshot_for", lambda *_: snapshot)
    monkeypatch.setattr(runner, "_review_candidates", lambda _s, _f, candidates, *_: (candidates, []))
    def disk_full(*_, **__):
        raise OSError("disk full")
    monkeypatch.setattr(pd.DataFrame, "to_parquet", disk_full)
    result = runner.scan_symbol({"code": "SH.600000", "name": "fixture"},
                               {"frequencies": ["5m"], "point_types": ["3buy"]}, {"5m": context}, tmp_path)
    assert not result["rows"][0]["selected"]
    assert result["rows"][0]["data_errors"] == ["EVIDENCE_WRITE_FAILED"]


def test_fetch_repairs_interior_gap_and_uses_trading_day_history(sample, monkeypatch):
    from unittest.mock import Mock
    from chanlun.screening import runner
    frame, _, context = sample
    exchange = Mock()
    exchange.get_start_date_by_frequency.return_value = "20260901000000"
    exchange.klines.side_effect = [frame.drop(index=len(frame)-150), frame]
    monkeypatch.setattr(runner, "_exchange", lambda: exchange)
    assert runner.fetch_frame("SH.600000", "5m", context) is frame
    first, second = exchange.klines.call_args_list
    assert first.kwargs["start_date"] <= context["history_from"].replace("-", "")
    assert first.kwargs["args"]["skip_download"] is True
    assert second.kwargs["args"] == {"exact_end": True}


def test_fetch_history_does_not_roll_forward_at_midnight_without_new_closed_bars(sample, monkeypatch):
    from types import SimpleNamespace
    from unittest.mock import Mock
    from datetime import timedelta
    from chanlun.exchange import exchange_qmt
    from chanlun.screening import runner

    frame, _, _ = sample
    ex = exchange_qmt.ExchangeQMT()
    ex.DEFAULT_LOOKBACK["5m"] = timedelta(days=365)
    ex.klines = Mock(return_value=frame)
    wall = [datetime(2026, 9, 30, 22, 42)]

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return wall[0] if tz is None else wall[0].replace(tzinfo=tz)

    monkeypatch.setattr(exchange_qmt, "datetime", SimpleNamespace(datetime=Clock))
    monkeypatch.setattr(runner, "_exchange", lambda: ex)
    monkeypatch.setattr(runner, "validate_frame", lambda *_: [])
    monkeypatch.setattr(runner, "frame_quality", lambda *_: {"errors": []})
    monkeypatch.setattr(runner, "_resolve_suspensions", lambda data, *_: data)
    contexts = []
    for observed in (datetime(2026, 9, 30, 22, 42, tzinfo=CN), datetime(2026, 10, 1, 2, 30, tzinfo=CN)):
        context = trading_context(observed, "5m")
        contexts.append(context)
        wall[0] = observed.replace(tzinfo=None)
        assert runner.fetch_frame("SZ.300879", "5m", context) is frame
    assert contexts[0]["cutoff"] == contexts[1]["cutoff"]
    first, second = ex.klines.call_args_list
    assert first.kwargs["start_date"] == second.kwargs["start_date"] == "20250930"
    assert first.kwargs["end_date"] == second.kwargs["end_date"] == "2026-09-30 15:00:00"


def test_repeat_calculation_reuses_only_identical_verified_input(sample, monkeypatch, tmp_path):
    from chanlun.screening import runner
    frame, snapshot, context = sample
    builds = []
    monkeypatch.setattr(runner, "fetch_frame", lambda *_: frame)
    def build(*_):
        builds.append(True)
        return snapshot
    monkeypatch.setattr(runner, "snapshot_for", build)
    def review(_s, _f, candidates, *_):
        for item in candidates:
            item["audit"] = {k: True for k in ("cold_rebuild", "confirmation_replay", "confirmation_boundary")}
        return candidates, []
    monkeypatch.setattr(runner, "_review_candidates", review)
    stock, settings = {"code": "SH.600000", "name": "fixture"}, {"frequencies": ["5m"], "point_types": ["3buy"]}
    def scan(n, revision="new", options=None):
        return runner.scan_symbol(stock, options or settings, {"5m": context}, tmp_path / str(n),
                                  cache_root=tmp_path / "cache", revision=revision)["rows"][0]
    first, second = scan(1), scan(2)
    assert first["selected"] == second["selected"]
    assert len(builds) == 1 and second["calculation_reused"] is True
    assert (tmp_path / "2/evidence/SH.600000_5m.parquet").is_file()
    frame.loc[0, "volume"] += 1
    assert scan(3)["calculation_reused"] is False
    frame.attrs["price_basis_revision"] = "adjustment changed"
    assert scan(4)["calculation_reused"] is False
    assert scan(5, options={**settings, "max_anchor_gain_pct": 15})["calculation_reused"] is True
    assert scan(6, revision="next")["calculation_reused"] is False
    (tmp_path / "cache/SH.600000_5m.json.gz").write_bytes(b"damaged")
    assert scan(7, revision="next")["calculation_reused"] is False
    assert len(builds) == 5


def test_cached_geometry_reapplies_filters_and_reviews_newly_eligible_points(sample, monkeypatch, tmp_path):
    from chanlun.screening import runner
    frame, snapshot, context = sample
    calls = []
    monkeypatch.setattr(runner, "fetch_frame", lambda *_: frame)
    def build(prefix, *_):
        calls.append(len(prefix))
        result = deepcopy(snapshot)
        if prefix.date.iloc[-1].timestamp() < snapshot["levels"][0]["points"][0]["available_at"]:
            result["levels"][0]["points"] = []
        return result
    monkeypatch.setattr(runner, "snapshot_for", build)
    def scan(n, **options):
        settings = {"frequencies": ["5m"], "point_types": ["3buy"], **options}
        return runner.scan_symbol({"code": "SH.600000", "name": "test"}, settings,
            {"5m": context}, tmp_path / str(n), cache_root=tmp_path / "cache", revision="test")["rows"][0]
    rejected = scan(1, max_anchor_gain_pct=1)
    assert not rejected["selected"] and len(calls) == 1
    admitted = scan(2, max_anchor_gain_pct=10)
    assert admitted["calculation_reused"] and len(admitted["selected"]) == 1
    assert len(calls) == 4  # Existing geometry plus independent full/prefix/prior-bar proof.
    assert admitted["replays_reused"] == 0
    narrowed = scan(3, point_types=["1buy"])
    assert narrowed["calculation_reused"] and not narrowed["selected"] and len(calls) == 4
    reused = scan(4, max_anchor_gain_pct=15)
    assert reused["calculation_reused"] and reused["replays_reused"] == 1 and len(calls) == 4
    assert (tmp_path / "4/evidence/SH.600000_5m.parquet").is_file()
    frame.loc[0, "volume"] += 1
    changed = scan(5)
    assert not changed["calculation_reused"] and changed["replays_reused"] == 0 and len(calls) == 8


def _deadline_fixture_worker(connection):
    import multiprocessing
    from chanlun.screening.worker_transport import JobInputReader, store_reply
    inputs = JobInputReader()
    try:
        while (message := connection.recv()) is not None:
            task = inputs.load(message)
            stock = task['stock']
            if stock["code"] == "SH.600000":
                multiprocessing.Event().wait(120)
            else:
                connection.send(store_reply(task, {"code": stock["code"], "rows": []}))
    except EOFError:
        pass


def test_blocked_worker_has_deadline_and_replacement_processes_next_stock(tmp_path):
    from chanlun.screening.runner import bounded_scan
    stocks = [{"code": code, "name": code} for code in ("SH.600000", "SH.600001")]
    results = [r for r, _ in bounded_scan(stocks, {"frequencies": ["5m"]}, {}, tmp_path, 1, "test",
               timeout_seconds=3, worker_target=_deadline_fixture_worker) if r is not None]
    assert results[0]["rows"][0]["data_errors"] == ["WORKER_TIMEOUT"]
    assert results[1] == {"code": "SH.600001", "rows": []}


def test_cancel_stops_a_blocked_worker_without_waiting_for_its_deadline(tmp_path):
    from chanlun.screening.runner import bounded_scan
    stream = bounded_scan([{"code": "SH.600000", "name": "fixture"}], {"frequencies": ["5m"]}, {}, tmp_path, 1,
                          "test", timeout_seconds=60, worker_target=_deadline_fixture_worker)
    assert next(stream)[0] is None
    (tmp_path / "cancel").touch()
    assert list(stream) == []


def _blocked_catalog_worker(connection, _settings, _run_dir):
    import multiprocessing
    multiprocessing.Event().wait(120)


def test_catalog_native_call_can_be_cancelled_without_waiting_for_return(tmp_path):
    from chanlun.screening.runner import bounded_catalog, ScreeningCancelled
    stream = bounded_catalog({}, tmp_path, timeout_seconds=60, worker_target=_blocked_catalog_worker)
    assert next(stream) is None
    (tmp_path / "cancel").touch()
    with pytest.raises(ScreeningCancelled):
        list(stream)


def test_catalog_has_its_own_deadline(tmp_path):
    from chanlun.screening.runner import bounded_catalog
    with pytest.raises(TimeoutError, match="股票目录读取"):
        list(bounded_catalog({}, tmp_path, timeout_seconds=2, worker_target=_blocked_catalog_worker))


def test_cancelled_catalog_does_not_start_a_process(tmp_path, monkeypatch):
    from chanlun.screening import runner
    (tmp_path / "cancel").touch()
    monkeypatch.setattr(runner.multiprocessing, "get_context", lambda *_:pytest.fail("cancelled job started a process"))
    with pytest.raises(runner.ScreeningCancelled):
        list(runner.bounded_catalog({}, tmp_path))


def test_verified_calendar_covers_cross_year_dependencies_and_holidays():
    from chanlun.screening.rules import expected_closes_between
    context = trading_context(datetime(2026, 1, 6, 15, tzinfo=CN), "5m")
    start = int(datetime(2025, 12, 31, 9, 30, tzinfo=CN).timestamp())
    closes = expected_closes_between(context, start)
    days = {datetime.fromtimestamp(t, CN).date().isoformat() for t in closes}
    assert days == {"2025-12-31", "2026-01-05", "2026-01-06"}
    assert len(context["calendar_sources"]) >= 2


def test_stale_end_duplicate_and_nonfinite_bars_fail_closed(sample):
    frame, snapshot, context = sample
    assert "STALE_DATA" in validate_frame(frame.iloc[:-1], context)
    assert "INVALID_BARS" in validate_frame(pd.concat([frame, frame.iloc[-1:]]), context)
    frame.loc[len(frame)-2, "close"] = float("nan")
    assert "INVALID_BARS" in validate_frame(frame, context)


@pytest.mark.parametrize("part", ["core", "entry", "return"])
def test_structurally_inconsistent_center_never_enters_results(sample, part):
    frame, snapshot, context = sample
    c = snapshot["levels"][0]["centers"][0]
    if part == "core":
        c["core"]["zd_tick"] = 9
    elif part == "entry":
        c["establishment_segments"][0]["locked"] = False
    else:
        c["completion_return_segment"]["low_tick"] = 10
    assert "STRUCTURE_MISMATCH" in audit_snapshot(snapshot, frame, context)["rejected"][0]["reasons"]


def test_anchor_price_must_exist_at_marked_time(sample):
    frame, snapshot, context = sample
    i = len(frame) - 100
    frame.loc[i, ["open", "low", "close", "high"]] = [14, 14, 14, 15]
    assert "ANCHOR_PRICE_MISMATCH" in audit_snapshot(snapshot, frame, context)["rejected"][0]["reasons"]


def test_valid_adjusted_anchor_is_checked_in_ticks_without_float_tolerance(sample):
    frame, snapshot, context = sample
    frame[["open", "high", "low", "close"]] = 5.2
    frame.loc[len(frame) - 100, ["open", "high", "low", "close"]] = 5.145
    snapshot["structure_price_quantum"] = "0.01"
    first_buy_proof(snapshot)
    snapshot["levels"][0]["points"][0].update(point_type="1buy", anchor_tick=515, invalidation_tick=515)
    result = audit_snapshot(snapshot, frame, context)
    assert len(result["selected"]) == 1
    assert not result["rejected"]


def test_prefix_replay_excludes_a_lookahead_only_buy(sample, monkeypatch):
    from chanlun.screening import runner
    frame, snapshot, context = sample
    candidates = audit_snapshot(snapshot, frame, context)["selected"]
    def build(prefix, *_):
        result = deepcopy(snapshot)
        if len(prefix) < len(frame):
            result["levels"][0]["points"] = []
        return result
    monkeypatch.setattr(runner, "snapshot_for", build)
    admitted, rejected = _review_candidates(snapshot, frame, candidates, "SH.600000", "5m")
    assert not admitted
    assert rejected[0]["reasons"] == ["CONFIRMATION_REPLAY_FAILED"]
    assert rejected[0]["audit"]["cold_rebuild"]


def test_replay_also_rejects_a_confirmation_time_that_is_too_late(sample, monkeypatch):
    from chanlun.screening import runner
    frame, snapshot, context = sample
    candidates = audit_snapshot(snapshot, frame, context)["selected"]
    # Reproducing the signal at the alleged confirmation bar is insufficient:
    # it must not already be confirmed in the immediately preceding prefix.
    monkeypatch.setattr(runner, "snapshot_for", lambda *_: deepcopy(snapshot))
    admitted, rejected = _review_candidates(snapshot, frame, candidates, "SH.600000", "5m")
    assert not admitted
    assert rejected[0]["reasons"] == ["CONFIRMATION_TIME_MISMATCH"]
    assert rejected[0]["audit"]["confirmation_boundary"] is False


@pytest.mark.parametrize("changed", ["divergence", "anchor_unit", "source_level", "center_roles", "parent"])
def test_replay_rejects_changed_proof_even_when_point_identity_and_price_match(sample, monkeypatch, changed):
    from chanlun.screening import runner
    frame, snapshot, context = sample
    if changed == "parent":
        point, _ = second_buy_proof(snapshot)
    else:
        point = first_buy_proof(snapshot)
    candidates = audit_snapshot(snapshot, frame, context, point_types=[point["point_type"]])["selected"]
    assert len(candidates) == 1

    def build(prefix, *_):
        result = deepcopy(snapshot)
        if int(prefix.date.iloc[-1].timestamp()) < point["available_at"]:
            result["levels"][0]["points"] = []
            return result
        p = result["levels"][0]["points"][0]
        if changed == "divergence":
            p["divergence"]["signal_leg_unit_ids"] = ["different-comparison-end"]
        elif changed == "anchor_unit":
            p["anchor_unit_id"] = "different-anchor-carrier"
        elif changed == "source_level":
            p["structural_level"] = 1
        elif changed == "parent":
            # The selected child's fields stay byte-for-byte identical. Its
            # parent's MACD proof still has to survive independent replay.
            result["levels"][0]["points"][1]["divergence"]["metrics"]["is_divergent"] = False
        else:
            result["levels"][0]["centers"][0]["establishment_segments"][0]["start_tick"] += 1
        return result

    monkeypatch.setattr(runner, "snapshot_for", build)
    admitted, rejected = _review_candidates(snapshot, frame, candidates, "SH.600000", "5m")
    assert not admitted
    assert set(rejected[0]["reasons"]) == {"REBUILD_MISMATCH", "CONFIRMATION_REPLAY_FAILED"}
    assert rejected[0]["audit"] == {"cold_rebuild": False, "confirmation_replay": False, "confirmation_boundary": False}


def test_replay_allows_new_render_revision_and_later_center_annotations(sample, monkeypatch):
    from chanlun.screening import runner
    frame, snapshot, context = sample
    point = snapshot["levels"][0]["points"][0]
    candidates = audit_snapshot(snapshot, frame, context)["selected"]

    def build(prefix, *_):
        result = deepcopy(snapshot)
        if int(prefix.date.iloc[-1].timestamp()) < point["available_at"]:
            result["levels"][0]["points"] = []
        else:
            result["levels"][0]["points"][0].update(render_id="same-point-new-snapshot", evidence_revision="later")
            result["levels"][0]["centers"][0].update(state="divergence_closed", available_at=context["cutoff"])
        return result

    monkeypatch.setattr(runner, "snapshot_for", build)
    admitted, rejected = _review_candidates(snapshot, frame, candidates, "SH.600000", "5m")
    assert admitted and not rejected
    assert all(admitted[0]["audit"].values())


def test_calendar_uses_friday_close_on_sunday_and_respects_lunch():
    context = trading_context(datetime(2026, 9, 13, 2, tzinfo=CN), "30m")
    assert datetime.fromtimestamp(context["cutoff"], CN) == datetime(2026, 9, 11, 15, tzinfo=CN)
    context = trading_context(datetime(2026, 9, 11, 12, tzinfo=CN), "30m")
    assert datetime.fromtimestamp(context["cutoff"], CN).hour == 11
    assert datetime.fromtimestamp(context["cutoff"], CN).minute == 30


@pytest.mark.parametrize("name", ["price_basis.py", "_lookback.py", "a_share_minute_grid.py", "fingerprints.py", "xtdata.py"])
def test_source_revision_tracks_transitive_runtime_helpers(monkeypatch, name):
    from pathlib import Path
    from chanlun.screening.runner import source_revision
    before = source_revision()
    original = Path.read_bytes
    def edited(path):
        content = original(path)
        return content + b"\n# dependency changed\n" if path.name == name else content
    monkeypatch.setattr(Path, "read_bytes", edited)
    assert source_revision() != before


@pytest.mark.parametrize("code,type_,expected", [("SH.600036","stock_cn",True),("SZ.301004","stock_cn",True),
    ("SH.510300","etf_cn",False),("SH.900901","stock_cn",False),("SZ.200001","stock_cn",False),
    ("SH.000001","index_cn",False)])
def test_universe_contains_a_shares_without_b_shares_etfs_or_indices(code,type_,expected):
    assert is_a_share({"code":code,"type":type_}) is expected
