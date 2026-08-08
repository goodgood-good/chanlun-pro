"""默认图表中枢必须服从连续五线段共同重叠合同。"""

from __future__ import annotations

from decimal import Decimal

from tests.web.test_segment_five_role_centers import _segment


def _five_role_three_sell_lines():
    return [
        _segment(0, "down", 130, 100),
        _segment(1, "up", 100, 120),
        _segment(2, "down", 120, 105),
        _segment(3, "up", 105, 115),
        _segment(4, "down", 115, 80),
        _segment(5, "up", 80, 100),
    ]


def test_three_or_four_locked_segments_never_draw_formal_center() -> None:
    from chanlun.cl_utils.tv_chart import xd_segment_centers_to_chart_dicts

    lines = _five_role_three_sell_lines()

    assert xd_segment_centers_to_chart_dicts(lines[:3]) == []
    four_role = xd_segment_centers_to_chart_dicts(lines[:4])
    assert len(four_role) == 1
    assert four_role[0]["render_kind"] == "center_preview"
    assert four_role[0]["provisional"] is True
    assert four_role[0]["tradable"] is False
    assert four_role[0]["establishment_component_count"] == 4


def test_five_segments_draw_frozen_middle_three_core() -> None:
    from chanlun.cl_utils.tv_chart import xd_segment_centers_to_chart_dicts

    lines = _five_role_three_sell_lines()[:5]
    center = xd_segment_centers_to_chart_dicts(lines)[0]

    assert center["center_state"] == "ongoing"
    assert center["entering_segment"]["start_time"] == int(
        lines[0].start.k.date.timestamp()
    )
    assert center["entry_role"] == "external_entry"
    assert center["overlap_component_count"] == 5
    assert center["core_line_count"] == 3
    # 进入段 130→100 不属于中枢本体；核心由首三个本体线段冻结。
    assert [point["price"] for point in center["points"]] == [115.0, 105.0]
    assert center["leaving_segment"]["direction"] == "down"


def test_sixth_segment_confirms_three_sell_instead_of_forming_new_center() -> None:
    from chanlun.cl_utils.tv_chart import xd_segment_centers_to_chart_dicts

    lines = _five_role_three_sell_lines()
    lines[5].line_mmds = lambda *_args, **_kwargs: ["3sell"]
    center = xd_segment_centers_to_chart_dicts(lines)[0]

    assert center["center_state"] == "completed"
    assert center["completion_point_type"] == "3sell"
    assert center["completion_return_segment"]["start_time"] == int(
        lines[5].start.k.date.timestamp()
    )
    assert center["done"] is True
    assert center["provisional"] is False


def test_pen_center_uses_the_same_entry_core_leave_lifecycle() -> None:
    from chanlun.cl_utils.tv_chart import bi_stroke_centers_to_chart_dicts

    centers = bi_stroke_centers_to_chart_dicts(_five_role_three_sell_lines())

    assert len(centers) == 1
    center = centers[0]
    assert center["tower"] == "bi"
    assert center["algorithm_revision"] == "chanlun-display-bi-five-role/v10"
    assert center["center_state"] == "completed"
    assert center["completion_point_type"] == "3sell"
    assert center["done"] is True
    assert center["tradable"] is False


def test_public_observation_adapter_uses_same_entry_core_leave_geometry() -> None:
    from chanlun.cl_utils.strict_chart import (
        display_segment_center_observations_to_chart_dicts,
    )

    lines = _five_role_three_sell_lines()[:5]
    payloads = display_segment_center_observations_to_chart_dicts(
        (object(),),
        lines,
        price_basis_revision="test-display-five-overlap/v1",
        price_quantum=Decimal("1"),
        as_of=lines[-1].end.k.date,
    )

    assert len(payloads) == 1
    center = payloads[0]
    assert center["algorithm_revision"] == "chanlun-display-xd-five-role/v10"
    assert center["entering_segment"] is not None
    assert center["entry_role"] == "external_entry"
    assert center["overlap_component_count"] == 5
    assert len(center["first_three_component_ids"]) == 3
    assert center["core"] == {"zd_tick": 105, "zg_tick": 115}


def test_qqq_shifted_preview_does_not_displace_active_mature_center() -> None:
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
    assert center["center_state"] == "ongoing"
    assert center["type"] == "zd"
    assert center["completion_phase"] == "AWAITING_SAME_LEVEL_DEPARTURE"
    assert center["leaving_segment"] is None
    assert center["completion_return_segment"] is None
    assert center["suppressed_overlapping_candidate_count"] == 1
    # The earliest five-unit center owns the full overlapping oscillation;
    # an internal narrower preview cannot rewrite that established identity.
    assert center["points"][0]["time"] == int(lines[1].start.k.date.timestamp())
    assert center["points"][1]["time"] == int(lines[7].end.k.date.timestamp())
