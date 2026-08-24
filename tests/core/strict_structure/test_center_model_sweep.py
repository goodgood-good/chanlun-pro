"""Deterministic model sweep for the strict center lifecycle.

The examples in the focused transition tests are intentionally readable.  This
module complements them with a bounded exhaustive sweep so that a scan-order or
suffix-ownership change cannot silently reintroduce any of these production
failures:

* two simultaneous unfinished centers;
* a completed center being rewritten after future units are appended;
* a completed third-class return not producing exactly one strict point;
* an equality-boundary return being lost by an accidental strict comparison.
"""

from __future__ import annotations

from decimal import Decimal
from itertools import product

from chanlun.core.strict_structure.center_machine import calculate_centers
from chanlun.core.strict_structure.models import (
    CenterPreviewState,
    CenterState,
    SourceKind,
    StrictLevelResult,
    StrictStructureResult,
)
from chanlun.core.strict_structure.signals import StrictSignalEngine

from tests.core.strict_structure.helpers import TEST_PRICE_BASIS, unit


def _alternating_walk(
    first_direction: str,
    steps: tuple[int, ...],
):
    tick = 100
    direction = first_direction
    values = []
    for index, step in enumerate(steps):
        next_tick = tick + step if direction == "up" else tick - step
        values.append(unit(index, direction, tick, next_tick))
        tick = next_tick
        direction = "down" if direction == "up" else "up"
    return tuple(values)


def test_bounded_segment_walks_preserve_center_and_third_point_invariants() -> None:
    """Exhaust 512 connected walks without allowing lifecycle ambiguity."""

    checked = 0
    completed_count = 0
    for first_direction in ("up", "down"):
        for steps in product((1, 4), repeat=8):
            values = _alternating_walk(first_direction, steps)
            final = calculate_centers(values, 0, SourceKind.SEGMENT)

            assert sum(
                center.state is CenterState.ONGOING
                for center in final.centers
            ) <= 1
            assert sum(
                preview.state is CenterPreviewState.FORMING
                for preview in final.previews
            ) <= 1

            final_by_id = {center.center_id: center for center in final.centers}
            assert len(final_by_id) == len(final.centers)
            for prefix_end in range(5, len(values) + 1):
                prefix = calculate_centers(
                    values[:prefix_end],
                    0,
                    SourceKind.SEGMENT,
                )
                for center in prefix.centers:
                    if center.state is CenterState.COMPLETED:
                        assert final_by_id.get(center.center_id) == center

            structure = StrictStructureResult(
                schema="chanlun-structure",
                price_basis_revision=TEST_PRICE_BASIS,
                levels=(
                    StrictLevelResult(
                        structural_level=0,
                        units=values,
                        center_result=final,
                        trend_types=(),
                        completed_trends=(),
                    ),
                ),
            )
            points = StrictSignalEngine(
                structure=structure,
                price_quantum=Decimal("1"),
            ).third_class_points()
            completed_ids = {
                center.center_id
                for center in final.centers
                if center.state is CenterState.COMPLETED
            }
            assert {point.center_id for point in points} == completed_ids

            for center in final.centers:
                assert len(center.core_units) == 3
                assert center.body_units[:3] == center.core_units
                assert center.entry_unit is not None
                assert center.establishment_leave_unit is not None
                assert len(center.establishment_units) == 5
                assert center.entry_unit not in center.body_units
                assert not set(center.failed_departure_units).intersection(
                    center.body_units
                )
                assert (
                    center.entry_unit.end_tick
                    == center.core_units[0].start_tick
                )
                if center.pending_leave_unit is not None:
                    assert center.pending_leave_unit not in center.body_units
                    assert (
                        center.pending_leave_unit.end_tick > center.zg_tick
                        if center.pending_leave_unit.direction == "up"
                        else center.pending_leave_unit.end_tick < center.zd_tick
                    )
                if center.state is not CenterState.COMPLETED:
                    continue
                leave = center.completion_leave_unit
                ret = center.completion_return_unit
                assert leave is not None and ret is not None
                assert leave not in center.body_units
                if leave.direction == "up":
                    assert ret.direction == "down"
                    assert ret.low_tick >= center.zg_tick
                else:
                    assert ret.direction == "up"
                    assert ret.high_tick <= center.zd_tick

            checked += 1
            completed_count += len(completed_ids)

    assert checked == 512
    # The five-role gate deliberately removes the former three-role centers.
    # Dedicated transition tests cover failed-leave re-arming; this sweep pins
    # the bounded population produced by the stricter establishment rule.
    assert completed_count == 168
