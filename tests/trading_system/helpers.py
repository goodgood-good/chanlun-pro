from __future__ import annotations

from datetime import datetime, timedelta
from types import SimpleNamespace
from typing import Callable, cast
from zoneinfo import ZoneInfo

from chanlun.core.bs_branch import BuySellPoint
from chanlun.core.zs_branch import DivergenceResult, ZsBranchResult
from chanlun.decision_support.trading_system.models import (
    PointType,
    PointVariant,
    SectorAssessment,
    StructuralPoint,
    StructureTower,
    TimeframeContext,
    build_point_id,
)
from chanlun.decision_support.trading_system.provisional import ProvisionalCandidate
from chanlun.decision_support.trading_system.lifecycle import build_setup


CN = ZoneInfo("Asia/Shanghai")
BASE_AT = datetime(2026, 7, 20, 9, 30, tzinfo=CN)
POINT_AT = datetime(2026, 7, 20, 10, 0, tzinfo=CN)
AS_OF = datetime(2026, 7, 20, 15, 0, tzinfo=CN)


def _fx(index: int, value: float, *, done: bool = True) -> SimpleNamespace:
    k = SimpleNamespace(
        k_index=index,
        index=index,
        date=BASE_AT + timedelta(minutes=index),
    )
    return SimpleNamespace(k=k, val=value, done=done, index=index)


def line(
    direction: str,
    start_index: int,
    start_value: float,
    end_index: int,
    end_value: float,
    *,
    done: bool = True,
) -> SimpleNamespace:
    start = _fx(start_index, start_value)
    end = _fx(end_index, end_value, done=done)
    return SimpleNamespace(
        _type=direction,
        type=direction,
        start=start,
        end=end,
        high=max(start_value, end_value),
        low=min(start_value, end_value),
        zs_high=max(start_value, end_value),
        zs_low=min(start_value, end_value),
        index=start_index,
    )


def divergence(
    leave: SimpleNamespace,
    *,
    kind: str,
    provisional: bool,
    is_beichi: bool = True,
) -> DivergenceResult:
    return DivergenceResult(
        is_beichi=is_beichi,
        kind=kind,
        compare_seg=leave,
        leave_seg=leave,
        provisional=provisional,
    )


def zone(
    leave: SimpleNamespace,
    *,
    zg: float = 10.0,
    zd: float = 9.0,
    zone_id: int = 1,
) -> SimpleNamespace:
    base_index = leave.start.k.k_index - 6
    body = [
        line("up", base_index, zd, base_index + 1, zg),
        line("down", base_index + 1, zg, base_index + 2, zd),
        line("up", base_index + 2, zd, base_index + 3, zg),
    ]
    return SimpleNamespace(
        start=body[0],
        end=leave,
        lines=body,
        zg=zg,
        zd=zd,
        gg=zg,
        dd=zd,
        done=True,
        real=True,
        index=zone_id,
        zs_type="bi",
    )


def zone_result(
    core: SimpleNamespace,
    result: DivergenceResult | None,
) -> ZsBranchResult:
    return ZsBranchResult(
        done_zss=[core],
        live=[],
        freeze_idx=0,
        done_divergence=[result],
    )


def weak_second_buy_case() -> tuple[
    ZsBranchResult,
    list[SimpleNamespace],
    Callable[[object, object], dict[str, dict[str, float]]],
]:
    first = line("down", 0, 10.0, 5, 8.0)
    rebound = line("up", 5, 8.0, 10, 10.0)
    pullback = line("down", 10, 10.0, 15, 7.5)
    result = zone_result(
        zone(first, zg=9.5, zd=8.5),
        divergence(first, kind="qs", provisional=False),
    )

    def provider(_start: object, end: object) -> dict[str, dict[str, float]]:
        later = end.k.k_index > first.end.k.k_index
        return {
            "hist": {
                "up_sum": 0.0,
                "down_sum": 5.0 if later else 10.0,
                "max": 0.0,
                "min": -1.0 if later else -2.0,
            },
            "dif": {
                "max": 0.0,
                "min": -1.0 if later else -2.0,
            },
        }

    return result, [first, rebound, pullback], provider


def raw_point(
    bs_type: str,
    signal: SimpleNamespace,
    *,
    zone_id: int = 1,
    level: int | None = None,
    definition_variant: str = "standard",
    core: SimpleNamespace | None = None,
) -> BuySellPoint:
    core = zone(signal, zone_id=zone_id) if core is None else core
    if bs_type == "3buy":
        stop_kwargs = {"structural_stop_below": core.zg}
    elif bs_type == "3sell":
        stop_kwargs = {"structural_stop_above": core.zd}
    elif bs_type.endswith("buy"):
        stop_kwargs = {"structural_stop_below": signal.end.val}
    else:
        stop_kwargs = {"structural_stop_above": signal.end.val}
    return BuySellPoint(
        bs_type,
        core,
        signal,
        signal.end,
        (
            None
            if bs_type.startswith("3")
            else divergence(signal, kind="qs", provisional=False)
        ),
        level=level,
        definition_variant=definition_variant,
        **stop_kwargs,
    )


def fake_cd_with_unfinished_down_line(
    *,
    mmds: tuple[str, ...],
    divergences: tuple[str, ...],
) -> SimpleNamespace:
    unfinished = line("down", 20, 11.0, 25, 9.0, done=False)
    real_zone = SimpleNamespace(real=True)
    unfinished.zs_type_mmds = {
        "bi": [SimpleNamespace(name=name, zs=real_zone) for name in mmds]
    }
    live = [
        (
            zone(unfinished, zone_id=index + 1),
            divergence(unfinished, kind=kind, provisional=True),
        )
        for index, kind in enumerate(divergences)
    ]
    level = SimpleNamespace(
        level=0,
        units=[unfinished],
        live_qs_divergence=live,
    )
    return SimpleNamespace(
        get_recursive_branch_levels_for_tower=(
            lambda use_xd: [] if use_xd else [level]
        )
    )


def confirmed_point(
    point_type: str,
    *,
    frequency: str = "5m",
    tower: str = "formal",
    level: int = 0,
    anchor: float = 10.0,
    stop: float | None = None,
    center_id: str | None = "center-a",
    center_zd: float | None = 9.0,
    center_zg: float | None = 9.8,
    center_ordinal: int | None = 1,
    variant: str = "standard",
    minutes_after: int = 0,
    available_minutes_after: int = 0,
    price_basis_revision: str = "test-raw-v1",
) -> StructuralPoint:
    typed_point = cast(PointType, point_type)
    typed_tower = cast(StructureTower, tower)
    typed_variant = cast(PointVariant, variant)
    anchor_at = POINT_AT + timedelta(minutes=minutes_after)
    side = "buy" if point_type.endswith("buy") else "sell"
    invalidation = (
        stop
        if stop is not None
        else anchor - 0.2
        if side == "buy"
        else anchor + 0.2
    )
    point_id = build_point_id(
        code="SZ.000001",
        price_basis_revision=price_basis_revision,
        point_type=typed_point,
        source_frequency=frequency,
        tower=typed_tower,
        recursive_level=level,
        anchor_at=anchor_at,
        center_id=center_id,
        parent_point_id=None,
    )
    return StructuralPoint(
        point_id=point_id,
        code="SZ.000001",
        point_type=typed_point,
        side=side,
        status="confirmed",
        variant=typed_variant,
        source_frequency=frequency,
        price_basis_revision=price_basis_revision,
        tower=typed_tower,
        recursive_level=level,
        anchor_at=anchor_at,
        confirmed_at=anchor_at,
        available_at=anchor_at + timedelta(minutes=available_minutes_after),
        structure_anchor_price=anchor,
        structure_invalidation_price=invalidation,
        center_id=center_id,
        center_zd=center_zd,
        center_zg=center_zg,
        center_ordinal=center_ordinal,
        divergence_kind="qs" if point_type.startswith("1") else None,
        parent_point_id=None,
        evidence_codes=("test_fixture",),
    )


def neutral_context(frequency: str) -> TimeframeContext:
    return TimeframeContext(
        frequency=frequency,
        direction="neutral",
        disposition="neutral",
        hard_block=False,
        dominant_point_id=None,
        dominant_point_type=None,
        reason_codes=("test_neutral",),
        observed_at=AS_OF,
    )


def supportive_context(frequency: str) -> TimeframeContext:
    point = confirmed_point("1buy", frequency=frequency)
    return TimeframeContext(
        frequency=frequency,
        direction="up",
        disposition="supportive",
        hard_block=False,
        dominant_point_id=point.point_id,
        dominant_point_type=point.point_type,
        reason_codes=("test_supportive",),
        observed_at=AS_OF,
    )


def hostile_context(frequency: str) -> TimeframeContext:
    point = confirmed_point("1sell", frequency=frequency)
    return TimeframeContext(
        frequency=frequency,
        direction="down",
        disposition="hostile",
        hard_block=True,
        dominant_point_id=point.point_id,
        dominant_point_type=point.point_type,
        reason_codes=("test_hostile",),
        observed_at=AS_OF,
    )


def eligible_sector() -> SectorAssessment:
    return SectorAssessment(
        sector_id="TDX.880301",
        sector_name="煤炭",
        eligible=True,
        hard_block=False,
        regime="neutral",
        rank_components=(("neutral_access", 5),),
        reason_codes=("test_eligible",),
    )


def hostile_sector() -> SectorAssessment:
    return SectorAssessment(
        sector_id="TDX.880301",
        sector_name="煤炭",
        eligible=False,
        hard_block=True,
        regime="hostile",
        rank_components=(),
        reason_codes=("test_hostile",),
    )


def provisional_point(
    point_type: str,
    *,
    frequency: str = "5m",
    tower: str = "formal",
    level: int = 0,
    anchor: float = 10.0,
) -> ProvisionalCandidate:
    side = "buy" if point_type.endswith("buy") else "sell"
    return ProvisionalCandidate(
        candidate_id=f"candidate:{point_type}:{tower}:{level}",
        code="SZ.000001",
        point_type=cast(PointType, point_type),
        side=side,
        status="provisional",
        source_frequency=frequency,
        tower=cast(StructureTower, tower),
        recursive_level=level,
        observed_at=POINT_AT,
        anchor_price=anchor,
        missing_conditions=("terminal_line_confirmed",),
        evidence_codes=("test_fixture",),
    )


def setup_for(point: StructuralPoint | ProvisionalCandidate):
    return build_setup(point, neutral_context("30m"), eligible_sector())
