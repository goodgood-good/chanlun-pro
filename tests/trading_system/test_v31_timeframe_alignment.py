from datetime import datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

import pandas as pd

from chanlun.decision_support.trading_system.backtest.fixed_year import (
    CausalCenterCompletionFact,
)
from chanlun.decision_support.trading_system.models import StructuralPoint
from chanlun.decision_support.trading_system.v3_timeframe_alignment import (
    CompletedL1TrendFact,
)
from chanlun.decision_support.trading_system.v31_structure_adapter import (
    build_v31_technical_entry_snapshot,
)
from chanlun.decision_support.trading_system.v31_timeframe_alignment import (
    ConfirmationBarFact,
    CompletedL1UnitFact,
    align_v31_independent_entry_chains,
    align_v31_independent_unit_entry_chains,
    confirmation_bar_fact,
    v31_alignment_contract,
)


CN = ZoneInfo("Asia/Shanghai")


def at(hour: int, minute: int) -> datetime:
    return datetime(2026, 1, 5, hour, minute, tzinfo=CN)


def l0_point() -> StructuralPoint:
    return StructuralPoint(
        point_id="l0:3buy",
        code="SH.510300",
        point_type="3buy",
        side="buy",
        status="confirmed",
        variant="standard",
        source_frequency="30m",
        price_basis_revision="revision:v1",
        tower="formal",
        recursive_level=0,
        anchor_at=at(11, 55),
        confirmed_at=at(12, 30),
        available_at=at(12, 30),
        structure_anchor_price=3.30,
        structure_invalidation_price=3.20,
        center_id="center:l0",
        center_zd=3.10,
        center_zg=3.20,
        center_ordinal=1,
        divergence_kind=None,
        parent_point_id=None,
        evidence_codes=("complete_leave", "complete_first_return"),
    )


def l2_point(*, available_at: datetime = at(12, 0)) -> StructuralPoint:
    return StructuralPoint(
        point_id="l2:1buy",
        code="SH.510300",
        point_type="1buy",
        side="buy",
        status="confirmed",
        variant="standard",
        source_frequency="1m",
        price_basis_revision="revision:v1",
        tower="formal",
        recursive_level=0,
        anchor_at=at(11, 50),
        confirmed_at=available_at,
        available_at=available_at,
        structure_anchor_price=3.21,
        structure_invalidation_price=3.21,
        center_id="center:l2",
        center_zd=3.22,
        center_zg=3.24,
        center_ordinal=None,
        divergence_kind="trend",
        parent_point_id=None,
        evidence_codes=("formal_trend",),
    )


def center() -> CausalCenterCompletionFact:
    return CausalCenterCompletionFact(
        center_id="center:l0",
        source_frequency="30m",
        structural_level=0,
        price_basis_revision="revision:v1",
        body_revision=1,
        available_at=at(12, 30),
        completed_at=at(12, 30),
        zd_tick=310,
        zg_tick=320,
        leave_unit_id="leave:l0",
        leave_direction="up",
        leave_market_start=at(10, 0),
        leave_market_end=at(11, 0),
        leave_available_at=at(11, 0),
        leave_start_tick=315,
        leave_end_tick=335,
        leave_low_tick=310,
        leave_high_tick=335,
        return_unit_id="return:l0",
        return_direction="down",
        return_market_start=at(11, 0),
        return_market_end=at(11, 55),
        return_available_at=at(12, 30),
        return_start_tick=335,
        return_end_tick=330,
        return_low_tick=320,
        return_high_tick=335,
    )


def trend(
    trend_id: str,
    direction: str,
    start: datetime,
    end: datetime,
    available: datetime,
    start_price: str,
    end_price: str,
    low: str,
    high: str,
    terminal_start: datetime,
) -> CompletedL1TrendFact:
    return CompletedL1TrendFact(
        trend_id=trend_id,
        source_frequency="5m",
        recursive_level=0,
        price_basis_revision="revision:v1",
        direction=direction,
        market_start=start,
        market_end=end,
        confirmed_at=available,
        available_at=available,
        start_price=Decimal(start_price),
        end_price=Decimal(end_price),
        low_price=Decimal(low),
        high_price=Decimal(high),
        terminal_start=terminal_start,
        terminal_end=end,
        evidence_unit_ids=(f"{trend_id}:1", f"{trend_id}:2"),
    )


def l1_trends() -> tuple[CompletedL1TrendFact, ...]:
    return (
        trend(
            "l1:departure",
            "up",
            at(10, 5),
            at(10, 55),
            at(11, 0),
            "3.15",
            "3.35",
            "3.12",
            "3.35",
            at(10, 45),
        ),
        trend(
            "l1:return",
            "down",
            at(11, 5),
            at(11, 55),
            at(12, 0),
            "3.35",
            "3.25",
            "3.20",
            "3.35",
            at(11, 45),
        ),
    )


def bar() -> ConfirmationBarFact:
    return ConfirmationBarFact(
        point_id="l2:1buy",
        source_frequency="1m",
        available_at=at(12, 0),
        raw_open=Decimal("3.22"),
        raw_high=Decimal("3.24"),
        raw_low=Decimal("3.21"),
        raw_close=Decimal("3.23"),
    )


def unit(
    unit_id: str,
    direction: str,
    start: datetime,
    end: datetime,
    available: datetime,
    start_price: str,
    end_price: str,
    low: str,
    high: str,
) -> CompletedL1UnitFact:
    return CompletedL1UnitFact(
        unit_id=unit_id,
        source_frequency="5m",
        structural_level=0,
        price_basis_revision="revision:v1",
        direction=direction,  # type: ignore[arg-type]
        market_start=start,
        market_end=end,
        confirmed_at=available,
        available_at=available,
        start_price=Decimal(start_price),
        end_price=Decimal(end_price),
        low_price=Decimal(low),
        high_price=Decimal(high),
        child_ids=(),
    )


def l1_units() -> tuple[CompletedL1UnitFact, ...]:
    return (
        unit(
            "l1-unit:departure",
            "up",
            at(10, 5),
            at(10, 55),
            at(11, 0),
            "3.15",
            "3.35",
            "3.12",
            "3.35",
        ),
        unit(
            "l1-unit:return",
            "down",
            at(11, 5),
            at(11, 55),
            at(12, 0),
            "3.35",
            "3.25",
            "3.20",
            "3.35",
        ),
    )


def align(**changes):
    values = {
        "l0_points": (l0_point(),),
        "l0_center_completions": (center(),),
        "l1_trends": l1_trends(),
        "l2_points": (l2_point(),),
        "confirmation_bars": {"l2:1buy": bar()},
        "l0_price_quantum": Decimal("0.01"),
    }
    values.update(changes)
    return align_v31_independent_entry_chains(**values)[0]


def align_units(**changes):
    values = {
        "l0_points": (l0_point(),),
        "l0_center_completions": (center(),),
        "l1_units": l1_units(),
        "l2_points": (l2_point(),),
        "confirmation_bars": {"l2:1buy": bar()},
        "l0_price_quantum": Decimal("0.01"),
    }
    values.update(changes)
    return align_v31_independent_unit_entry_chains(**values)[0]


def test_v31_alignment_uses_center_leave_and_return_not_point_anchor_window() -> None:
    decision = align()
    assert decision.status == "PASS"
    assert decision.window_start == at(10, 0)
    assert decision.window_end == at(11, 55)
    assert decision.chain is not None
    assert decision.chain.l2_confirmation_bar_high == Decimal("3.24")
    assert decision.chain.structural_invalidation_price == Decimal("3.20")


def test_equal_l1_return_low_at_zg_is_valid() -> None:
    assert align().status == "PASS"


def test_trends_after_l0_return_are_not_misread_as_formation_evidence() -> None:
    late = trend(
        "late",
        "up",
        at(12, 0),
        at(12, 20),
        at(12, 25),
        "3.15",
        "3.35",
        "3.15",
        "3.35",
        at(12, 10),
    )
    decision = align(l1_trends=(late,))
    assert decision.status == "REJECT"
    assert decision.reason_codes == (
        "NO_COMPLETED_L1_UP_DEPARTURE_ALIGNED_WITH_L0_LEAVE_UNIT",
    )


def test_missing_confirmation_bar_or_future_locator_fails_closed() -> None:
    assert align(confirmation_bars={}).reason_codes == (
        "L2_CONFIRMATION_BAR_EVIDENCE_MISSING",
    )
    assert align(l2_points=(l2_point(available_at=at(12, 31)),)).reason_codes == (
        "NO_L2_LOCATOR_AT_FIRST_L1_RETURN_TERMINAL",
    )


def test_confirmation_bar_reads_raw_high_not_structural_anchor() -> None:
    point = l2_point()
    frame = pd.DataFrame(
        [
            {
                "date": point.available_at,
                "raw_open": 3.22,
                "raw_high": 3.24,
                "raw_low": 3.21,
                "raw_close": 3.23,
            }
        ]
    )
    fact = confirmation_bar_fact(point, frame)
    assert fact.raw_high == Decimal("3.24")
    assert fact.raw_high != Decimal(str(point.structure_anchor_price))


def test_v31_adapter_preserves_confirmation_bar_high() -> None:
    decision = align()
    assert decision.chain is not None
    snapshot = build_v31_technical_entry_snapshot(
        structure_snapshot_id="structure:v31",
        observed_at=decision.chain.decision_at,
        chain=decision.chain,
        l0_three_buy=l0_point(),
        l2_locator=l2_point(),
    )
    assert snapshot.l2_confirmation_bar_high == Decimal("3.24")
    assert snapshot.level_relation_resolved


def test_v31_unit_alignment_remains_a_compatible_non_active_diagnostic() -> None:
    decision = align_units()
    assert decision.status == "PASS"
    assert decision.chain is not None
    assert decision.chain.l1_evidence_kind == "COMPLETED_CONSTITUENT_UNIT"
    assert decision.chain.return_low == Decimal("3.20")
    assert decision.chain.l2_confirmation_bar_high == Decimal("3.24")
    assert (
        v31_alignment_contract().l1_evidence_kind
        == "LOCKED_COMPLETED_5M_LEVEL_ZERO_TREND"
    )


def test_v31_unit_alignment_requires_the_first_completed_down_return() -> None:
    departure = l1_units()[0]
    decision = align_units(l1_units=(departure,))
    assert decision.status == "REJECT"
    assert decision.reason_codes == (
        "NO_SUBSEQUENT_COMPLETED_L1_DOWN_UNIT",
    )


def test_v31_unit_alignment_is_prefix_invariant_to_later_units() -> None:
    original = align_units()
    later = unit(
        "l1-unit:future",
        "up",
        at(13, 0),
        at(13, 30),
        at(13, 35),
        "3.25",
        "3.40",
        "3.25",
        "3.40",
    )
    appended = align_units(l1_units=l1_units() + (later,))
    assert appended == original


def test_independent_l1_departure_need_not_start_inside_or_be_contained() -> None:
    departure = trend(
        "l1:cross-partition-departure",
        "up",
        at(9, 55),
        at(10, 55),
        at(11, 0),
        "3.00",
        "3.35",
        "2.99",
        "3.35",
        at(10, 45),
    )
    decision = align(l1_trends=(departure, l1_trends()[1]))
    assert decision.status == "PASS"
    assert decision.chain is not None
    assert decision.chain.l1_departure_evidence_id == departure.trend_id


def test_first_subsequent_l1_return_cannot_be_skipped_for_later_alignment() -> None:
    early = trend(
        "l1:first-down-before-return-unit",
        "down",
        at(10, 55),
        at(10, 59),
        at(11, 0),
        "3.35",
        "3.30",
        "3.25",
        "3.35",
        at(10, 57),
    )
    decision = align(l1_trends=(l1_trends()[0], early, l1_trends()[1]))
    assert decision.status == "REJECT"
    assert decision.reason_codes == (
        "FIRST_COMPLETED_L1_DOWN_RETURN_NOT_ALIGNED_WITH_L0_RETURN_UNIT",
    )


def test_independent_l1_unit_need_not_start_inside_or_be_contained() -> None:
    departure = unit(
        "l1-unit:cross-partition-departure",
        "up",
        at(9, 55),
        at(10, 55),
        at(11, 0),
        "3.00",
        "3.35",
        "2.99",
        "3.35",
    )
    decision = align_units(l1_units=(departure, l1_units()[1]))
    assert decision.status == "PASS"
    assert decision.chain is not None
    assert decision.chain.l1_departure_evidence_id == departure.unit_id


def test_first_subsequent_l1_unit_cannot_be_skipped_for_later_alignment() -> None:
    early = unit(
        "l1-unit:first-down-before-return-unit",
        "down",
        at(10, 55),
        at(10, 59),
        at(11, 0),
        "3.35",
        "3.30",
        "3.25",
        "3.35",
    )
    decision = align_units(l1_units=(l1_units()[0], early, l1_units()[1]))
    assert decision.status == "REJECT"
    assert decision.reason_codes == (
        "FIRST_COMPLETED_L1_DOWN_UNIT_NOT_ALIGNED_WITH_L0_RETURN_UNIT",
    )
