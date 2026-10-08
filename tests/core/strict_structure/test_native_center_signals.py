"""Production center signals must stay causal under the declared policy."""

from datetime import timedelta
from dataclasses import replace
from pathlib import Path

import pandas as pd
import pytest

from chanlun.cl_utils.strict_chart_runtime import build_strict_chart_cd
from chanlun.core.strict_structure.center_relation import classify_center_relation
from chanlun.core.strict_structure.models import CenterRelation
from chanlun.core.strict_structure.center_machine import calculate_three_core_segment_centers
from chanlun.core.strict_structure.native_center_signals import build_native_center_signals
from chanlun.core.strict_structure.strength import MacdStrengthProvider, StrengthSnapshot
from tests.core.strict_structure.helpers import (
    completed_up_center,
    ongoing_center,
    unit,
)


class FixedStrength:
    def snapshot(self, segment):
        weak = segment.unit_id == "u-7"
        return StrengthSnapshot(
            unit_id=segment.unit_id,
            direction=segment.direction,
            histogram_area=5 if weak else 10,
            histogram_peak=-5 if segment.direction == "down" and weak else
                           -10 if segment.direction == "down" else 10,
            dif_extreme=-1 if segment.direction == "down" and weak else
                        -2 if segment.direction == "down" else 2,
            source="macd",
            available_at=segment.available_at,
        )


@pytest.mark.parametrize("mirror", [False, True])
def test_failed_departures_remain_center_oscillation_and_cannot_create_a_false_trend(mirror):
    # L020 author text lines 40-58: separation includes surrounding waves,
    # not just the immutable core or legs ending back inside that core.
    prices = [100, 80, 95, 85, 120, 60, 130, 110, 125, 115, 150, 140, 155]
    if mirror:
        prices = [300 - price for price in prices]
    units = tuple(unit(index, "up" if end > start else "down", start, end)
                  for index, (start, end) in enumerate(zip(prices, prices[1:])))
    centers = calculate_three_core_segment_centers(units, 0).centers
    assert len(centers) == 2
    first, second = centers
    assert [item.unit_id for item in first.failed_departure_units] == ["u-3", "u-4"]
    assert classify_center_relation(first, second) is CenterRelation.UPGRADE

    class WeakLastDeparture:
        def snapshot(self, segment):
            weak = segment.market_start >= units[9].market_start
            size = 1 if weak else 100
            sign = 1 if segment.direction == "up" else -1
            return StrengthSnapshot(segment.unit_id, segment.direction, size, sign * size,
                                    sign * size, "macd", segment.available_at)

    signals = build_native_center_signals(units, centers, WeakLastDeparture(), as_of=units[-1].available_at)
    assert not any(point["point_type"].startswith("1") for point in signals)
    assert len([point for point in signals if point["point_type"].startswith("3")]) == 2


def test_native_points_cannot_omit_failed_departures_from_the_actual_source_graph():
    prices = [100, 80, 95, 85, 120, 60, 130, 110]
    units = tuple(unit(index, "up" if end > start else "down", start, end)
                  for index, (start, end) in enumerate(zip(prices, prices[1:])))
    centers = calculate_three_core_segment_centers(units, 0).centers
    assert build_native_center_signals(units, centers, None, as_of=units[-1].available_at)
    damaged = tuple(item for item in units if item.unit_id != "u-3")
    assert build_native_center_signals(damaged, centers, None, as_of=units[-1].available_at) == []


def test_one_center_and_arbitrary_adjacent_macd_weakening_issue_no_first_or_second_point():
    center = ongoing_center()
    trail = (
        unit(5, "down", 120, 100),
        unit(6, "up", 100, 125),
        unit(7, "down", 125, 95),
        unit(8, "up", 95, 120),
        unit(9, "down", 120, 98),
    )
    values = (*center.establishment_units, *trail)
    alerts = build_native_center_signals(
        values, (center,), FixedStrength(), as_of=trail[-1].available_at,
    )
    assert not any(item["point_type"].startswith(("1", "2")) for item in alerts)


def test_recorded_first_and_second_points_use_two_separated_centers_and_complete_legs():
    frame = pd.read_parquet(
        Path("tests/fixtures/segment_completion/SH_600004_5m.parquet")
    )
    frame.attrs.update(structure_price_quantum="0.01", price_basis_revision="recorded-raw")
    runtime = build_strict_chart_cd(
        market="a", code="SH.600004", frequency="5m",
        frame=frame, last_bar_closed=True,
    )
    cd = runtime.cd
    assert cd is not None
    centers = tuple(cd.get_native_centers().centers)
    signals = build_native_center_signals(
        tuple(cd.get_segment_units()), centers, MacdStrengthProvider(cd),
        as_of=cd._strict_as_of(),
    )
    first, = (item for item in signals if item["point_type"] == "1buy")
    second, = (item for item in signals if item["point_type"] == "2buy")
    later_index = next(index for index, center in enumerate(centers)
                       if center.center_id == first["center_id"])
    previous, later = centers[later_index - 1:later_index + 1]
    assert classify_center_relation(previous, later) is CenterRelation.DOWN_TREND
    assert first["divergence"]["first_center_id"] == previous.center_id
    assert first["divergence"]["compare_leg_unit_ids"][-1] == later.entry_unit.unit_id
    assert len(first["divergence"]["signal_leg_unit_ids"]) == 3
    assert later.completion_leave_unit.unit_id in first["divergence"]["signal_leg_unit_ids"]
    assert later.completion_return_unit.unit_id in first["divergence"]["signal_leg_unit_ids"]
    assert first["divergence"]["histogram_area_decayed"] is True
    assert second["parent_point_id"] == first["point_id"]
    assert second["related_unit_ids"][0] == first["price_anchor_unit_id"]
    assert first["price_anchor_unit_id"] != first["anchor_unit_id"]
    assert second["anchor_unit_id"] == first["anchor_unit_id"]
    assert first["anchor_at"] < second["anchor_at"]
    assert first["available_at"] <= second["confirmed_at"]


def test_price_extreme_with_only_peak_or_dif_weakening_is_not_a_first_point():
    frame = pd.read_parquet(Path("tests/fixtures/segment_completion/SH_600004_5m.parquet"))
    frame.attrs.update(structure_price_quantum="0.01", price_basis_revision="recorded-raw")
    runtime = build_strict_chart_cd(
        market="a", code="SH.600004", frequency="5m",
        frame=frame, last_bar_closed=True,
    )
    cd = runtime.cd
    assert cd is not None
    centers = tuple(cd.get_native_centers().centers)
    native_strength = MacdStrengthProvider(cd)
    baseline = build_native_center_signals(
        tuple(cd.get_segment_units()), centers, native_strength,
        as_of=cd._strict_as_of(),
    )
    first, = (item for item in baseline if item["point_type"] == "1buy")
    owner = next(center for center in centers if center.center_id == first["center_id"])
    departure = owner.completion_leave_unit
    assert departure is not None

    class NoDepartureAreaDecay:
        def snapshot(self, unit):
            strength = native_strength.snapshot(unit)
            if len(getattr(unit, "child_ids", ())) == 3 and unit.market_start >= departure.market_start:
                return replace(strength, histogram_area=1_000_000.0)
            return strength

    signals = build_native_center_signals(
        tuple(cd.get_segment_units()), centers, NoDepartureAreaDecay(),
        as_of=cd._strict_as_of(),
    )
    assert not any(item["point_type"] in {"1buy", "2buy"} for item in signals)


def test_unlocked_first_rebound_cannot_be_skipped_to_create_a_second_point():
    frame = pd.read_parquet(Path("tests/fixtures/segment_completion/SH_600004_5m.parquet"))
    frame.attrs.update(structure_price_quantum="0.01", price_basis_revision="recorded-raw")
    runtime = build_strict_chart_cd(
        market="a", code="SH.600004", frequency="5m",
        frame=frame, last_bar_closed=True,
    )
    cd = runtime.cd
    assert cd is not None
    centers = tuple(cd.get_native_centers().centers)
    values = tuple(cd.get_segment_units())
    original = build_native_center_signals(
        values, centers, MacdStrengthProvider(cd), as_of=cd._strict_as_of(),
    )
    first, = (item for item in original if item["point_type"] == "1buy")
    first_index = next(index for index, unit in enumerate(values)
                        if unit.unit_id == first["price_anchor_unit_id"])
    rebound = values[first_index + 1]
    altered = (*values[:first_index + 1],
               replace(rebound, locked=False, confirmed_at=None),
               *values[first_index + 2:])
    signals = build_native_center_signals(
        altered, centers, MacdStrengthProvider(cd), as_of=cd._strict_as_of(),
    )
    assert not any(item["point_type"] == "2buy" for item in signals)


def test_center_first_return_yields_third_reminder_only_when_known():
    center = completed_up_center()
    retest = center.completion_return_unit
    assert retest is not None
    before = build_native_center_signals(
        (*center.establishment_units, retest), (center,), None,
        as_of=center.available_at - timedelta(microseconds=1),
    )
    after = build_native_center_signals(
        (*center.establishment_units, retest), (center,), None,
        as_of=center.available_at,
    )
    assert before == []
    assert [item["point_type"] for item in after] == ["3buy"]
    assert after[0]["evidence_scope"] == "native_center_signal"
    assert after[0]["missing_conditions"] == []


def test_unlocked_tail_never_issues_a_reminder():
    center = ongoing_center()
    weak_tail = unit(7, "down", 125, 95, locked=False)
    values = (*center.establishment_units, unit(5, "down", 120, 100),
              unit(6, "up", 100, 125), weak_tail)
    assert build_native_center_signals(
        values, (center,), FixedStrength(), as_of=weak_tail.available_at,
    ) == []


@pytest.mark.parametrize("fault", ["missing_entry", "unlocked_leave", "different_basis"])
def test_center_cannot_issue_points_from_missing_or_mismatched_actual_sources(fault):
    center = completed_up_center()
    sources = (*center.establishment_units, center.completion_return_unit)
    assert len(build_native_center_signals(sources, (center,), None, as_of=center.available_at)) == 1
    if fault == "missing_entry":
        sources = sources[1:]
    else:
        leave = center.completion_leave_unit
        replacement = (replace(leave, locked=False, confirmed_at=None) if fault == "unlocked_leave"
                       else replace(leave, price_basis_revision="another-price-basis"))
        sources = tuple(replacement if unit.unit_id == leave.unit_id else unit for unit in sources)
    assert not build_native_center_signals(sources, (center,), None, as_of=center.available_at)


def test_second_point_waits_for_the_available_time_of_its_complete_first_point():
    frame = pd.read_parquet(Path("tests/fixtures/segment_completion/SH_600004_5m.parquet"))
    frame.attrs.update(structure_price_quantum="0.01", price_basis_revision="recorded-raw")
    cd = build_strict_chart_cd(market="a", code="SH.600004", frequency="5m",
                               frame=frame, last_bar_closed=True).cd
    units, centers = tuple(cd.get_segment_units()), tuple(cd.get_native_centers().centers)
    strength = MacdStrengthProvider(cd)
    original = build_native_center_signals(units, centers, strength, as_of=cd._strict_as_of())
    first = next(point for point in original if point["point_type"] == "1buy")
    terminal = next(unit for unit in units if unit.unit_id == first["anchor_unit_id"])
    delayed = replace(terminal, available_at=terminal.available_at + timedelta(minutes=10))
    changed = tuple(delayed if unit.unit_id == terminal.unit_id else unit for unit in units)
    result = build_native_center_signals(changed, centers, strength, as_of=cd._strict_as_of())
    first = next(point for point in result if point["point_type"] == "1buy")
    second = next(point for point in result if point["point_type"] == "2buy")
    assert first["available_at"] >= int(delayed.available_at.timestamp())
    assert second["confirmed_at"] >= first["available_at"]
    assert second["related_unit_ids"][0] == first["price_anchor_unit_id"]
