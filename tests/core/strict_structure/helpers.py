from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from chanlun.core.strict_structure.center_machine import (
    advance_center,
    establish_center,
)
from chanlun.core.strict_structure.models import (
    CenterEventKind,
    CenterLevelResult,
    CenterState,
    ConstituentUnit,
    SourceKind,
    StrictLevelResult,
    StrictStructureResult,
    TrendCenter,
)


BASE = datetime(2026, 1, 5, 9, 30, tzinfo=timezone.utc)
TEST_PRICE_BASIS = "test-raw"


def unit(
    index: int,
    direction: str,
    start_tick: int,
    end_tick: int,
    *,
    locked: bool = True,
    source_kind: SourceKind = SourceKind.SEGMENT,
    structural_level: int = 0,
) -> ConstituentUnit:
    start = BASE + timedelta(minutes=index * 5)
    end = start + timedelta(minutes=5)
    return ConstituentUnit(
        unit_id=f"u-{index}",
        structural_level=structural_level,
        source_kind=source_kind,
        price_basis_revision=TEST_PRICE_BASIS,
        direction=direction,
        start_tick=start_tick,
        end_tick=end_tick,
        low_tick=min(start_tick, end_tick),
        high_tick=max(start_tick, end_tick),
        market_start=start,
        market_end=end,
        confirmed_at=(end + timedelta(minutes=5) if locked else None),
        available_at=end + timedelta(minutes=5),
        locked=locked,
        child_ids=(),
    )


def valid_up_center_lifecycle(
    unit_offset: int = 0,
    *,
    structural_level: int = 0,
    zd_tick: int = 105,
    zg_tick: int = 115,
    return_low_tick: int | None = None,
) -> tuple[ConstituentUnit, ...]:
    """Return three core segments, an up departure, and its outside return."""

    resolved_return_low = zg_tick + 5 if return_low_tick is None else return_low_tick
    return (
        unit(
            unit_offset,
            "down",
            zg_tick + 5,
            zd_tick - 5,
            structural_level=structural_level,
        ),
        unit(
            unit_offset + 1,
            "up",
            zd_tick - 5,
            zg_tick,
            structural_level=structural_level,
        ),
        unit(
            unit_offset + 2,
            "down",
            zg_tick,
            zd_tick,
            structural_level=structural_level,
        ),
        unit(
            unit_offset + 3,
            "up",
            zd_tick,
            zg_tick + 15,
            structural_level=structural_level,
        ),
        unit(
            unit_offset + 4,
            "down",
            zg_tick + 15,
            resolved_return_low,
            structural_level=structural_level,
        ),
    )


def valid_five_up_exit(
    unit_offset: int = 0,
    *,
    structural_level: int = 0,
    zd_tick: int = 105,
    zg_tick: int = 115,
) -> tuple[ConstituentUnit, ...]:
    """进入段 + 中间三段核心 + 独立向上离开段。"""

    return (
        unit(
            unit_offset,
            "up",
            zd_tick - 15,
            zg_tick + 5,
            structural_level=structural_level,
        ),
        unit(
            unit_offset + 1,
            "down",
            zg_tick + 5,
            zd_tick - 5,
            structural_level=structural_level,
        ),
        unit(
            unit_offset + 2,
            "up",
            zd_tick - 5,
            zg_tick,
            structural_level=structural_level,
        ),
        unit(
            unit_offset + 3,
            "down",
            zg_tick,
            zd_tick,
            structural_level=structural_level,
        ),
        unit(
            unit_offset + 4,
            "up",
            zd_tick,
            zg_tick + 15,
            structural_level=structural_level,
        ),
    )


def valid_down_center_lifecycle(
    unit_offset: int = 0,
    *,
    structural_level: int = 0,
    zd_tick: int = 95,
    zg_tick: int = 105,
    return_high_tick: int | None = None,
) -> tuple[ConstituentUnit, ...]:
    """Return three core segments, a down departure, and its outside return."""

    resolved_return_high = (
        zd_tick - 5 if return_high_tick is None else return_high_tick
    )
    return (
        unit(
            unit_offset,
            "up",
            zd_tick - 5,
            zg_tick + 5,
            structural_level=structural_level,
        ),
        unit(
            unit_offset + 1,
            "down",
            zg_tick + 5,
            zd_tick,
            structural_level=structural_level,
        ),
        unit(
            unit_offset + 2,
            "up",
            zd_tick,
            zg_tick,
            structural_level=structural_level,
        ),
        unit(
            unit_offset + 3,
            "down",
            zg_tick,
            zd_tick - 15,
            structural_level=structural_level,
        ),
        unit(
            unit_offset + 4,
            "up",
            zd_tick - 15,
            resolved_return_high,
            structural_level=structural_level,
        ),
    )


def valid_three_center_seed(
    unit_offset: int = 0,
    *,
    structural_level: int = 0,
    zd_tick: int = 105,
    zg_tick: int = 115,
) -> tuple[ConstituentUnit, ...]:
    """Three connected units for explicit recursive trend-type tests."""

    values = valid_up_center_lifecycle(
        unit_offset,
        structural_level=structural_level,
        zd_tick=zd_tick,
        zg_tick=zg_tick,
    )[:3]
    return tuple(
        replace(item, source_kind=SourceKind.TREND_TYPE)
        for item in values
    )


def ongoing_center(
    unit_offset: int = 0,
    *,
    structural_level: int = 0,
    zd_tick: int = 105,
    zg_tick: int = 115,
    center_id: str | None = None,
) -> TrendCenter:
    lifecycle = valid_five_up_exit(
        unit_offset,
        structural_level=structural_level,
        zd_tick=zd_tick,
        zg_tick=zg_tick,
    )
    value = establish_center(lifecycle, structural_level, SourceKind.SEGMENT)
    assert value is not None
    return value if center_id is None else replace(value, center_id=center_id)


def completed_up_center(
    unit_offset: int = 0,
    *,
    structural_level: int = 0,
    zd_tick: int = 105,
    zg_tick: int = 115,
    return_low_tick: int | None = None,
    center_id: str | None = None,
) -> TrendCenter:
    value = ongoing_center(
        unit_offset,
        structural_level=structural_level,
        zd_tick=zd_tick,
        zg_tick=zg_tick,
        center_id=center_id,
    )
    resolved_return_low = zg_tick + 5 if return_low_tick is None else return_low_tick
    ret = unit(
        unit_offset + 5,
        "down",
        zg_tick + 15,
        resolved_return_low,
        structural_level=structural_level,
    )
    completed, event = advance_center(value, ret)
    assert completed.state is CenterState.COMPLETED
    assert event.kind is CenterEventKind.COMPLETED_UP
    return completed


def ongoing_down_center(
    unit_offset: int = 0,
    *,
    structural_level: int = 0,
    zd_tick: int = 95,
    zg_tick: int = 105,
    center_id: str | None = None,
) -> TrendCenter:
    lifecycle = (
        unit(
            unit_offset,
            "down",
            zg_tick + 15,
            zd_tick - 5,
            structural_level=structural_level,
        ),
        unit(
            unit_offset + 1,
            "up",
            zd_tick - 5,
            zg_tick,
            structural_level=structural_level,
        ),
        unit(
            unit_offset + 2,
            "down",
            zg_tick,
            zd_tick,
            structural_level=structural_level,
        ),
        unit(
            unit_offset + 3,
            "up",
            zd_tick,
            zg_tick,
            structural_level=structural_level,
        ),
        unit(
            unit_offset + 4,
            "down",
            zg_tick,
            zd_tick - 15,
            structural_level=structural_level,
        ),
    )
    value = establish_center(lifecycle, structural_level, SourceKind.SEGMENT)
    assert value is not None
    return value if center_id is None else replace(value, center_id=center_id)


def completed_down_center(
    unit_offset: int = 0,
    *,
    structural_level: int = 0,
    zd_tick: int = 95,
    zg_tick: int = 105,
    return_high_tick: int | None = None,
    center_id: str | None = None,
) -> TrendCenter:
    value = ongoing_down_center(
        unit_offset,
        structural_level=structural_level,
        zd_tick=zd_tick,
        zg_tick=zg_tick,
        center_id=center_id,
    )
    resolved_return_high = zd_tick - 5 if return_high_tick is None else return_high_tick
    ret = unit(
        unit_offset + 5,
        "up",
        zd_tick - 15,
        resolved_return_high,
        structural_level=structural_level,
    )
    completed, event = advance_center(value, ret)
    assert completed.state is CenterState.COMPLETED
    assert event.kind is CenterEventKind.COMPLETED_DOWN
    return completed


def structure_for(*centers, completed_trends=()) -> StrictStructureResult:
    center_values = tuple(centers)
    trend_values = tuple(completed_trends)
    levels_seen = {
        item.structural_level for item in center_values + trend_values
    }
    max_level = max(levels_seen, default=-1)
    levels = []
    for structural_level in range(max_level + 1):
        level_centers = tuple(
            item
            for item in center_values
            if item.structural_level == structural_level
        )
        level_trends = tuple(
            item
            for item in trend_values
            if item.structural_level == structural_level
        )
        by_id = {}
        for trend in level_trends:
            for item in trend.constituent_units:
                by_id.setdefault(item.unit_id, item)
        for center_value in level_centers:
            for item in (
                *(
                    ()
                    if center_value.entry_unit is None
                    else (center_value.entry_unit,)
                ),
                *center_value.body_units,
                *center_value.failed_departure_units,
                *center_value.supersession_bridge_units,
                *(
                    ()
                    if center_value.pending_leave_unit is None
                    else (center_value.pending_leave_unit,)
                ),
                *(
                    ()
                    if center_value.completion_leave_unit is None
                    else (center_value.completion_leave_unit,)
                ),
            ):
                by_id.setdefault(item.unit_id, item)
            ret = center_value.completion_return_unit
            if ret is not None:
                by_id.setdefault(ret.unit_id, ret)
        units = tuple(
            sorted(
                by_id.values(),
                key=lambda item: (item.market_start, item.unit_id),
            )
        )
        if any(
            current.market_start < previous.market_start
            for previous, current in zip(units, units[1:])
        ):
            raise ValueError("structure helper unit time moved backward")
        center_result = CenterLevelResult(
            structural_level=structural_level,
            price_basis_revision=(
                units[0].price_basis_revision if units else TEST_PRICE_BASIS
            ),
            centers=level_centers,
            previews=(),
            events=(),
            locked_unit_count=sum(1 for item in units if item.locked),
            replay_from=0,
        )
        levels.append(
            StrictLevelResult(
                structural_level=structural_level,
                units=units,
                center_result=center_result,
                trend_types=level_trends,
                completed_trends=level_trends,
            )
        )
    return StrictStructureResult(
        schema="chanlun-structure",
        price_basis_revision=TEST_PRICE_BASIS,
        levels=tuple(levels),
    )


def engine_for(*centers, completed_trends=()):
    from chanlun.core.strict_structure.signals import StrictSignalEngine

    return StrictSignalEngine(
        structure=structure_for(
            *centers,
            completed_trends=completed_trends,
        ),
        price_quantum=Decimal("0.01"),
    )


def only_point(points):
    values = tuple(points)
    assert len(values) == 1
    return values[0]
