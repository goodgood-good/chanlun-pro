from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace


BASE = datetime(2026, 7, 1, 9, 30, tzinfo=timezone(timedelta(hours=8)))


def _segment(
    index: int,
    direction: str,
    start: float,
    end: float,
    *,
    done: bool = True,
):
    start_time = BASE + timedelta(minutes=index)
    end_time = BASE + timedelta(minutes=index + 1)
    start_fx = SimpleNamespace(
        val=start,
        k=SimpleNamespace(date=start_time, k_index=index),
    )
    end_fx = SimpleNamespace(
        val=end,
        k=SimpleNamespace(date=end_time, k_index=index + 1),
    )
    line = SimpleNamespace(
        index=index,
        type=direction,
        start=start_fx,
        end=end_fx,
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
        _segment(2, "down", 120, 90),
        _segment(3, "up", 90, 110),
        _segment(4, "down", 110, 80),
    ]


def test_upward_center_uses_first_three_then_leave() -> None:
    from chanlun.cl_utils.tv_chart import xd_segment_centers_to_chart_dicts

    lines = _upward_center_segments()
    payloads = xd_segment_centers_to_chart_dicts(lines)

    assert len(payloads) == 1
    center = payloads[0]
    assert center["type"] == "up"
    assert center["core_directions"] == ["up", "down", "up"]
    assert center["entering_segment"] is None
    assert center["leaving_segment"]["direction"] == "up"
    assert [point["price"] for point in center["points"]] == [115.0, 100.0]
    assert center["points"][0]["time"] == int(
        lines[0].start.k.date.timestamp()
    )
    assert center["points"][1]["time"] == int(
        lines[4].start.k.date.timestamp()
    )


def test_downward_center_uses_first_three_then_leave() -> None:
    from chanlun.cl_utils.tv_chart import xd_segment_centers_to_chart_dicts

    lines = _downward_center_segments()
    payloads = xd_segment_centers_to_chart_dicts(lines)

    assert len(payloads) == 1
    center = payloads[0]
    assert center["type"] == "down"
    assert center["core_directions"] == ["down", "up", "down"]
    assert center["entering_segment"] is None
    assert center["leaving_segment"]["direction"] == "down"
    assert [point["price"] for point in center["points"]] == [120.0, 100.0]


def test_fifth_segment_is_retained_as_ongoing_leave_leg_inside_core() -> None:
    from chanlun.cl_utils.tv_chart import xd_segment_centers_to_chart_dicts

    lines = _upward_center_segments()
    lines[-1] = _segment(4, "up", 105, 110)

    center = xd_segment_centers_to_chart_dicts(lines)[0]

    assert center["done"] is False
    assert center["center_state"] == "ongoing"
    assert center["completion_phase"] == "AWAITING_SAME_LEVEL_DEPARTURE"
    assert center["confirmation_scope"] == "xd"
    assert center["completion_point_type"] is None
    assert center["completion_point_status"] is None
    assert center["completion_return_segment"] is None
    assert center["leaving_segment"] is None


def test_unfinished_fifth_segment_participates_in_center_recognition() -> None:
    from chanlun.cl_utils.tv_chart import xd_segment_centers_to_chart_dicts

    lines = _upward_center_segments()
    lines[-1] = _segment(4, "up", 105, 130, done=False)

    payloads = xd_segment_centers_to_chart_dicts(lines)

    assert len(payloads) == 1
    center = payloads[0]
    assert center["type"] == "up"
    assert center["core_directions"] == ["up", "down", "up"]
    assert center["leaving_segment"]["start_time"] == int(
        lines[-1].start.k.date.timestamp()
    )
    assert center["leaving_segment"]["end_price"] == 130
    assert center["done"] is False
    assert center["linestyle"] == "1"
    assert center["center_state"] == "forming"
    assert center["provisional"] is True
    assert center["contains_unfinished_segment"] is True
    assert center["algorithm_revision"] == "chanlun-display-xd-original-three/v1"


def test_unfinished_fifth_segment_participates_for_downward_center() -> None:
    from chanlun.cl_utils.tv_chart import xd_segment_centers_to_chart_dicts

    lines = _downward_center_segments()
    lines[-1] = _segment(4, "down", 110, 80, done=False)

    center = xd_segment_centers_to_chart_dicts(lines)[0]

    assert center["type"] == "down"
    assert center["core_directions"] == ["down", "up", "down"]
    assert center["leaving_segment"]["direction"] == "down"
    assert center["leaving_segment"]["end_price"] == 80
    assert center["provisional"] is True
    assert center["done"] is False


def test_unfinished_extension_can_become_the_current_leaving_segment() -> None:
    from chanlun.cl_utils.tv_chart import xd_segment_centers_to_chart_dicts

    lines = _upward_center_segments()
    lines[4] = _segment(4, "up", 105, 110)
    lines.extend(
        [
            _segment(5, "down", 110, 104),
            _segment(6, "up", 104, 130, done=False),
        ]
    )

    centers = xd_segment_centers_to_chart_dicts(lines)
    live_center = centers[-1]

    assert live_center["leaving_segment"]["start_time"] == int(
        lines[-1].start.k.date.timestamp()
    )
    assert live_center["leaving_segment"]["end_price"] == 130
    assert live_center["core_directions"] == ["up", "down", "up"]
    assert live_center["provisional"] is True
    assert live_center["done"] is False


def test_three_legs_after_active_leave_do_not_draw_shifted_center() -> None:
    """Do not move the old leaving segment into the next center's core."""

    from chanlun.cl_utils.tv_chart import xd_segment_centers_to_chart_dicts

    lines = _upward_center_segments()
    lines.extend(
        [
            _segment(5, "down", 130, 120, done=False),
            _segment(6, "up", 120, 128, done=False),
            _segment(7, "down", 128, 122, done=False),
        ]
    )

    centers = xd_segment_centers_to_chart_dicts(lines)

    assert len(centers) == 1
    # The only live box is the projection of the existing center.  Its fixed
    # core remains U1-U3; the separated U5-U7 tail must not become [120, 128].
    assert centers[0]["render_kind"] == "center_preview"
    assert centers[0]["provisional"] is True
    assert centers[0]["tradable"] is False
    assert (centers[0]["zd"], centers[0]["zg"]) == (100.0, 115.0)
    assert centers[0]["entering_segment"] is None
    assert centers[0]["leaving_segment"]["start_time"] == int(
        lines[4].start.k.date.timestamp()
    )


def test_adjacent_preview_sharing_only_boundary_keeps_both_centers() -> None:
    """A genuinely adjacent candidate must not erase the prior center."""

    from chanlun.cl_utils.tv_chart import xd_segment_centers_to_chart_dicts

    lines = _upward_center_segments()
    lines.extend(
        [
            _segment(5, "down", 130, 120, done=False),
            _segment(6, "up", 120, 140, done=False),
            _segment(7, "down", 140, 125, done=False),
            _segment(8, "up", 125, 150, done=False),
        ]
    )

    centers = xd_segment_centers_to_chart_dicts(lines)

    assert [item["render_kind"] for item in centers] == ["center_preview"]
    # The first provisional return has already completed the old center's
    # same-level 3-buy geometry.  The later adjacent candidate may coexist,
    # but cannot regress that evidence back to ordinary "forming".
    assert centers[0]["center_state"] == "completed"
    assert centers[0]["completion_phase"] == "GEOMETRIC_THIRD_CLASS_POINT"
    assert centers[0]["completion_point_type"] == "3buy"
    assert centers[0]["completion_return_segment"]["start_time"] == int(
        lines[5].start.k.date.timestamp()
    )
    # A provisional completion owns the live suffix exclusively; a shifted
    # forming candidate must not recreate the two-unfinished-centers bug.


def test_overlapping_later_preview_is_folded_into_active_center_extension() -> None:
    """A candidate born inside an active center is not a second center.

    This is the reduced QQQ 30m sequence observed on 2026-08-04.  The later
    five-role candidate starts before the active center's displayed body has
    ended and its price core overlaps the active core.  Rendering both makes
    one same-level extension look like two simultaneous centers.
    """

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

    centers = xd_segment_centers_to_chart_dicts(lines)

    assert len(centers) == 1
    assert centers[0]["render_kind"] == "center_preview"
    assert centers[0]["center_state"] == "forming"
    assert centers[0]["suppressed_overlapping_candidate_count"] == 0
    assert centers[0]["algorithm_revision"] == "chanlun-display-xd-original-three/v1"
    assert centers[0]["leaving_segment"] is None
    assert not (centers[0]["done"] and centers[0]["provisional"])


def test_sh513100_unresolved_extension_draws_one_active_center() -> None:
    """A provisional re-entry extends the active center, not a second box."""

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
    lines = [
        _segment(index, direction, start, end, done=done)
        for index, (direction, start, end, done) in enumerate(prices)
    ]

    centers = xd_segment_centers_to_chart_dicts(lines)

    assert len(centers) == 1
    center = centers[0]
    assert center["type"] == "up"
    assert center["center_state"] == "forming"
    assert center["render_kind"] == "center_preview"
    assert center["line_count"] == 8
    assert (center["zd"], center["zg"]) == (2.103, 2.118)
    assert center["entering_segment"] is None
    assert center["leaving_segment"]["direction"] == "up"
    assert center["leaving_segment"]["start_price"] == 2.035
    assert center["leaving_segment"]["end_price"] == 2.186
    assert center["provisional"] is True
    assert center["tradable"] is False


def test_rejected_active_projection_does_not_draw_shifted_forming_center() -> None:
    """A provisional tail cannot coexist with an unresolved formal center."""

    from chanlun.cl_utils.tv_chart import xd_segment_centers_to_chart_dicts

    prices = (
        ("down", 10.61, 7.07, True),
        ("up", 7.07, 9.82, True),
        ("down", 9.82, 8.41, True),
        ("up", 8.41, 11.06, True),
        ("down", 11.06, 9.52, True),
        ("up", 9.52, 12.73, True),
        ("down", 12.73, 10.67, False),
        ("up", 10.67, 11.63, False),
        ("down", 11.63, 9.99, False),
    )
    lines = [
        _segment(index, direction, start, end, done=done)
        for index, (direction, start, end, done) in enumerate(prices)
    ]

    centers = xd_segment_centers_to_chart_dicts(lines)

    assert len(centers) == 1
    assert centers[0]["render_kind"] == "center_preview"
    assert centers[0]["center_state"] == "completed"
    assert centers[0]["type"] == "up"
    assert (centers[0]["zd"], centers[0]["zg"]) == (8.41, 9.82)
    assert centers[0]["leaving_segment"]["direction"] == "up"


def test_completed_three_sell_preview_supersedes_overlapping_ongoing_center() -> None:
    """SZ.300826 5m: completed 3-sell geometry must not regress to forming.

    The first window is an ongoing upward center.  Shifting one segment gives
    a downward center whose leave and first return are already present.  The
    two display bodies overlap, but retaining the older ongoing object would
    hide the 3-sell and even splice a down leave into an up center.
    """

    from chanlun.cl_utils.tv_chart import xd_segment_centers_to_chart_dicts

    prices = (
        ("up", 12.01, 12.96, True),
        ("down", 12.96, 11.92, True),
        ("up", 11.92, 12.65, True),
        ("down", 12.65, 10.02, True),
        ("up", 10.02, 12.11, True),
        ("down", 12.11, 11.33, False),
        ("up", 11.33, 11.82, False),
        ("down", 11.82, 11.19, False),
    )
    lines = [
        _segment(index, direction, start, end, done=done)
        for index, (direction, start, end, done) in enumerate(prices)
    ]
    lines[6].line_mmds = lambda *_args, **_kwargs: ["3sell"]

    centers = xd_segment_centers_to_chart_dicts(lines)

    assert len(centers) == 1
    center = centers[0]
    assert center["center_state"] == "completed"
    assert center["completion_phase"] == "GEOMETRIC_THIRD_CLASS_POINT"
    assert center["completion_point_type"] == "3sell"
    assert center["completion_point_status"] == "provisional"
    assert center["completion_point_observed"] is True
    assert center["associated_points"] == ["3sell"]
    assert center["type"] == "down"
    assert center["entering_segment"] is None
    assert center["leaving_segment"]["direction"] == "down"
    assert center["completion_return_segment"]["direction"] == "up"
    assert center["linestyle"] == "0"
    assert center["done"] is False
    assert center["provisional"] is True
    assert center["suppressed_overlapping_candidate_count"] == 0


def test_unfinished_confirmation_uses_locked_leave_but_stays_provisional() -> None:
    from chanlun.cl_utils.tv_chart import xd_segment_centers_to_chart_dicts

    lines = _upward_center_segments()
    lines.append(_segment(5, "down", 130, 120, done=False))

    center = xd_segment_centers_to_chart_dicts(lines)[0]

    assert center["state"] == "completed"
    assert center["leaving_segment"]["start_time"] == int(
        lines[4].start.k.date.timestamp()
    )
    assert center["leaving_segment"]["end_price"] == 130
    assert center["contains_unfinished_segment"] is True
    assert center["provisional"] is True
    assert center["done"] is False
    # 三类点几何已经成立，图上用实线；正式性仍由 done/provisional 区分。
    assert center["linestyle"] == "0"
    assert center["completion_phase"] == "GEOMETRIC_THIRD_CLASS_POINT"
    assert center["completion_point_type"] == "3buy"
    assert center["expected_completion_point_type"] == "3buy"
    assert center["completion_point_status"] == "provisional"
    assert center["associated_points"] == ["3buy"]
    assert center["completion_return_segment"]["direction"] == "down"
    assert center["completion_return_segment"]["start_time"] == int(
        lines[5].start.k.date.timestamp()
    )


def test_first_three_are_rectangle_body_and_leave_is_separate() -> None:
    from chanlun.cl_utils.tv_chart import xd_segment_centers_to_chart_dicts

    lines = _upward_center_segments()
    center = xd_segment_centers_to_chart_dicts(lines)[0]

    assert center["core_line_count"] == 3
    assert center["entering_segment"] is None
    assert center["first_three_components"][0]["start_time"] == center["points"][0]["time"]
    assert center["leaving_segment"]["start_time"] == center["points"][1]["time"]
    assert center["leaving_segment"]["end_time"] > center["points"][1]["time"]


def test_completed_center_keeps_leave_and_confirmation_return_separate() -> None:
    from chanlun.cl_utils.tv_chart import xd_segment_centers_to_chart_dicts

    lines = _upward_center_segments()
    lines.append(_segment(5, "down", 130, 120))
    center = xd_segment_centers_to_chart_dicts(lines)[0]

    assert center["done"] is True
    assert center["center_state"] == "completed"
    assert center["leaving_segment"]["start_time"] == int(
        lines[4].start.k.date.timestamp()
    )
    assert center["points"][1]["time"] == int(
        lines[4].start.k.date.timestamp()
    )
    assert center["completion_phase"] == "FORMAL_THIRD_CLASS_POINT"
    assert center["completion_point_type"] == "3buy"
    assert center["completion_point_status"] == "confirmed"
    assert center["associated_points"] == ["3buy"]
    assert center["completion_return_segment"]["start_time"] == int(
        lines[5].start.k.date.timestamp()
    )


def test_completed_downward_center_binds_same_level_three_sell() -> None:
    """三卖属于确认回抽段，不能错误地从离开段或低一级笔上取。"""

    from chanlun.cl_utils.tv_chart import xd_segment_centers_to_chart_dicts

    lines = _downward_center_segments()
    lines.append(_segment(5, "up", 80, 90))

    center = xd_segment_centers_to_chart_dicts(lines)[0]

    assert center["done"] is True
    assert center["completion_phase"] == "FORMAL_THIRD_CLASS_POINT"
    assert center["confirmation_scope"] == "xd"
    assert center["completion_point_type"] == "3sell"
    assert center["expected_completion_point_type"] == "3sell"
    assert center["completion_point_status"] == "confirmed"
    assert center["associated_points"] == ["3sell"]
    assert center["completion_return_segment"]["direction"] == "up"
    assert center["completion_return_segment"]["start_time"] == int(
        lines[5].start.k.date.timestamp()
    )


def test_center_extension_moves_leave_without_changing_first_three_core() -> None:
    from chanlun.cl_utils.tv_chart import xd_segment_centers_to_chart_dicts

    lines = _upward_center_segments()
    lines[4] = _segment(4, "up", 105, 110)
    lines.extend(
        [
            _segment(5, "down", 110, 104),
            _segment(6, "up", 104, 130),
        ]
    )
    center = xd_segment_centers_to_chart_dicts(lines)[0]

    assert center["core_directions"] == ["up", "down", "up"]
    assert [point["price"] for point in center["points"]] == [115.0, 100.0]
    assert center["leaving_segment"]["start_time"] == int(
        lines[6].start.k.date.timestamp()
    )
    assert center["points"][1]["time"] == int(
        lines[6].start.k.date.timestamp()
    )
