from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from chanlun.core.strict_structure.models import ConstituentUnit, SourceKind
from chanlun.core.strict_structure.identity import build_center_id
from chanlun.decision_support.trading_system.backtest.fixed_year import (
    CAUSAL_CENTER_COMPLETION_CONTRACT,
    FACT_SCHEMA,
    CausalCenterCompletionFact,
    CausalStructureEventLedger,
    SymbolResearchFacts,
)


START = datetime(2026, 6, 1, 9, 30, tzinfo=ZoneInfo("Asia/Shanghai"))
REVISION = "test-price-basis"


def _unit(
    name: str,
    offset: int,
    *,
    direction: str,
    start_tick: int,
    end_tick: int,
    source_kind: SourceKind = SourceKind.SEGMENT,
) -> ConstituentUnit:
    market_start = START + timedelta(minutes=offset)
    market_end = market_start + timedelta(minutes=1)
    return ConstituentUnit(
        unit_id=name,
        structural_level=0,
        source_kind=source_kind,
        price_basis_revision=REVISION,
        direction=direction,  # type: ignore[arg-type]
        start_tick=start_tick,
        end_tick=end_tick,
        low_tick=min(start_tick, end_tick),
        high_tick=max(start_tick, end_tick),
        market_start=market_start,
        market_end=market_end,
        confirmed_at=market_end,
        available_at=market_end,
        locked=True,
        child_ids=(f"{name}-child",),
    )


def _physical_center_fixture(
    source_kind: SourceKind = SourceKind.SEGMENT,
) -> tuple[
    CausalCenterCompletionFact,
    tuple[ConstituentUnit, ...],
]:
    entry = _unit(
        "entry",
        0,
        direction="up",
        start_tick=80,
        end_tick=120,
        source_kind=source_kind,
    )
    core_a = _unit(
        "core-a",
        1,
        direction="down",
        start_tick=120,
        end_tick=90,
        source_kind=source_kind,
    )
    core_b = _unit(
        "core-b",
        2,
        direction="up",
        start_tick=90,
        end_tick=110,
        source_kind=source_kind,
    )
    core_c = _unit(
        "core-c",
        3,
        direction="down",
        start_tick=110,
        end_tick=95,
        source_kind=source_kind,
    )
    establishment_leave = _unit(
        "establishment-leave",
        4,
        direction="up",
        start_tick=95,
        end_tick=120,
        source_kind=source_kind,
    )
    completion_leave = establishment_leave
    completion_return = _unit(
        "completion-return",
        5,
        direction="down",
        start_tick=120,
        end_tick=115,
        source_kind=source_kind,
    )
    fact = CausalCenterCompletionFact(
        center_id=build_center_id(
            price_basis_revision=REVISION,
            structural_level=0,
            source_kind=source_kind.value,
            entry_unit_id=entry.unit_id,
            initial_unit_ids=(core_a.unit_id, core_b.unit_id, core_c.unit_id),
            establishment_leave_unit_id=establishment_leave.unit_id,
            zd_tick=95,
            zg_tick=110,
        ),
        source_frequency="5m",
        structural_level=0,
        source_kind=source_kind,
        price_basis_revision=REVISION,
        body_revision=0,
        available_at=completion_return.available_at,
        completed_at=completion_return.confirmed_at,
        zd_tick=95,
        zg_tick=110,
        entry_unit_id=entry.unit_id,
        core_unit_ids=(core_a.unit_id, core_b.unit_id, core_c.unit_id),
        establishment_leave_unit_id=establishment_leave.unit_id,
        establishment_unit_ids=(
            entry.unit_id,
            core_a.unit_id,
            core_b.unit_id,
            core_c.unit_id,
            establishment_leave.unit_id,
        ),
        leave_unit_id=completion_leave.unit_id,
        leave_direction=completion_leave.direction,
        leave_market_start=completion_leave.market_start,
        leave_market_end=completion_leave.market_end,
        leave_available_at=completion_leave.available_at,
        leave_start_tick=completion_leave.start_tick,
        leave_end_tick=completion_leave.end_tick,
        leave_low_tick=completion_leave.low_tick,
        leave_high_tick=completion_leave.high_tick,
        return_unit_id=completion_return.unit_id,
        return_direction=completion_return.direction,
        return_market_start=completion_return.market_start,
        return_market_end=completion_return.market_end,
        return_available_at=completion_return.available_at,
        return_start_tick=completion_return.start_tick,
        return_end_tick=completion_return.end_tick,
        return_low_tick=completion_return.low_tick,
        return_high_tick=completion_return.high_tick,
    )
    return fact, (
        entry,
        core_a,
        core_b,
        core_c,
        establishment_leave,
        completion_return,
    )


def _completion_replacement_fields(
    leave: ConstituentUnit,
    ret: ConstituentUnit,
) -> dict[str, object]:
    return {
        "available_at": ret.available_at,
        "completed_at": ret.confirmed_at,
        "leave_unit_id": leave.unit_id,
        "leave_direction": leave.direction,
        "leave_market_start": leave.market_start,
        "leave_market_end": leave.market_end,
        "leave_available_at": leave.available_at,
        "leave_start_tick": leave.start_tick,
        "leave_end_tick": leave.end_tick,
        "leave_low_tick": leave.low_tick,
        "leave_high_tick": leave.high_tick,
        "return_unit_id": ret.unit_id,
        "return_direction": ret.direction,
        "return_market_start": ret.market_start,
        "return_market_end": ret.market_end,
        "return_available_at": ret.available_at,
        "return_start_tick": ret.start_tick,
        "return_end_tick": ret.end_tick,
        "return_low_tick": ret.low_tick,
        "return_high_tick": ret.high_tick,
    }


def _recursive_center_fixture() -> tuple[
    CausalCenterCompletionFact,
    tuple[ConstituentUnit, ...],
]:
    source_kind = SourceKind.TREND_TYPE
    core = (
        _unit(
            "recursive-core-a",
            0,
            direction="down",
            start_tick=120,
            end_tick=90,
            source_kind=source_kind,
        ),
        _unit(
            "recursive-core-b",
            1,
            direction="up",
            start_tick=90,
            end_tick=110,
            source_kind=source_kind,
        ),
        _unit(
            "recursive-core-c",
            2,
            direction="down",
            start_tick=110,
            end_tick=95,
            source_kind=source_kind,
        ),
    )
    leave = _unit(
        "recursive-leave",
        3,
        direction="up",
        start_tick=95,
        end_tick=130,
        source_kind=source_kind,
    )
    ret = _unit(
        "recursive-return",
        4,
        direction="down",
        start_tick=130,
        end_tick=115,
        source_kind=source_kind,
    )
    physical, _units = _physical_center_fixture()
    core_ids = tuple(item.unit_id for item in core)
    fact = replace(
        physical,
        center_id=build_center_id(
            price_basis_revision=REVISION,
            structural_level=0,
            source_kind=source_kind.value,
            entry_unit_id=None,
            initial_unit_ids=core_ids,
            establishment_leave_unit_id=None,
            zd_tick=95,
            zg_tick=110,
        ),
        source_kind=source_kind,
        entry_unit_id=None,
        core_unit_ids=core_ids,
        establishment_leave_unit_id=None,
        establishment_unit_ids=core_ids,
        **_completion_replacement_fields(leave, ret),
    )
    return fact, (*core, leave, ret)


@pytest.mark.parametrize(
    "source_kind",
    (SourceKind.SEGMENT, SourceKind.STROKE_OBSERVATION),
)
def test_physical_causal_center_records_and_validates_five_roles(
    source_kind: SourceKind,
) -> None:
    fact, units = _physical_center_fixture(source_kind)

    ledger = CausalStructureEventLedger(
        points=(),
        completed_trends=(),
        completed_units=units,
        center_completions=(fact,),
    )

    assert FACT_SCHEMA == "chanlun-fixed-year-symbol-facts-v14"
    assert fact.contract == CAUSAL_CENTER_COMPLETION_CONTRACT
    assert fact.establishment_unit_ids == (
        fact.entry_unit_id,
        *fact.core_unit_ids,
        fact.establishment_leave_unit_id,
    )
    assert len(set(fact.establishment_unit_ids)) == 5
    assert ledger.center_completions == (fact,)


def test_physical_causal_center_rejects_reordered_core_roles() -> None:
    fact, units = _physical_center_fixture()
    core_a, core_b, core_c = fact.core_unit_ids
    with pytest.raises(ValueError, match="immutable establishment roles"):
        reordered = replace(
            fact,
            core_unit_ids=(core_b, core_a, core_c),
            establishment_unit_ids=(
                fact.entry_unit_id,
                core_b,
                core_a,
                core_c,
                fact.establishment_leave_unit_id,
            ),
        )
        CausalStructureEventLedger(
            points=(),
            completed_trends=(),
            completed_units=units,
            center_completions=(reordered,),
        )


def test_causal_center_rejects_return_that_reenters_the_core() -> None:
    fact, units = _physical_center_fixture()
    forged_return = replace(units[-1], end_tick=105, low_tick=105)
    forged_fact = replace(
        fact,
        return_end_tick=forged_return.end_tick,
        return_low_tick=forged_return.low_tick,
    )

    with pytest.raises(ValueError, match="outside up-leave/down-return"):
        CausalStructureEventLedger(
            points=(),
            completed_trends=(),
            completed_units=(*units[:-1], forged_return),
            center_completions=(forged_fact,),
        )


def test_causal_center_fact_rejects_forged_center_identity() -> None:
    fact, _units = _physical_center_fixture()

    with pytest.raises(ValueError, match="immutable establishment roles"):
        replace(fact, center_id="forged-center")


def test_causal_center_rejects_disconnected_later_completion_leave() -> None:
    fact, units = _physical_center_fixture()
    forged_leave = _unit(
        "forged-later-leave",
        5,
        direction="up",
        start_tick=95,
        end_tick=125,
    )
    forged_return = _unit(
        "forged-later-return",
        6,
        direction="down",
        start_tick=125,
        end_tick=115,
    )
    forged_fact = replace(
        fact,
        available_at=forged_return.available_at,
        completed_at=forged_return.confirmed_at,
        leave_unit_id=forged_leave.unit_id,
        leave_market_start=forged_leave.market_start,
        leave_market_end=forged_leave.market_end,
        leave_available_at=forged_leave.available_at,
        leave_start_tick=forged_leave.start_tick,
        leave_end_tick=forged_leave.end_tick,
        leave_low_tick=forged_leave.low_tick,
        leave_high_tick=forged_leave.high_tick,
        return_unit_id=forged_return.unit_id,
        return_market_start=forged_return.market_start,
        return_market_end=forged_return.market_end,
        return_available_at=forged_return.available_at,
        return_start_tick=forged_return.start_tick,
        return_end_tick=forged_return.end_tick,
        return_low_tick=forged_return.low_tick,
        return_high_tick=forged_return.high_tick,
    )

    with pytest.raises(ValueError, match="lifecycle cannot be replayed"):
        CausalStructureEventLedger(
            points=(),
            completed_trends=(),
            completed_units=(*units[:-1], forged_leave, forged_return),
            center_completions=(forged_fact,),
        )


def test_causal_center_rejects_forged_unit_availability() -> None:
    fact, units = _physical_center_fixture()
    forged_fact = replace(
        fact,
        leave_available_at=fact.leave_available_at - timedelta(seconds=1),
    )

    with pytest.raises(ValueError, match="geometry changed"):
        CausalStructureEventLedger(
            points=(),
            completed_trends=(),
            completed_units=units,
            center_completions=(forged_fact,),
        )


def test_causal_center_rejects_completion_after_earlier_outside_return() -> None:
    fact, base = _physical_center_fixture()
    outside_up = _unit(
        "outside-up",
        6,
        direction="up",
        start_tick=115,
        end_tick=125,
    )
    late_reentry = _unit(
        "late-reentry",
        7,
        direction="down",
        start_tick=125,
        end_tick=105,
    )
    later_leave = _unit(
        "later-leave",
        8,
        direction="up",
        start_tick=105,
        end_tick=130,
    )
    later_return = _unit(
        "later-return",
        9,
        direction="down",
        start_tick=130,
        end_tick=115,
    )
    forged = replace(
        fact,
        **_completion_replacement_fields(later_leave, later_return),
    )

    with pytest.raises(ValueError, match="first outside return"):
        CausalStructureEventLedger(
            points=(),
            completed_trends=(),
            completed_units=(
                *base,
                outside_up,
                late_reentry,
                later_leave,
                later_return,
            ),
            center_completions=(forged,),
        )


def test_causal_center_replays_failed_departure_and_body_revision() -> None:
    fact, establishment = _physical_center_fixture()
    failed_return = replace(
        establishment[-1],
        end_tick=105,
        low_tick=105,
    )
    next_leave = _unit(
        "next-leave",
        6,
        direction="up",
        start_tick=105,
        end_tick=135,
    )
    next_return = _unit(
        "next-return",
        7,
        direction="down",
        start_tick=135,
        end_tick=120,
    )
    completed = replace(
        fact,
        body_revision=1,
        **_completion_replacement_fields(next_leave, next_return),
    )

    ledger = CausalStructureEventLedger(
        points=(),
        completed_trends=(),
        completed_units=(
            *establishment[:-1],
            failed_return,
            next_leave,
            next_return,
        ),
        center_completions=(completed,),
    )

    assert ledger.center_completions == (completed,)
    with pytest.raises(ValueError, match="body revision changed"):
        CausalStructureEventLedger(
            points=(),
            completed_trends=(),
            completed_units=ledger.completed_units,
            center_completions=(replace(completed, body_revision=0),),
        )


def test_recursive_causal_replay_accepts_closed_interval_boundary_leave() -> None:
    source_kind = SourceKind.TREND_TYPE
    core = (
        _unit(
            "touch-core-a",
            0,
            direction="down",
            start_tick=130,
            end_tick=90,
            source_kind=source_kind,
        ),
        _unit(
            "touch-core-b",
            1,
            direction="up",
            start_tick=90,
            end_tick=110,
            source_kind=source_kind,
        ),
        _unit(
            "touch-core-c",
            2,
            direction="down",
            start_tick=110,
            end_tick=110,
            source_kind=source_kind,
        ),
    )
    boundary_leave = _unit(
        "boundary-leave",
        3,
        direction="up",
        start_tick=110,
        end_tick=130,
        source_kind=source_kind,
    )
    outside_return = _unit(
        "boundary-return",
        4,
        direction="down",
        start_tick=130,
        end_tick=115,
        source_kind=source_kind,
    )
    physical, _units = _physical_center_fixture()
    core_ids = tuple(item.unit_id for item in core)
    completed = replace(
        physical,
        center_id=build_center_id(
            price_basis_revision=REVISION,
            structural_level=0,
            source_kind=source_kind.value,
            entry_unit_id=None,
            initial_unit_ids=core_ids,
            establishment_leave_unit_id=None,
            zd_tick=110,
            zg_tick=110,
        ),
        source_kind=source_kind,
        zd_tick=110,
        zg_tick=110,
        entry_unit_id=None,
        core_unit_ids=core_ids,
        establishment_leave_unit_id=None,
        establishment_unit_ids=core_ids,
        **_completion_replacement_fields(boundary_leave, outside_return),
    )

    ledger = CausalStructureEventLedger(
        points=(),
        completed_trends=(),
        completed_units=(
            *core,
            boundary_leave,
            outside_return,
        ),
        center_completions=(completed,),
    )

    assert ledger.center_completions == (completed,)


def test_recursive_causal_center_rejects_disconnected_lifecycle() -> None:
    fact, units = _recursive_center_fixture()
    disconnected_leave = _unit(
        "disconnected-recursive-leave",
        20,
        direction="up",
        start_tick=96,
        end_tick=130,
        source_kind=SourceKind.TREND_TYPE,
    )
    disconnected_return = _unit(
        "disconnected-recursive-return",
        21,
        direction="down",
        start_tick=130,
        end_tick=115,
        source_kind=SourceKind.TREND_TYPE,
    )
    forged = replace(
        fact,
        **_completion_replacement_fields(
            disconnected_leave,
            disconnected_return,
        ),
    )

    with pytest.raises(ValueError, match="lifecycle cannot be replayed"):
        CausalStructureEventLedger(
            points=(),
            completed_trends=(),
            completed_units=(*units[:3], disconnected_leave, disconnected_return),
            center_completions=(forged,),
        )


def test_recursive_causal_center_rejects_later_completion() -> None:
    fact, base = _recursive_center_fixture()
    outside_up = _unit(
        "recursive-outside-up",
        5,
        direction="up",
        start_tick=115,
        end_tick=125,
        source_kind=SourceKind.TREND_TYPE,
    )
    late_reentry = _unit(
        "recursive-late-reentry",
        6,
        direction="down",
        start_tick=125,
        end_tick=105,
        source_kind=SourceKind.TREND_TYPE,
    )
    later_leave = _unit(
        "recursive-later-leave",
        7,
        direction="up",
        start_tick=105,
        end_tick=130,
        source_kind=SourceKind.TREND_TYPE,
    )
    later_return = _unit(
        "recursive-later-return",
        8,
        direction="down",
        start_tick=130,
        end_tick=115,
        source_kind=SourceKind.TREND_TYPE,
    )
    forged = replace(
        fact,
        **_completion_replacement_fields(later_leave, later_return),
    )

    with pytest.raises(ValueError, match="first outside return"):
        CausalStructureEventLedger(
            points=(),
            completed_trends=(),
            completed_units=(
                *base,
                outside_up,
                late_reentry,
                later_leave,
                later_return,
            ),
            center_completions=(forged,),
        )


def test_physical_causal_center_rejects_old_contract() -> None:
    fact, _units = _physical_center_fixture()

    with pytest.raises(ValueError, match="contract changed"):
        replace(
            fact,
            contract="chanlun-causal-center-completion-v2-five-role",
        )


def test_recursive_causal_center_keeps_three_core_roles_only() -> None:
    fact, _units = _physical_center_fixture()
    recursive = replace(
        fact,
        center_id=build_center_id(
            price_basis_revision=REVISION,
            structural_level=0,
            source_kind=SourceKind.TREND_TYPE.value,
            entry_unit_id=None,
            initial_unit_ids=fact.core_unit_ids,
            establishment_leave_unit_id=None,
            zd_tick=fact.zd_tick,
            zg_tick=fact.zg_tick,
        ),
        source_kind=SourceKind.TREND_TYPE,
        entry_unit_id=None,
        establishment_leave_unit_id=None,
        establishment_unit_ids=fact.core_unit_ids,
    )

    assert recursive.establishment_unit_ids == recursive.core_unit_ids
    assert len(recursive.establishment_unit_ids) == 3
    with pytest.raises(ValueError, match="physical leave role"):
        replace(
            recursive,
            establishment_leave_unit_id="not-allowed",
        )


def test_fixed_year_v13_symbol_fact_schema_is_rejected() -> None:
    with pytest.raises(ValueError, match="unsupported fixed-year fact schema"):
        SymbolResearchFacts(
            schema="chanlun-fixed-year-symbol-facts-v13",
            algorithm_revision="sha256:" + "0" * 64,
            source_revision="sha256:" + "1" * 64,
            code="SZ.000001",
            sector_id="test-sector",
            requested_start=date(2026, 1, 1),
            requested_end=date(2026, 12, 31),
            effective_start=date(2026, 1, 1),
            row_counts=(),
            daily_points=(),
            thirty_points=(),
            five_points=(),
            one_points=(),
            evaluations=(),
        )
