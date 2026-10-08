"""L025:808-823: a small first return cannot certify a higher center."""

from dataclasses import replace
from types import SimpleNamespace

import pytest

from chanlun.core.strict_structure.center_machine import advance_center, establish_center
from chanlun.core.strict_structure.models import SourceKind, TrendState
from chanlun.core.strict_structure.rule_state import center_first_return_grade_watch_states
from tests.core.strict_structure.helpers import unit


@pytest.mark.parametrize("mirror", [False, True])
def test_first_lower_return_touches_outer_band_before_child_grade_return(mirror):
    def make(index, direction, start, end, *, source_kind, level):
        if mirror:
            direction = "down" if direction == "up" else "up"
            start, end = 230 - start, 230 - end
        return unit(
            index, direction, start, end,
            source_kind=source_kind, structural_level=level,
        )

    source = SourceKind.TREND_TYPE
    parent_units = (
        make(0, "up", 80, 120, source_kind=source, level=1),
        make(1, "down", 120, 100, source_kind=source, level=1),
        make(2, "up", 100, 115, source_kind=source, level=1),
        make(3, "down", 115, 105, source_kind=source, level=1),
        make(4, "up", 105, 130, source_kind=source, level=1),
    )
    center = establish_center(
        parent_units[1:4], 1, source, entry_unit=parent_units[0]
    )
    assert center is not None
    waiting, _ = advance_center(center, parent_units[4])
    raw = (
        make(4, "up", 105, 130, source_kind=SourceKind.SEGMENT, level=0),
        make(5, "down", 130, 120, source_kind=SourceKind.SEGMENT, level=0),
        make(6, "up", 120, 125, source_kind=SourceKind.SEGMENT, level=0),
        make(7, "down", 125, 116, source_kind=SourceKind.SEGMENT, level=0),
    )

    def watch(value, *, as_of, lower_trends=()):
        parent = SimpleNamespace(
            structural_level=1,
            center_result=SimpleNamespace(centers=(value,)),
        )
        lower = SimpleNamespace(
            structural_level=0, units=raw,
            trend_types=lower_trends,
        )
        return center_first_return_grade_watch_states(parent, lower, as_of)[0]

    before_return_lock = watch(waiting, as_of=raw[0].available_at)
    assert before_return_lock["status"] == "pending_first_lower_return"
    assert before_return_lock["first_lower_return_unit_id"] is None

    early = watch(waiting, as_of=raw[1].available_at)
    assert early["status"] == "lower_grade_outer_touch_only"
    assert early["first_lower_return_unit_id"] == raw[1].unit_id
    assert early["first_lower_return_touch_zone"] == "outer_band"
    assert early["qualified_child_return_trend_id"] is None
    assert early["higher_center_formal"] == "independent_three_completed_child_types_required"
    assert early["tradable"] is False

    parent_return = make(
        5, "down", 130, 116, source_kind=source, level=1
    )
    parent_return = replace(
        parent_return,
        market_end=raw[3].market_end,
        confirmed_at=raw[3].confirmed_at,
        available_at=raw[3].available_at,
    )
    completed, _ = advance_center(waiting, parent_return)
    missing = watch(completed, as_of=parent_return.available_at)
    assert missing["status"] == "pending_exact_locked_child_return_source"
    assert missing["qualified_child_return_trend_id"] is None

    exact_child = SimpleNamespace(
        trend_id=parent_return.unit_id,
        state=TrendState.LOCKED,
        available_at=parent_return.available_at,
        price_basis_revision=parent_return.price_basis_revision,
    )
    qualified = watch(
        completed, as_of=parent_return.available_at,
        lower_trends=(exact_child,),
    )
    assert qualified["status"] == (
        "qualified_child_return_touches_outer_band_parent_pending"
    )
    assert qualified["qualified_child_return_trend_id"] == parent_return.unit_id
    assert qualified["qualified_child_return_touch_zone"] == "outer_band"
    assert qualified["first_lower_return_unit_id"] == raw[1].unit_id
    assert qualified["first_lower_return_known_at"] <= (
        qualified["qualified_child_return_known_at"]
    )
    assert qualified["higher_center_formal"] == (
        "independent_three_completed_child_types_required"
    )
    assert qualified["tradable"] is False


@pytest.mark.parametrize("mirror", [False, True])
def test_equal_core_edge_is_boundary_contact_at_both_grades(mirror):
    def make(index, direction, start, end, source, level):
        if mirror:
            direction = "down" if direction == "up" else "up"
            start, end = 230 - start, 230 - end
        return unit(
            index, direction, start, end,
            source_kind=source, structural_level=level,
        )

    source = SourceKind.TREND_TYPE
    body = (
        make(0, "down", 120, 100, source, 1),
        make(1, "up", 100, 115, source, 1),
        make(2, "down", 115, 105, source, 1),
    )
    center = establish_center(body, 1, source)
    assert center is not None
    leave = make(3, "up", 105, 130, source, 1)
    waiting, _ = advance_center(center, leave)
    raw = (
        make(3, "up", 105, 130, SourceKind.SEGMENT, 0),
        make(4, "down", 130, 115, SourceKind.SEGMENT, 0),
    )
    parent = SimpleNamespace(
        structural_level=1,
        center_result=SimpleNamespace(centers=(waiting,)),
    )
    lower = SimpleNamespace(structural_level=0, units=raw, trend_types=())
    early = center_first_return_grade_watch_states(
        parent, lower, raw[1].available_at
    )[0]
    assert early["first_lower_return_touch_zone"] == "core_boundary_contact"
    assert early["status"] == "lower_grade_core_boundary_contact_only"

    parent_return = make(4, "down", 130, 115, source, 1)
    completed, _ = advance_center(waiting, parent_return)
    exact_child = SimpleNamespace(
        trend_id=parent_return.unit_id,
        state=TrendState.LOCKED,
        available_at=parent_return.available_at,
        price_basis_revision=parent_return.price_basis_revision,
    )
    parent.center_result.centers = (completed,)
    lower.trend_types = (exact_child,)
    qualified = center_first_return_grade_watch_states(
        parent, lower, parent_return.available_at
    )[0]
    # The fully known boundary contact has folded into the same center, so
    # this departure is no longer a pending third-point return at either grade.
    assert completed.state.value == "ongoing"
    assert completed.body_units[-1] is parent_return
    assert qualified["qualified_child_return_touch_zone"] is None
    assert qualified["higher_center_formal"] == (
        "independent_three_completed_child_types_required"
    )
