from __future__ import annotations

from datetime import date, datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

from chanlun.decision_support.trading_system.backtest.fixed_year import (
    CausalDirectRecursiveDecisionFact,
)
from chanlun.decision_support.trading_system.models import StructuralPoint
from chanlun.decision_support.trading_system.v3_technical_approximation import (
    approximate_technical_entry_decision,
    bind_approximate_entry_chain,
    technical_approximation_alignment_contract,
    technical_approximation_parameters,
)


CN = ZoneInfo("Asia/Shanghai")
CODE = "SZ.000001"


def at(day: int, hour: int = 10) -> datetime:
    return datetime(2026, 1, 5, hour, tzinfo=CN) + timedelta(days=day)


def point(
    identity: str,
    point_type: str,
    *,
    level: int,
    observed_at: datetime,
) -> StructuralPoint:
    side = "buy" if point_type.endswith("buy") else "sell"
    return StructuralPoint(
        point_id=identity,
        code=CODE,
        point_type=point_type,
        side=side,
        status="confirmed",
        variant="standard",
        source_frequency="1m",
        price_basis_revision="sha256:" + "1" * 64,
        tower="formal",
        recursive_level=level,
        anchor_at=observed_at,
        confirmed_at=observed_at,
        available_at=observed_at,
        structure_anchor_price=Decimal("10.5"),
        structure_invalidation_price=(
            Decimal("10") if side == "buy" else Decimal("11")
        ),
        center_id=f"center-{identity}",
        center_zd=Decimal("9.8"),
        center_zg=Decimal("10"),
        center_ordinal=1,
        divergence_kind=None,
        parent_point_id=None,
        evidence_codes=("test",),
    )


def rejected_fact(
    strategic: StructuralPoint,
    *reasons: str,
) -> CausalDirectRecursiveDecisionFact:
    return CausalDirectRecursiveDecisionFact(
        l0_point_id=strategic.point_id,
        first_seen_at=strategic.available_at,
        status="REJECT",
        reason_codes=tuple(reasons),
        structure_snapshot_id="sha256:" + "2" * 64,
        technical_entry=None,
    )


def sessions(count: int = 15) -> tuple[date, ...]:
    return tuple(at(index).date() for index in range(count))


def test_waivable_recursive_reasons_become_warnings_with_causal_locator() -> None:
    strategic = point("strategic", "3buy", level=2, observed_at=at(0))
    locator = point("locator", "3buy", level=0, observed_at=at(2))
    strict = rejected_fact(
        strategic,
        "ACTIVE_CENTER_EXPANSION_RECLASSIFYING",
        "NO_L2_1M_LOCATOR_IN_DIRECT_FIRST_RETURN",
    )

    result = approximate_technical_entry_decision(
        strict_decision=strict,
        strategic_point=strategic,
        structural_points=(strategic, locator),
        trading_sessions=sessions(),
    )

    assert result.status == "PASS"
    assert result.locator_point_id == locator.point_id
    assert result.locator_point_type == "3buy"
    assert result.locator_delay_sessions == 2
    assert result.confidence == "MEDIUM"
    assert result.reason_codes == ()
    assert result.warning_codes == (
        "APPROXIMATION_WARNING_ACTIVE_CENTER_EXPANSION_RECLASSIFYING",
        "APPROXIMATION_WARNING_NO_L2_1M_LOCATOR_IN_DIRECT_FIRST_RETURN",
    )
    assert result.live_status == "LIVE_DISABLED"


def test_non_geometric_shortcut_is_not_waived() -> None:
    strategic = point("strategic", "3buy", level=2, observed_at=at(0))
    locator = point("locator", "3buy", level=0, observed_at=at(1))
    strict = rejected_fact(strategic, "L0_30M_STANDARD_CENTER_NOT_COMPLETED")

    result = approximate_technical_entry_decision(
        strict_decision=strict,
        strategic_point=strategic,
        structural_points=(strategic, locator),
        trading_sessions=sessions(),
    )

    assert result.status == "REJECT"
    assert result.reason_codes == (
        "REJECT_UNSUPPORTED_STRICT_STRUCTURE_FAILURE",
        "L0_30M_STANDARD_CENTER_NOT_COMPLETED",
    )


def test_locator_wait_is_bounded_by_trading_sessions() -> None:
    strategic = point("strategic", "3buy", level=2, observed_at=at(0))
    locator = point("locator", "3buy", level=0, observed_at=at(11))
    strict = rejected_fact(strategic, "NO_L2_1M_LOCATOR_IN_DIRECT_FIRST_RETURN")

    result = approximate_technical_entry_decision(
        strict_decision=strict,
        strategic_point=strategic,
        structural_points=(strategic, locator),
        trading_sessions=sessions(),
    )

    assert result.status == "REJECT"
    assert result.reason_codes == (
        "REJECT_APPROXIMATE_1M_LOCATOR_WAIT_EXCEEDED",
    )


def test_passing_decision_binds_hash_identified_approximate_chain() -> None:
    strategic = point("strategic", "3buy", level=2, observed_at=at(0))
    locator = point("locator", "3buy", level=0, observed_at=at(1))
    strict = rejected_fact(strategic, "NO_L2_1M_LOCATOR_IN_DIRECT_FIRST_RETURN")
    decision = approximate_technical_entry_decision(
        strict_decision=strict,
        strategic_point=strategic,
        structural_points=(strategic, locator),
        trading_sessions=sessions(),
    )

    chain = bind_approximate_entry_chain(
        decision=decision,
        strategic_point=strategic,
        locator_point=locator,
        point_anchor_unit_ids={strategic.point_id: "l2-return", locator.point_id: "l0-locator"},
        confirmation_bar_high=Decimal("10.80"),
    )

    assert chain.strategic_anchor_unit_id == "l2-return"
    assert chain.locator_anchor_unit_id == "l0-locator"
    assert chain.l2_confirmation_bar_high == Decimal("10.80")
    assert chain.chain_id.startswith("sha256:")


def test_parameter_and_alignment_contracts_are_frozen_and_live_disabled() -> None:
    parameters = technical_approximation_parameters()
    alignment = technical_approximation_alignment_contract()

    assert parameters.parameter_set_id.startswith("sha256:")
    assert parameters.live_status == "LIVE_DISABLED"
    assert alignment.technical_parameter_set_id == parameters.parameter_set_id
    assert alignment.parameter_set_id.startswith("sha256:")
    assert alignment.live_status == "LIVE_DISABLED"
