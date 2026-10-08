"""L025/L038: lower entry timing is separate from a formal third buy."""

from dataclasses import replace
from datetime import timedelta
from types import SimpleNamespace

from chanlun.core.strict_structure.center_machine import advance_center, establish_center
from chanlun.core.strict_structure.models import SourceKind, StrictPointStatus, TrendState
from chanlun.core.strict_structure.rule_state import third_point_entry_precision_rule_states
from tests.core.strict_structure.helpers import unit


def _case(*, anchor_on_second_return: bool):
    source = SourceKind.TREND_TYPE
    parent_units = (
        unit(0, "up", 80, 120, source_kind=source, structural_level=1),
        unit(1, "down", 120, 100, source_kind=source, structural_level=1),
        unit(2, "up", 100, 115, source_kind=source, structural_level=1),
        unit(3, "down", 115, 105, source_kind=source, structural_level=1),
        unit(4, "up", 105, 130, source_kind=source, structural_level=1),
    )
    center = establish_center(parent_units[1:4], 1, source, entry_unit=parent_units[0])
    assert center is not None
    waiting, _ = advance_center(center, parent_units[4])
    last_end = 116 if anchor_on_second_return else 121
    raw = (
        unit(5, "down", 130, 120),
        unit(6, "up", 120, 125),
        unit(7, "down", 125, last_end),
    )
    anchor_tick = min(item.low_tick for item in raw)
    anchor_at = raw[2].market_end if anchor_on_second_return else raw[0].market_end
    parent_return = unit(5, "down", 130, last_end, source_kind=source, structural_level=1)
    later_parent_lock = raw[-1].available_at + timedelta(minutes=10)
    parent_return = replace(
        parent_return,
        market_end=raw[-1].market_end,
        low_tick=anchor_tick,
        low_market_time=anchor_at,
        confirmed_at=later_parent_lock,
        available_at=later_parent_lock,
    )
    completed, _ = advance_center(waiting, parent_return)
    lower_trend = SimpleNamespace(
        trend_id=parent_return.unit_id,
        structural_level=0,
        price_basis_revision=parent_return.price_basis_revision,
        direction="down",
        state=TrendState.COMPLETE,
        centers=(object(),),
        constituent_units=raw,
        low_tick=anchor_tick,
        high_tick=130,
        market_start=raw[0].market_start,
        market_end=raw[-1].market_end,
        available_at=raw[-1].available_at,
    )

    def point(status, available_at):
        return SimpleNamespace(
            point_id="third-buy-case", point_type="3buy", status=status,
            structural_level=1, center_id=center.center_id,
            anchor_unit_id=parent_return.unit_id,
            price_basis_revision=parent_return.price_basis_revision,
            anchor_at=anchor_at, anchor_tick=anchor_tick,
            available_at=available_at,
        )

    lower = SimpleNamespace(structural_level=0, trend_types=(lower_trend,))
    return waiting, completed, lower, raw, point, parent_return


def _state(center, lower, points, as_of):
    parent = SimpleNamespace(
        structural_level=1,
        center_result=SimpleNamespace(centers=(center,)),
    )
    return third_point_entry_precision_rule_states(parent, lower, points, as_of)


def test_two_lower_returns_support_entry_timing_before_formal_third_buy_lock():
    waiting, completed, lower, raw, point, parent_return = _case(
        anchor_on_second_return=True
    )
    as_of = raw[-1].available_at
    approaching = point(StrictPointStatus.APPROACHING, as_of)
    early, = _state(waiting, lower, (approaching,), as_of)
    assert early["status"] == "bounded_precision_condition_observed"
    assert early["parent_center_initial_known_at"] >= waiting.established_at
    assert early["precision_branch"] == "double_subsub_return"
    assert early["same_direction_lower_member_count_to_anchor"] == 2
    assert early["anchor_lower_member_id"] == raw[-1].unit_id
    assert early["precision_known_at"] < parent_return.available_at
    assert early["formal_third_point_independent"] and not early["tradable"]

    confirmed = point(StrictPointStatus.CONFIRMED, parent_return.available_at)
    later, = _state(completed, lower, (confirmed,), parent_return.available_at)
    assert later["precision_branch"] == "double_subsub_return"
    assert later["formal_third_point_available_at"] == parent_return.available_at


def test_exact_lower_source_and_completed_child_are_required_for_double_return():
    waiting, _completed, lower, raw, point, _parent_return = _case(
        anchor_on_second_return=True
    )
    approaching = point(StrictPointStatus.APPROACHING, raw[-1].available_at)
    missing = SimpleNamespace(structural_level=0, trend_types=())
    absent, = _state(waiting, missing, (approaching,), raw[-1].available_at)
    assert absent["status"] == "pending_exact_lower_return_type"
    assert not absent["author_entry_condition_supported"]

    forming = SimpleNamespace(
        **{**lower.trend_types[0].__dict__, "state": TrendState.FORMING}
    )
    incomplete = SimpleNamespace(structural_level=0, trend_types=(forming,))
    pending, = _state(waiting, incomplete, (approaching,), raw[-1].available_at)
    assert pending["status"] == "pending_lower_return_completion"
    assert not pending["double_subsub_return_observed"]

    wrong_anchor_time = replace(raw[-1], low_market_time=raw[-1].market_start)
    mismatched = SimpleNamespace(**{
        **lower.trend_types[0].__dict__,
        "constituent_units": (*raw[:-1], wrong_anchor_time),
    })
    uncertain = SimpleNamespace(structural_level=0, trend_types=(mismatched,))
    wrong_time, = _state(waiting, uncertain, (approaching,), raw[-1].available_at)
    assert wrong_time["status"] == "pending_subsub_divergence_or_double_return"
    assert wrong_time["precision_known_at"] is None


def test_exact_lower_first_buy_divergence_is_an_alternative_to_double_return():
    waiting, _completed, lower, raw, point, _parent_return = _case(
        anchor_on_second_return=False
    )
    as_of = raw[-1].available_at
    approaching = point(StrictPointStatus.APPROACHING, as_of)
    no_divergence, = _state(waiting, lower, (approaching,), as_of)
    assert no_divergence["same_direction_lower_member_count_to_anchor"] == 1
    assert no_divergence["status"] == "pending_subsub_divergence_or_double_return"

    lower_first = SimpleNamespace(
        point_id="lower-first", point_type="1buy",
        status=StrictPointStatus.CONFIRMED, structural_level=0,
        price_basis_revision=approaching.price_basis_revision,
        anchor_tick=approaching.anchor_tick, anchor_at=approaching.anchor_at,
        anchor_unit_id=raw[0].unit_id,
        available_at=as_of, divergence=SimpleNamespace(kind="trend"),
    )
    supported, = _state(waiting, lower, (approaching, lower_first), as_of)
    assert supported["status"] == "bounded_precision_condition_observed"
    assert supported["precision_branch"] == "lower_trend_divergence"
    assert supported["lower_divergence_point_id"] == "lower-first"
    assert not supported["double_subsub_return_observed"]
    assert supported["formal_third_point_independent"] and not supported["tradable"]
