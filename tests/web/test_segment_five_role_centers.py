"""Displayed line centers obey the same five-role state machine as screening."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace


BASE = datetime(2026, 7, 1, 9, 30, tzinfo=timezone(timedelta(hours=8)))


def _segment(index, direction, start, end, *, done=True):
    start_time = BASE + timedelta(minutes=index)
    end_time = BASE + timedelta(minutes=index + 1)
    line = SimpleNamespace(
        index=index,
        type=direction,
        start=SimpleNamespace(val=start, k=SimpleNamespace(date=start_time, k_index=index)),
        end=SimpleNamespace(val=end, k=SimpleNamespace(date=end_time, k_index=index + 1)),
        locked_at=end_time if done else None,
        zs_low=min(start, end),
        zs_high=max(start, end),
    )
    line.is_done = lambda: done
    line.line_mmds = lambda *_args, **_kwargs: []
    return line


def _upward_center_segments():
    return [
        _segment(0, "up", 90, 120),
        _segment(1, "down", 120, 100),
        _segment(2, "up", 100, 115),
        _segment(3, "down", 115, 105),
        _segment(4, "up", 105, 130),
    ]


def _downward_center_segments():
    return [
        _segment(0, "down", 130, 100),
        _segment(1, "up", 100, 120),
        _segment(2, "down", 120, 105),
        _segment(3, "up", 105, 115),
        _segment(4, "down", 115, 80),
    ]


def test_upward_five_segments_keep_entry_and_leave_outside_middle_body():
    from chanlun.cl_utils.tv_chart import xd_segment_centers_to_chart_dicts

    lines = _upward_center_segments()
    center = xd_segment_centers_to_chart_dicts(lines)[0]

    assert center["type"] == "up"
    assert center["entry_role"] == "external_entry"
    assert center["entering_segment"]["direction"] == "up"
    assert center["core_directions"] == ["down", "up", "down"]
    assert center["overlap_component_count"] == 5
    assert center["leaving_segment"]["direction"] == "up"
    assert [point["price"] for point in center["points"]] == [115.0, 105.0]
    assert center["points"][0]["time"] == int(lines[1].start.k.date.timestamp())
    assert center["points"][1]["time"] == int(lines[3].end.k.date.timestamp())
    assert center["establishment_component_count"] == 5


def test_downward_five_segments_expose_the_real_leave_direction():
    from chanlun.cl_utils.tv_chart import xd_segment_centers_to_chart_dicts

    center = xd_segment_centers_to_chart_dicts(_downward_center_segments())[0]
    assert center["type"] == "down"
    assert center["entering_segment"]["direction"] == "down"
    assert center["core_directions"] == ["up", "down", "up"]
    assert center["overlap_component_count"] == 5
    assert center["leaving_segment"]["direction"] == "down"


def test_four_segments_draw_non_tradable_preview_before_five_segment_maturity():
    from chanlun.cl_utils.tv_chart import xd_segment_centers_to_chart_dicts

    lines = _upward_center_segments()[:-1]
    centers = xd_segment_centers_to_chart_dicts(lines)

    assert len(centers) == 1
    center = centers[0]
    assert center["center_state"] == "forming"
    assert center["provisional"] is True
    assert center["tradable"] is False
    assert center["linestyle"] == "1"
    assert center["completion_phase"] == "AWAITING_MATURITY_SEGMENT"
    assert center["establishment_component_count"] == 4
    assert center["establishment_unit_id"] is None
    assert center["overlap_component_count"] == 4
    assert center["contains_unfinished_segment"] is False


def test_fifth_segment_inside_core_matures_as_first_extension():
    from chanlun.cl_utils.tv_chart import xd_segment_centers_to_chart_dicts

    lines = _upward_center_segments()
    lines[-1] = _segment(4, "up", 105, 110)

    center = xd_segment_centers_to_chart_dicts(lines)[0]

    assert center["center_state"] == "ongoing"
    assert center["type"] == "zd"
    assert center["leaving_segment"] is None
    assert center["line_count"] == 4
    assert center["lifecycle_role_count"] == 5
    assert center["body_components"][-1]["end_price"] == 110
    assert center["establishment_unit_id"] == center[
        "establishment_segment_ids"
    ][-1]
    assert center["points"][1]["time"] == int(lines[4].end.k.date.timestamp())


def test_unfinished_fifth_body_is_one_non_tradable_preview():
    from chanlun.cl_utils.tv_chart import xd_segment_centers_to_chart_dicts

    lines = _upward_center_segments()
    lines[-1] = _segment(4, "up", 105, 130, done=False)
    payloads = xd_segment_centers_to_chart_dicts(lines)

    assert len(payloads) == 1
    center = payloads[0]
    assert center["center_state"] == "forming"
    assert center["render_kind"] == "center_preview"
    assert center["overlap_component_count"] == 5
    assert center["leaving_segment"]["direction"] == "up"
    assert center["provisional"] is True
    assert center["tradable"] is False
    assert center["algorithm_revision"] == "chanlun-display-xd-five-role/v10"


def test_later_leave_and_first_outside_return_complete_three_buy():
    from chanlun.cl_utils.tv_chart import xd_segment_centers_to_chart_dicts

    lines = _upward_center_segments() + [_segment(5, "down", 130, 120)]
    center = xd_segment_centers_to_chart_dicts(lines)[0]

    assert center["done"] is True
    assert center["center_state"] == "completed"
    assert center["overlap_component_count"] == 5
    assert center["leaving_segment"]["start_time"] == int(lines[4].start.k.date.timestamp())
    assert center["completion_return_segment"]["start_time"] == int(lines[5].start.k.date.timestamp())
    assert center["completion_point_type"] == "3buy"
    assert center["completion_point_status"] == "confirmed"
    assert center["associated_points"] == ["3buy"]


def test_later_leave_and_first_outside_return_complete_three_sell():
    from chanlun.cl_utils.tv_chart import xd_segment_centers_to_chart_dicts

    lines = _downward_center_segments() + [_segment(5, "up", 80, 100)]
    lines[5].line_mmds = lambda *_args, **_kwargs: ["3sell"]
    center = xd_segment_centers_to_chart_dicts(lines)[0]

    assert center["done"] is True
    assert center["type"] == "down"
    assert center["completion_point_type"] == "3sell"
    assert center["completion_point_observed"] is True
    assert center["leaving_segment"]["direction"] == "down"
    assert center["completion_return_segment"]["direction"] == "up"


def test_unlocked_return_completes_geometry_but_never_becomes_tradable():
    from chanlun.cl_utils.tv_chart import xd_segment_centers_to_chart_dicts

    lines = _upward_center_segments() + [
        _segment(5, "down", 130, 120, done=False),
    ]
    center = xd_segment_centers_to_chart_dicts(lines)[0]

    assert center["center_state"] == "completed"
    assert center["completion_phase"] == "GEOMETRIC_THIRD_CLASS_POINT"
    assert center["provisional"] is True
    assert center["done"] is False
    assert center["tradable"] is False
    assert center["linestyle"] == "0"


def test_failed_leave_and_return_extend_same_center_without_second_box():
    from chanlun.cl_utils.tv_chart import xd_segment_centers_to_chart_dicts

    lines = _upward_center_segments() + [
        _segment(5, "down", 130, 110),
        _segment(6, "up", 110, 125, done=False),
    ]
    centers = xd_segment_centers_to_chart_dicts(lines)

    assert len(centers) == 1
    center = centers[0]
    assert center["center_state"] == "forming"
    assert center["overlap_component_count"] == 7
    assert center["entering_segment"]["direction"] == "up"
    assert (center["zd"], center["zg"]) == (105.0, 115.0)


def test_qqq_30m_regression_has_one_owner_and_no_sub_five_body_payload():
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
    lines = [_segment(i, d, a, b, done=done) for i, (d, a, b, done) in enumerate(prices)]
    centers = xd_segment_centers_to_chart_dicts(lines)

    assert len(centers) == 1
    assert centers[0]["overlap_component_count"] >= 5
    assert centers[0]["algorithm_revision"] == "chanlun-display-xd-five-role/v10"


def test_sh513100_regression_never_draws_two_unresolved_centers():
    from chanlun.cl_utils.tv_chart import xd_segment_centers_to_chart_dicts

    prices = (
        ("down", 2.192, 2.103, True),
        ("up", 2.103, 2.118, True),
        ("down", 2.118, 2.094, True),
        ("up", 2.094, 2.153, True),
        ("down", 2.153, 2.095, True),
        ("up", 2.095, 2.122, False),
        ("down", 2.122, 2.035, False),
        ("up", 2.035, 2.186, False),
    )
    lines = [_segment(i, d, a, b, done=done) for i, (d, a, b, done) in enumerate(prices)]
    centers = xd_segment_centers_to_chart_dicts(lines)

    unresolved = [item for item in centers if item["center_state"] != "completed"]
    assert len(unresolved) <= 1
    assert all(item["overlap_component_count"] >= 5 for item in centers)


def test_entry_and_departure_overlap_price_core_but_are_outside_time_range():
    from chanlun.cl_utils.tv_chart import xd_segment_centers_to_chart_dicts

    lines = _upward_center_segments()
    center = xd_segment_centers_to_chart_dicts(lines)[0]

    assert center["overlap_component_count"] == 5
    assert center["entering_segment"]["end_time"] == center["points"][0]["time"]
    assert center["leaving_segment"]["start_time"] == center["points"][1]["time"]
    assert center["entering_segment"] in center["overlap_components"]
    assert center["leaving_segment"] in center["overlap_components"]
    assert center["display_range"]["includes_entry"] is False
    assert center["display_range"]["includes_leave"] is False


def test_shared_lifecycle_leg_does_not_collapse_adjacent_center_bodies():
    from chanlun.cl_utils.tv_chart import (
        _collapse_overlapping_xd_center_candidates,
    )

    left = {
        "center_id": "left",
        "points": [{"time": 2}, {"time": 5}],
        "body_components": [
            {"start_time": 2, "end_time": 5},
        ],
        "overlap_components": [
            {"start_time": 0, "end_time": 2},
            {"start_time": 2, "end_time": 5},
            {"start_time": 5, "end_time": 8},
        ],
        "center_state": "completed",
        "done": True,
    }
    right = {
        "center_id": "right",
        "points": [{"time": 8}, {"time": 11}],
        "body_components": [
            {"start_time": 8, "end_time": 11},
        ],
        "overlap_components": [
            {"start_time": 5, "end_time": 8},
            {"start_time": 8, "end_time": 11},
            {"start_time": 11, "end_time": 14},
        ],
        "center_state": "ongoing",
        "done": False,
    }

    values = _collapse_overlapping_xd_center_candidates([left, right])

    assert [value["center_id"] for value in values] == ["left", "right"]


def test_tsla_1m_regression_reuses_completed_leave_as_next_center_entry():
    from chanlun.cl_utils.tv_chart import xd_segment_centers_to_chart_dicts

    # Exact segment vertices from the TSLA.US 1m production snapshot that
    # exposed alternating missing centers. Centers 4 and 5 share segment 29:
    # it is the former center's leave and the successor's external entry.
    vertices = (
        310.452, 306.91, 308.79, 305.44, 309.34, 300.69, 311.15,
        301.73, 303.67, 299.7, 307.99, 297.5, 309.12, 304.15,
        308.48, 306.8, 315.5, 301.97, 310.16, 305.42, 310.97,
        309.82, 312.153, 309.76, 324.2, 322.51, 324.08, 321.02,
        326.9, 320.79, 325.79, 324.33, 327.45, 325.05, 329.57,
        320.43, 327.14, 321.0, 324.41, 320.4, 322.793,
    )
    lines = [
        _segment(
            index,
            "up" if end > start else "down",
            start,
            end,
            done=index < len(vertices) - 2,
        )
        for index, (start, end) in enumerate(zip(vertices, vertices[1:]))
    ]

    centers = xd_segment_centers_to_chart_dicts(lines)

    assert len(centers) == 7
    assert [center["center_state"] for center in centers] == [
        "completed",
        "completed",
        "completed",
        "completed",
        "completed",
        "completed",
        "forming",
    ]
    assert centers[4]["leaving_segment"] == centers[5]["entering_segment"]
    assert centers[5]["entering_segment"]["start_time"] == int(
        lines[29].start.k.date.timestamp()
    )
    assert (centers[5]["zd"], centers[5]["zg"]) == (325.05, 325.79)
    assert centers[5]["leaving_segment"] == centers[6]["entering_segment"]
    assert centers[6]["entering_segment"]["start_time"] == int(
        lines[36].start.k.date.timestamp()
    )
    assert (centers[6]["zd"], centers[6]["zg"]) == (321.0, 322.793)
    assert centers[6]["establishment_component_count"] == 4
    assert centers[6]["establishment_unit_id"] is None
    assert centers[6]["completion_phase"] == "AWAITING_MATURITY_SEGMENT"
    assert centers[6]["contains_unfinished_segment"] is True
    assert centers[6]["tradable"] is False
    assert all(
        center["suppressed_overlapping_candidate_count"] == 0
        for center in centers
    )
