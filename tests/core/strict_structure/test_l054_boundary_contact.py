"""L054's selected second decomposition keeps an equal-boundary return inside.

Source: D:/缠论/chanlun_lesson_corpus/
L054_一个具体走势的分析(2007-05-24013731).md:181-196.
Tick magnitudes below are synthetic and preserve the quoted ordering/equality.
The mirrored buy case is a geometric derivation, not a separately quoted chart.
"""

import pytest

from chanlun.core.strict_structure.center_machine import calculate_centers
from chanlun.core.strict_structure.models import SourceKind
from chanlun.core.strict_structure.native_center_signals import (
    build_native_center_signals,
)
from tests.core.strict_structure.helpers import unit


def selected_view(delta=0, *, mirror=False, locked=True, continuation=False):
    # g1,d2,g2,d3,g3,d4,g4; core=(d2g2,g2d3,d3g3), leave=g3d4.
    prices = [90, 60, 80, 50, 70, 40, 60 + delta]
    if continuation:
        prices.extend([35, 55])
    if mirror:
        prices = [200 - price for price in prices]
    return tuple(
        unit(
            i,
            "up" if end > start else "down",
            start,
            end,
            locked=locked or i != len(prices) - 2,
        )
        for i, (start, end) in enumerate(zip(prices, prices[1:]))
    )


@pytest.mark.parametrize("mirror", [False, True])
@pytest.mark.parametrize("delta", [-1, 0, 1])
def test_third_point_requires_a_return_strictly_outside_the_closed_core(mirror, delta):
    values = selected_view(delta, mirror=mirror)
    result = calculate_centers(values, 0, SourceKind.SEGMENT)
    (center,) = result.centers
    points = build_native_center_signals(
        values, result.centers, None, as_of=values[-1].available_at
    )
    if delta < 0:
        assert center.state.value == "completed"
        assert [p["point_type"] for p in points] == ["3buy" if mirror else "3sell"]
    else:
        assert center.state.value == "ongoing"
        assert center.completion_return_unit is None and not points
        assert center.body_units[-1] == values[-1]
        assert center.failed_departure_units == (values[-2],)


@pytest.mark.parametrize("mirror", [False, True])
def test_unfinished_touching_return_does_not_preview_a_completed_center(mirror):
    values = selected_view(mirror=mirror, locked=False)
    result = calculate_centers(values, 0, SourceKind.SEGMENT)
    assert result.centers[0].state.value == "ongoing"
    assert all(p.state.value != "completed" for p in result.previews)
    assert not build_native_center_signals(
        values, result.centers, None, as_of=values[-1].available_at
    )


@pytest.mark.parametrize("mirror", [False, True])
def test_contact_extends_the_same_core_until_a_later_independent_return_stays_outside(
    mirror,
):
    values = selected_view(mirror=mirror, continuation=True)
    at_contact = calculate_centers(values[:6], 0, SourceKind.SEGMENT)
    later = calculate_centers(values, 0, SourceKind.SEGMENT)
    (first,) = at_contact.centers
    final = next(c for c in later.centers if c.center_id == first.center_id)
    assert first.state.value == "ongoing" and final.state.value == "completed"
    assert (first.zd_tick, first.zg_tick) == (final.zd_tick, final.zg_tick)
    assert final.completion_leave_unit == values[-2]
    assert final.completion_return_unit == values[-1]
    points = build_native_center_signals(
        values, later.centers, None, as_of=values[-1].available_at
    )
    (point,) = (p for p in points if p["center_id"] == first.center_id)
    assert point["anchor_unit_id"] == values[-1].unit_id
    assert point["available_at"] == int(values[-1].available_at.timestamp())
