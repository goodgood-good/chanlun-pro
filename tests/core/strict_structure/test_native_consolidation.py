"""Completed, single-center comparisons under the declared segment policy."""

from copy import deepcopy
from dataclasses import replace

import pandas as pd
import pytest

from chanlun.cl_utils.strict_chart import strict_center_to_chart_dict
from chanlun.core.strict_structure.native_consolidation import build_native_consolidation_alerts
from chanlun.core.strict_structure.strength import StrengthSnapshot
from chanlun.screening.rules import audit_native_consolidation_alerts, native_consolidation_source_valid
from tests.core.strict_structure.helpers import ongoing_center, ongoing_down_center


class Areas:
    def __init__(self, values=None):
        self.values = values or {"u-3": 5, "u-4": 5}

    def snapshot(self, unit):
        peak = 1 if unit.unit_id in {"u-3", "u-4"} else 2
        sign = -1 if unit.direction == "down" else 1
        return StrengthSnapshot(unit_id=unit.unit_id, direction=unit.direction,
                                histogram_area=self.values.get(unit.unit_id, 10),
                                histogram_peak=sign * peak, dif_extreme=sign * peak,
                                source="macd", available_at=unit.available_at)


def fixture(center=None, strength=None):
    center = center or ongoing_center()
    units = center.establishment_units
    as_of = max(unit.available_at for unit in units)
    alerts = build_native_consolidation_alerts(units, (center,), strength or Areas(), as_of=as_of)
    serialized = [{"unit_id": unit.unit_id, "structural_level": 0, "source_kind": "segment",
                   "price_basis_revision": unit.price_basis_revision, "direction": unit.direction,
                   "start_time": int(unit.market_start.timestamp()), "end_time": int(unit.market_end.timestamp()),
                   "start_tick": unit.start_tick, "end_tick": unit.end_tick, "low_tick": unit.low_tick,
                   "high_tick": unit.high_tick, "locked": unit.locked, "forming": unit.forming,
                   "confirmed_at": None if unit.confirmed_at is None else int(unit.confirmed_at.timestamp()),
                   "available_at": int(unit.available_at.timestamp())} for unit in units]
    snapshot = {"analysis_scope": "native_centers", "source_frequency": "5m", "source_closed_at": int(as_of.timestamp()),
                "price_basis_revision": center.price_basis_revision, "structure_price_quantum": "1",
                "screening_segments": serialized, "native_consolidation_alerts": alerts,
                "levels": [{"centers": [strict_center_to_chart_dict(center)], "points": []}]}
    frame = pd.DataFrame([{"date": unit.market_end, "open": unit.start_tick, "close": unit.end_tick,
                           "low": unit.low_tick, "high": unit.high_tick, "volume": 100} for unit in units])
    context = {"frequency": "5m", "cutoff": snapshot["source_closed_at"],
               "recent_from": "2026-01-01", "anchor_from": "2026-01-01"}
    return center, units, snapshot, frame, context


@pytest.mark.parametrize("down", [False, True])
def test_one_center_produces_independent_up_and_down_area_comparisons(down):
    center = ongoing_down_center() if down else ongoing_center()
    _, _, snapshot, _, _ = fixture(center)
    alerts = snapshot["native_consolidation_alerts"]
    departure = next(alert for alert in alerts if alert["comparison_form"] == "entry_departure")
    assert departure["direction"] == ("down" if down else "up")
    assert departure["core_position"] == "breakout"
    assert departure["macd_area_c"] < departure["macd_area_a"]
    assert "point_type" not in departure
    assert all(native_consolidation_source_valid(snapshot, alert) for alert in alerts)


def test_center_internal_area_decay_does_not_require_a_new_price_extreme():
    _, _, snapshot, _, _ = fixture()
    inside = next(alert for alert in snapshot["native_consolidation_alerts"]
                  if alert["comparison_form"] == "center_oscillation" and alert["core_position"] == "inside")
    assert inside["price_extreme_confirmed"] is False
    assert inside["direction"] == "down"
    assert native_consolidation_source_valid(snapshot, inside)


def test_peak_weakening_with_equal_or_larger_area_produces_no_consolidation_alert():
    center, units, _, _, _ = fixture()
    for area in (10, 20):
        assert not build_native_consolidation_alerts(units, (center,), Areas({"u-3": area, "u-4": area}),
                                                    as_of=units[-1].available_at)


def test_unlocked_or_future_source_cannot_confirm_a_consolidation_alert():
    center, units, _, _, _ = fixture()
    changed = (*units[:-1], replace(units[-1], locked=False, confirmed_at=None))
    assert not build_native_consolidation_alerts(changed, (center,), Areas(), as_of=units[-1].available_at)
    assert not build_native_consolidation_alerts(units, (center,), Areas(), as_of=units[-1].market_end)


@pytest.mark.parametrize("fault", ["future", "source", "basis", "price", "area", "clock"])
def test_frozen_consolidation_source_rejects_broken_proof(fault):
    _, _, snapshot, _, _ = fixture()
    alert = deepcopy(next(alert for alert in snapshot["native_consolidation_alerts"] if alert["comparison_form"] == "entry_departure"))
    if fault == "future":
        snapshot["screening_segments"][-1]["available_at"] = alert["available_at"] + 1
    elif fault == "source":
        alert["source_unit_ids"].pop(1)
    elif fault == "basis":
        alert["price_basis_revision"] = "wrong-basis"
    elif fault == "price":
        alert["anchor_tick"] += 1
    elif fault == "area":
        alert["macd_area_c"] = alert["macd_area_a"]
    else:
        alert["available_at"] += 1
    assert not native_consolidation_source_valid(snapshot, alert)


def test_current_selection_keeps_only_latest_directional_legs_and_recent_confirmations():
    _, _, snapshot, frame, context = fixture()
    selected = audit_native_consolidation_alerts(snapshot, frame, context)
    assert selected and len({(item["alert"]["center_id"], item["alert"]["direction"],
                             item["alert"]["comparison_form"]) for item in selected}) == len(selected)
    assert not audit_native_consolidation_alerts(snapshot, frame, {**context, "recent_from": "2026-02-01"})
