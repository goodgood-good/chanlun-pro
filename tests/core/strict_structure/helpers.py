from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone

from chanlun.core.strict_structure.center_machine import (
    advance_center,
    establish_center,
)
from chanlun.core.strict_structure.models import (
    CenterEventKind,
    CenterState,
    ConstituentUnit,
    SourceKind,
    TrendCenter,
)


BASE = datetime(2026, 1, 5, 9, 30, tzinfo=timezone.utc)
TEST_PRICE_BASIS = "test-raw-v1"


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


def valid_five_up_exit(
    unit_offset: int = 0,
    *,
    structural_level: int = 0,
    zd_tick: int = 105,
    zg_tick: int = 115,
) -> tuple[ConstituentUnit, ...]:
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


def ongoing_center(
    unit_offset: int = 0,
    *,
    structural_level: int = 0,
    zd_tick: int = 105,
    zg_tick: int = 115,
    center_id: str | None = None,
) -> TrendCenter:
    initial = valid_five_up_exit(
        unit_offset,
        structural_level=structural_level,
        zd_tick=zd_tick,
        zg_tick=zg_tick,
    )
    value = establish_center(initial, structural_level, SourceKind.SEGMENT)
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
    initial = (
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
            zg_tick + 5,
            structural_level=structural_level,
        ),
        unit(
            unit_offset + 2,
            "down",
            zg_tick + 5,
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
    value = establish_center(initial, structural_level, SourceKind.SEGMENT)
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


# Transitional aliases keep older dependent test modules importable while their
# assertions are migrated in Task 2.
def center(unit_offset: int = 0, **changes) -> TrendCenter:
    return ongoing_center(
        unit_offset,
        structural_level=changes.pop("structural_level", 0),
        zd_tick=changes.pop("zd_tick", 105),
        zg_tick=changes.pop("zg_tick", 115),
        center_id=changes.pop("center_id", None),
    )


def destroyed_up_center(unit_offset: int = 0, **changes) -> TrendCenter:
    changes.pop("dd_tick", None)
    changes.pop("gg_tick", None)
    return completed_up_center(unit_offset, **changes)


def destroyed_down_center(unit_offset: int = 0, **changes) -> TrendCenter:
    changes.pop("dd_tick", None)
    changes.pop("gg_tick", None)
    return completed_down_center(unit_offset, **changes)
