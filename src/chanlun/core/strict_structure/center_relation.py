from __future__ import annotations

from chanlun.core.strict_structure.models import CenterRelation, TrendCenter


def classify_center_relation(
    previous: TrendCenter,
    current: TrendCenter,
) -> CenterRelation:
    if (
        previous.structural_level != current.structural_level
        or previous.source_kind is not current.source_kind
    ):
        raise ValueError("centers must have the same level and source")
    if previous.price_basis_revision != current.price_basis_revision:
        raise ValueError("centers must have the same price basis")
    if previous.center_id == current.center_id:
        raise ValueError("centers must have distinct identities")
    shares_completion_leave = (
        previous.completion_leave_unit is not None
        and current.entry_unit.unit_id
        == previous.completion_leave_unit.unit_id
    )
    if (
        current.body_start_market_time <= previous.body_start_market_time
        or (
            current.body_start_market_time < previous.last_touch_market_time
            and not shares_completion_leave
        )
    ):
        raise ValueError("centers must be strictly time ordered")
    if current.dd_tick > previous.gg_tick:
        return CenterRelation.UP_TREND
    if current.gg_tick < previous.dd_tick:
        return CenterRelation.DOWN_TREND
    return CenterRelation.UPGRADE
