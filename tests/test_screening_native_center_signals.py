"""Native-center-policy points enter screening only after source checks."""

from datetime import timedelta
from pathlib import Path
from copy import deepcopy

import pandas as pd
import pytest

from chanlun.core.strict_structure.native_center_signals import build_native_center_signals
from chanlun.cl_utils.point_exits import build_point_exit_plans
from chanlun.cl_utils.strict_chart import strict_center_to_chart_dict
from chanlun.screening import rules
from chanlun.screening import runner
from tests.core.strict_structure.helpers import completed_down_center, completed_up_center


def _sample(monkeypatch, *, unit_offset=0, return_low_tick=None, side="buy"):
    center = (completed_up_center(unit_offset, return_low_tick=return_low_tick) if side == "buy"
              else completed_down_center(unit_offset))
    retest = center.completion_return_unit
    source_units = (*center.establishment_units, retest)
    point, = build_native_center_signals(
        source_units, (center,), None,
        as_of=center.available_at,
    )
    anchor = pd.Timestamp(point["anchor_at"], unit="s", tz="UTC")
    later = anchor + timedelta(minutes=5)
    frame = pd.DataFrame({
        "date": [anchor, later],
        "low": [point["anchor_tick"] if side == "buy" else point["anchor_tick"] - 1] * 2,
        "high": [point["anchor_tick"] + 1 if side == "buy" else point["anchor_tick"]] * 2,
        "close": [point["anchor_tick"] + 1 if side == "buy" else point["anchor_tick"] - 1] * 2,
    }).astype({"low": float, "high": float, "close": float})
    snapshot = {
        "analysis_scope": "native_centers", "source_frequency": "1m",
        "source_closed_at": int(later.timestamp()),
        "structure_price_quantum": "1", "price_basis_revision": center.price_basis_revision,
        "levels": [{"centers": [strict_center_to_chart_dict(center)], "points": [point]}],
        "screening_segments": [
            {"unit_id": unit.unit_id, "direction": unit.direction,
             "source_kind": unit.source_kind.value, "structural_level": unit.structural_level,
             "price_basis_revision": unit.price_basis_revision,
             "start_time": int(unit.market_start.timestamp()),
             "end_time": int(unit.market_end.timestamp()),
             "start_tick": unit.start_tick, "end_tick": unit.end_tick,
             "low_tick": unit.low_tick, "high_tick": unit.high_tick,
             "locked": unit.locked, "forming": unit.forming,
             "confirmed_at": int(unit.confirmed_at.timestamp()),
             "available_at": int(unit.available_at.timestamp())}
            for unit in source_units
        ],
    }
    context = {
        "frequency": "1m", "cutoff": snapshot["source_closed_at"],
        "recent_from": "2026-01-01", "anchor_from": "2026-01-01",
    }
    monkeypatch.setattr(rules, "frame_quality", lambda *_: {"errors": [], "status": "complete"})
    return frame, snapshot, context


def test_center_retest_is_selected_and_a_later_boundary_break_removes_it(monkeypatch):
    frame, snapshot, context = _sample(monkeypatch)
    result = rules.audit_native_center_signals(snapshot, frame, context)
    assert result["observations"] == []
    assert len(result["selected"]) == 1
    assert result["selected"][0]["point"]["point_type"] == "3buy"
    assert result["selected"][0]["reasons"] == []

    frame.loc[1, "low"] = snapshot["levels"][0]["points"][0]["invalidation_tick"] - 1
    assert rules.audit_native_center_signals(snapshot, frame, context)["selected"] == []


def test_confirmed_third_point_keeps_its_crossing_stop_when_a_later_bar_touches(monkeypatch):
    # Formation first requires a strictly outside return. This test concerns
    # the existing stop trigger on a subsequent candle, not point formation.
    frame, snapshot, context = _sample(monkeypatch, return_low_tick=116)
    point, = snapshot["levels"][0]["points"]
    assert point["anchor_tick"] > point["invalidation_tick"]
    frame.loc[1, "low"] = point["invalidation_tick"]
    assert len(rules.audit_native_center_signals(snapshot, frame, context)["selected"]) == 1
    assert build_point_exit_plans(snapshot)[point["point_id"]]["stop_loss"]["trigger"] == "lt"

    frame.loc[1, "low"] = point["invalidation_tick"] - 1
    assert not rules.audit_native_center_signals(snapshot, frame, context)["selected"]


@pytest.mark.parametrize("side", ["buy", "sell"])
@pytest.mark.parametrize("close_noise", [0.0, 1e-12])
def test_exact_gain_limit_is_inclusive_and_an_extra_price_tick_is_excluded(monkeypatch, side, close_noise):
    frame, snapshot, context = _sample(monkeypatch, side=side)
    point, = snapshot["levels"][0]["points"]
    last = frame.index[-1]
    close = point["anchor_tick"] * (1.1 if side == "buy" else 0.9) + (close_noise if side == "buy" else -close_noise)
    frame.loc[last, "close"] = close
    frame.loc[last, "high"] = max(frame.high.iloc[-1], close)
    frame.loc[last, "low"] = min(frame.low.iloc[-1], close)
    selected, = rules.audit_native_center_signals(snapshot, frame, context, max_anchor_gain_pct=10)["selected"]
    assert selected["distance_from_anchor_pct"] == 10.0
    frame.loc[last, "close"] += 1 if side == "buy" else -1
    frame.loc[last, "high"] = max(frame.high.iloc[-1], frame.close.iloc[-1])
    frame.loc[last, "low"] = min(frame.low.iloc[-1], frame.close.iloc[-1])
    assert not rules.audit_native_center_signals(snapshot, frame, context, max_anchor_gain_pct=10)["selected"]


@pytest.mark.parametrize("fault", ["future_core", "future_point", "unlocked_core", "wrong_core",
                                   "wrong_envelope", "external_return_in_body", "wrong_anchor_time"])
def test_native_signal_proof_rejects_broken_center_body_or_price_anchor(monkeypatch, fault):
    frame, snapshot, context = _sample(monkeypatch)
    center, = snapshot["levels"][0]["centers"]
    point, = snapshot["levels"][0]["points"]
    core = next(unit for unit in snapshot["screening_segments"]
                if unit["unit_id"] == center["core_unit_ids"][1])
    if fault == "future_core":
        core["available_at"] = point["confirmed_at"] + 1
    elif fault == "future_point":
        point.update(confirmed_at=snapshot["source_closed_at"] + 1,
                     available_at=snapshot["source_closed_at"] + 1)
    elif fault == "unlocked_core":
        core.update(locked=False, confirmed_at=None)
    elif fault == "wrong_core":
        center["core"]["zg_tick"] -= 1
    elif fault == "wrong_envelope":
        center["envelope"]["gg_tick"] += 1
    elif fault == "external_return_in_body":
        center["body_unit_ids"].append(center["completion_return_unit_id"])
    else:
        point["anchor_at"] = snapshot["screening_segments"][0]["start_time"]
    with pytest.raises(ValueError, match="source proof mismatch"):
        rules.audit_native_center_signals(snapshot, frame, context)


@pytest.mark.parametrize("source,kind,reason", [
    ("segment_local/SZ.001309_1m.parquet", "3sell", "LATER_BUY"),
    ("stroke_v13/SH.600519_1m.parquet", "3buy", "LATER_SELL"),
])
def test_later_confirmed_opposite_ends_current_selection_but_keeps_chart_point(
    monkeypatch, source, kind, reason,
):
    frame = pd.read_parquet(Path("tests/fixtures") / source)
    frame.attrs.update(structure_price_quantum="0.01", price_basis_revision="recorded-raw")
    code = "SZ.001309" if "SZ.001309" in source else "SH.600519"
    snapshot = runner.snapshot_for(frame, code, "1m")
    context = {"frequency": "1m", "cutoff": snapshot["source_closed_at"],
               "recent_from": "2026-01-01", "anchor_from": "2026-01-01"}
    monkeypatch.setattr(rules, "frame_quality", lambda *_: {"errors": [], "status": "complete"})
    before = deepcopy(snapshot["levels"][0]["points"])
    result = rules.audit_native_center_signals(snapshot, frame, context, point_types=(kind,), max_anchor_gain_pct=100)
    closed = [item for item in result["rejected"] if reason in item["reasons"]]
    assert closed
    assert not {item["point"]["point_id"] for item in closed} & {
        item["point"]["point_id"] for item in result["selected"]
    }
    assert snapshot["levels"][0]["points"] == before
    assert not result["observations"]


def test_opposite_point_has_to_be_known_before_it_ends_the_current_reminder(monkeypatch):
    frame = pd.read_parquet(Path("tests/fixtures/stroke_v13/SH.600519_1m.parquet"))
    frame.attrs.update(structure_price_quantum="0.01", price_basis_revision="recorded-raw")
    full = runner.snapshot_for(frame, "SH.600519", "1m")
    sell = next(point for point in full["levels"][0]["points"] if point["point_type"] == "3sell")
    prefix = frame.loc[frame.date < pd.Timestamp(sell["available_at"], unit="s", tz="UTC")].copy()
    snapshot = runner.snapshot_for(prefix, "SH.600519", "1m")
    assert not any(point["point_id"] == sell["point_id"] for point in snapshot["levels"][0]["points"])
    context = {"frequency": "1m", "cutoff": snapshot["source_closed_at"],
               "recent_from": "2026-01-01", "anchor_from": "2026-01-01"}
    monkeypatch.setattr(rules, "frame_quality", lambda *_: {"errors": [], "status": "complete"})
    result = rules.audit_native_center_signals(snapshot, prefix, context, point_types=("3buy",), max_anchor_gain_pct=100)
    assert not any("LATER_SELL" in item["reasons"] for item in result["rejected"])
    assert any(item["point_type"] == "3buy" for item in snapshot["levels"][0]["points"])


def test_native_signal_recency_and_age_follow_the_market_session_day(monkeypatch):
    frame, snapshot, context = _sample(monkeypatch, unit_offset=120)
    context.update(session_timezone="America/New_York", recent_from="2026-01-05",
                   anchor_from="2026-01-05", trading_days=["2026-01-02", "2026-01-05"])
    selected, = rules.audit_native_center_signals(snapshot, frame, context)["selected"]
    assert selected["anchor_age_sessions"] == 0
    assert selected["confirmation_age_sessions"] == 0
    assert selected["confirmation_delay_sessions"] == 0

    # The point was known on Jan 5 in New York, even though Shanghai had
    # already crossed midnight. It must leave a Jan 6-only result window.
    frame.loc[1, "date"] += timedelta(days=1)
    snapshot["source_closed_at"] = int(frame.date.iloc[-1].timestamp())
    context.update(cutoff=snapshot["source_closed_at"], recent_from="2026-01-06",
                   trading_days=["2026-01-02", "2026-01-05", "2026-01-06"])
    assert not rules.audit_native_center_signals(snapshot, frame, context)["selected"]


def test_native_signal_audit_rejects_a_point_without_declared_policy(monkeypatch):
    frame, snapshot, context = _sample(monkeypatch)
    snapshot["levels"][0]["points"][0]["rule_basis"] = "unverified"
    with pytest.raises(ValueError, match="proof mismatch"):
        rules.audit_native_center_signals(snapshot, frame, context)


def test_frozen_source_graph_rejects_weaker_first_and_unrelated_second_parent():
    frame = pd.read_parquet(Path("tests/fixtures/segment_completion/SH_600004_5m.parquet"))
    frame.attrs.update(structure_price_quantum="0.01", price_basis_revision="recorded-raw")
    snapshot = runner.snapshot_for(frame, "SH.600004", "5m")
    level = snapshot["levels"][0]
    centers = {item["center_id"]: item for item in level["centers"]}
    first = next(item for item in level["points"] if item["point_type"] == "1buy")
    second = next(item for item in level["points"] if item["point_type"] == "2buy")
    assert rules._native_center_signal_source_valid(
        snapshot, first, centers, snapshot["screening_segments"],
    )
    assert rules._native_center_signal_source_valid(
        snapshot, second, centers, snapshot["screening_segments"],
    )
    weakened = deepcopy(first)
    weakened["divergence"]["histogram_area_decayed"] = False
    assert not rules._native_center_signal_source_valid(
        snapshot, weakened, centers, snapshot["screening_segments"],
    )
    unrelated = {**second, "parent_point_id": "not-the-first"}
    assert not rules._native_center_signal_source_valid(
        snapshot, unrelated, centers, snapshot["screening_segments"],
    )
    late_sources = deepcopy(snapshot["screening_segments"])
    comparison_source = next(unit for unit in late_sources
                             if unit["unit_id"] == first["divergence"]["compare_unit_id"])
    comparison_source["available_at"] = first["confirmed_at"] + 1
    assert not rules._native_center_signal_source_valid(snapshot, first, centers, late_sources)
    future_second = {**second, "confirmed_at": first["available_at"] - 1}
    assert not rules._native_center_signal_source_valid(
        snapshot, future_second, centers, snapshot["screening_segments"],
    )
    cyclic = deepcopy(snapshot)
    cyclic_second = next(item for item in cyclic["levels"][0]["points"]
                         if item["point_id"] == second["point_id"])
    cyclic_second["parent_point_id"] = cyclic_second["point_id"]
    assert not rules._native_center_signal_source_valid(
        cyclic, cyclic_second, centers, cyclic["screening_segments"],
    )


def test_recorded_native_center_signal_reaches_confirmed_screening_result(tmp_path, monkeypatch):
    frame = pd.read_parquet(Path("tests/fixtures/SH.600519_5m.parquet")).head(10000)
    frame.attrs.update(structure_price_quantum="0.01", price_basis_revision="recorded-raw")
    full = runner.snapshot_for(frame, "SH.600519", "5m")
    point = full["levels"][0]["points"][-1]
    prefix = frame.loc[frame.date <= pd.Timestamp(
        point["available_at"], unit="s", tz="Asia/Shanghai",
    )].copy()
    prefix.attrs.update(frame.attrs)
    context = rules.trading_context(prefix.date.iloc[-1] + timedelta(minutes=1), "5m")
    monkeypatch.setattr(runner, "fetch_frame", lambda *_args, **_kwargs: prefix)
    result = runner.scan_symbol(
        {"code": "SH.600519", "name": "fixture", "market": "a"},
        {"frequencies": ["5m"], "point_types": list(rules.POINT_TYPES),
         "strategy": runner.DIRECT_STRATEGY, "structural_levels": [0]},
        {"5m": context}, tmp_path,
    )
    row, = result["rows"]
    assert row["data_errors"] == []
    assert row["observations"] == []
    selected, = row["selected"]
    assert selected["point"]["point_type"] == "3buy"
    assert selected["point"]["status"] == "confirmed"
    assert all(selected["audit"].values())
    assert row["evidence"]["status"] == "complete"


@pytest.mark.parametrize("kind", ["1buy", "2buy"])
def test_recorded_first_and_second_points_reach_screening_after_confirmed_prefix(
    tmp_path, monkeypatch, kind,
):
    frame = pd.read_parquet(Path("tests/fixtures/segment_completion/SH_600004_5m.parquet"))
    frame.attrs.update(structure_price_quantum="0.01", price_basis_revision="recorded-raw")
    full = runner.snapshot_for(frame, "SH.600004", "5m")
    point = next(item for item in full["levels"][0]["points"]
                 if item["point_type"] == kind)
    prefix = frame.loc[frame.date <= pd.Timestamp(
        point["available_at"], unit="s", tz="Asia/Shanghai",
    )].copy()
    prefix.attrs.update(frame.attrs)
    context = rules.trading_context(
        prefix.date.iloc[-1] + timedelta(minutes=1), "5m", 20, 60,
    )
    monkeypatch.setattr(runner, "fetch_frame", lambda *_args, **_kwargs: prefix)
    result = runner.scan_symbol(
        {"code": "SH.600004", "name": "fixture", "market": "a"},
        {"frequencies": ["5m"], "point_types": list(rules.POINT_TYPES),
         "strategy": runner.DIRECT_STRATEGY, "structural_levels": [0],
         "max_anchor_gain_pct": 100},
        {"5m": context}, tmp_path,
    )
    row, = result["rows"]
    assert not row["data_errors"]
    selected_types = {candidate["point"]["point_type"] for candidate in row["selected"]}
    if kind == "1buy":
        assert "1buy" not in selected_types and "2buy" in selected_types
        assert row["reason_counts"]["FIRST_ADVANCED_TO_SECOND"]
    else:
        assert kind in selected_types
    assert all(candidate["point"]["status"] == "confirmed"
               and all(candidate["audit"].values()) for candidate in row["selected"])
