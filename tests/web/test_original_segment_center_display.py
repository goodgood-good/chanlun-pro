"""The default chart center overlay must use the original three-part core."""

from __future__ import annotations

from decimal import Decimal

from tests.web.test_segment_five_role_centers import _segment


def _three_sell_lines():
    return [
        _segment(0, "up", 90, 110),
        _segment(1, "down", 110, 100),
        _segment(2, "up", 100, 115),
        _segment(3, "down", 115, 90),
        _segment(4, "up", 90, 99),
    ]


def test_three_locked_segments_are_visible_as_one_ongoing_center() -> None:
    from chanlun.cl_utils.tv_chart import xd_segment_centers_to_chart_dicts

    lines = _three_sell_lines()[:3]
    payloads = xd_segment_centers_to_chart_dicts(lines)

    assert len(payloads) == 1
    center = payloads[0]
    assert center["center_state"] == "ongoing"
    assert center["core_line_count"] == 3
    assert center["first_three_component_ids"]
    assert [point["price"] for point in center["points"]] == [110.0, 100.0]


def test_fifth_segment_confirms_three_sell_instead_of_forming_new_center() -> None:
    from chanlun.cl_utils.tv_chart import xd_segment_centers_to_chart_dicts

    lines = _three_sell_lines()
    lines[4].line_mmds = lambda *_args, **_kwargs: ["3sell"]
    payloads = xd_segment_centers_to_chart_dicts(lines)

    assert len(payloads) == 1
    center = payloads[0]
    assert center["center_state"] == "completed"
    assert center["completion_point_type"] == "3sell"
    assert center["completion_return_segment"]["start_time"] == int(
        lines[4].start.k.date.timestamp()
    )
    assert center["done"] is True
    assert center["provisional"] is False


def test_pen_center_uses_the_same_first_return_completion_lifecycle() -> None:
    from chanlun.cl_utils.tv_chart import bi_stroke_centers_to_chart_dicts

    centers = bi_stroke_centers_to_chart_dicts(_three_sell_lines())

    assert len(centers) == 1
    center = centers[0]
    assert center["tower"] == "bi"
    assert center["algorithm_revision"] == "chanlun-display-bi-original-three/v1"
    assert center["center_state"] == "completed"
    assert center["completion_point_type"] == "3sell"
    assert center["done"] is True
    assert center["tradable"] is False


def test_public_observation_adapter_ignores_legacy_five_role_geometry() -> None:
    from chanlun.cl_utils.strict_chart import (
        display_segment_center_observations_to_chart_dicts,
    )

    lines = _three_sell_lines()[:3]
    payloads = display_segment_center_observations_to_chart_dicts(
        (object(),),
        lines,
        price_basis_revision="test-display-original-three/v1",
        price_quantum=Decimal("1"),
        as_of=lines[-1].end.k.date,
    )

    assert len(payloads) == 1
    center = payloads[0]
    assert center["algorithm_revision"] == "chanlun-display-xd-original-three/v1"
    assert center["entering_segment"] is None
    assert len(center["first_three_component_ids"]) == 3
    assert center["core"] == {"zd_tick": 100, "zg_tick": 110}


def test_qqq_reentry_clears_stale_pending_leave_and_phantom_line() -> None:
    from chanlun.cl_utils.tv_chart import xd_segment_centers_to_chart_dicts

    prices = (
        ("down", 721.237, 694.506, True),
        ("up", 694.506, 745.400, True),
        ("down", 745.400, 685.616, True),
        ("up", 685.616, 745.430, True),
        ("down", 745.430, 704.450, True),
        ("up", 704.450, 737.620, True),
        ("down", 737.620, 700.910, True),
        ("up", 700.910, 726.390, True),
        ("down", 726.390, 686.760, False),
        ("up", 686.760, 710.050, False),
        ("down", 710.050, 663.300, False),
        ("up", 663.300, 701.590, False),
    )
    lines = [
        _segment(index, direction, start, end, done=done)
        for index, (direction, start, end, done) in enumerate(prices)
    ]

    center = xd_segment_centers_to_chart_dicts(lines)[0]
    assert center["center_state"] == "forming"
    assert center["type"] == "zd"
    assert center["leaving_segment"] is None
    assert center["completion_phase"] == "AWAITING_SAME_LEVEL_DEPARTURE"
