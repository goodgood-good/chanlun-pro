from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Literal


DataEligibility = Literal[
    "FULL_SYSTEM_ELIGIBLE",
    "COMPONENT_ONLY",
    "RESEARCH_ONLY",
]


@dataclass(frozen=True, slots=True)
class DataContractEvidence:
    one_minute_available: bool
    five_minute_from_same_one_minute_source: bool
    thirty_minute_from_same_one_minute_source: bool
    daily_from_same_source: bool
    weekly_from_completed_daily: bool
    monthly_from_completed_daily: bool
    completed_bar_enforcement: bool
    point_in_time_adjustment_factors: bool
    point_in_time_security_master: bool
    point_in_time_sector_membership: bool
    point_in_time_suspension_st_limits: bool
    delisting_and_continuity_events: bool
    point_in_time_corporate_actions: bool
    point_in_time_fundamental_research: bool
    point_in_time_market_cap_and_peer_sets: bool
    t_plus_one_and_sellable_quantity: bool
    effective_fee_schedule: bool
    buy_sell_quantity_increments: bool
    historical_quotes_and_trades: bool
    frozen_broker_latency: bool
    survivorship_free_universe: bool
    missing_data_retained_as_rejection: bool
    historical_quotes_for_selection: bool
    source_ranges: tuple[tuple[str, str], ...] = ()
    coverage: tuple[tuple[str, Decimal], ...] = ()

    def __post_init__(self) -> None:
        names = tuple(name for name, _value in self.source_ranges)
        if len(names) != len(set(names)):
            raise ValueError("source range names must be unique")
        coverage_names = tuple(name for name, _value in self.coverage)
        if len(coverage_names) != len(set(coverage_names)):
            raise ValueError("coverage names must be unique")
        if any(not Decimal("0") <= value <= Decimal("1") for _, value in self.coverage):
            raise ValueError("coverage values must be in [0, 1]")


@dataclass(frozen=True, slots=True)
class DataGateResult:
    eligibility: DataEligibility
    full_system_failures: tuple[str, ...]
    component_failures: tuple[str, ...]
    warnings: tuple[str, ...]
    source_ranges: tuple[tuple[str, str], ...]
    coverage: tuple[tuple[str, Decimal], ...]
    pnl_evaluation_allowed: bool
    full_system_pnl_allowed: bool


@dataclass(frozen=True, slots=True)
class BarProxyDataGateResult:
    eligibility: DataEligibility
    execution_mode: Literal["BAR_CAUSAL_PROXY"]
    waived_requirements: tuple[str, ...]
    full_system_failures: tuple[str, ...]
    component_failures: tuple[str, ...]
    warnings: tuple[str, ...]
    source_ranges: tuple[tuple[str, str], ...]
    coverage: tuple[tuple[str, Decimal], ...]
    pnl_evaluation_allowed: bool
    full_system_pnl_allowed: bool


_COMPONENT_FIELDS = (
    "one_minute_available",
    "five_minute_from_same_one_minute_source",
    "thirty_minute_from_same_one_minute_source",
    "completed_bar_enforcement",
    "point_in_time_adjustment_factors",
    "t_plus_one_and_sellable_quantity",
    "effective_fee_schedule",
    "buy_sell_quantity_increments",
)

_FULL_FIELDS = _COMPONENT_FIELDS + (
    "daily_from_same_source",
    "weekly_from_completed_daily",
    "monthly_from_completed_daily",
    "point_in_time_security_master",
    "point_in_time_sector_membership",
    "point_in_time_suspension_st_limits",
    "delisting_and_continuity_events",
    "point_in_time_corporate_actions",
    "point_in_time_fundamental_research",
    "point_in_time_market_cap_and_peer_sets",
    "historical_quotes_and_trades",
    "frozen_broker_latency",
    "survivorship_free_universe",
    "missing_data_retained_as_rejection",
)

_BAR_PROXY_WAIVED_FIELDS = ("historical_trade_prints",)

_BAR_PROXY_FULL_FIELDS = tuple(
    name for name in _FULL_FIELDS if name != "historical_quotes_and_trades"
) + ("historical_quotes_for_selection",)


def audit_data_contract(evidence: DataContractEvidence) -> DataGateResult:
    component_failures = tuple(
        name for name in _COMPONENT_FIELDS if not getattr(evidence, name)
    )
    full_failures = tuple(
        name for name in _FULL_FIELDS if not getattr(evidence, name)
    )
    if not component_failures:
        eligibility: DataEligibility = (
            "FULL_SYSTEM_ELIGIBLE" if not full_failures else "COMPONENT_ONLY"
        )
    else:
        eligibility = "RESEARCH_ONLY"
    has_execution_evidence = (
        evidence.historical_quotes_and_trades
        and evidence.frozen_broker_latency
        and evidence.t_plus_one_and_sellable_quantity
        and evidence.effective_fee_schedule
        and evidence.buy_sell_quantity_increments
    )
    warnings: list[str] = []
    if not evidence.survivorship_free_universe:
        warnings.append("survivorship_bias_not_excluded")
    if not evidence.point_in_time_sector_membership:
        warnings.append("current_constituent_backfill_not_excluded")
    if not evidence.missing_data_retained_as_rejection:
        warnings.append("missing_data_deletion_bias_not_excluded")
    if not evidence.historical_quotes_and_trades:
        warnings.append("strict_historical_fill_validation_unavailable")
    return DataGateResult(
        eligibility=eligibility,
        full_system_failures=full_failures,
        component_failures=component_failures,
        warnings=tuple(warnings),
        source_ranges=evidence.source_ranges,
        coverage=evidence.coverage,
        pnl_evaluation_allowed=(eligibility != "RESEARCH_ONLY" and has_execution_evidence),
        full_system_pnl_allowed=(eligibility == "FULL_SYSTEM_ELIGIBLE"),
    )


def audit_bar_proxy_data_contract(
    evidence: DataContractEvidence,
) -> BarProxyDataGateResult:
    """Audit the explicit research-only completed-bar execution variant.

    Historical trade prints are waived only for this separately fingerprinted
    proxy.  Historical best quotes needed by selection, frozen confirmation
    timing and every other point-in-time, accounting, fee, quantity and
    universe requirement remain unchanged.
    """

    component_failures = tuple(
        name for name in _COMPONENT_FIELDS if not getattr(evidence, name)
    )
    full_failures = tuple(
        name for name in _BAR_PROXY_FULL_FIELDS if not getattr(evidence, name)
    )
    if not component_failures:
        eligibility: DataEligibility = (
            "FULL_SYSTEM_ELIGIBLE" if not full_failures else "COMPONENT_ONLY"
        )
    else:
        eligibility = "RESEARCH_ONLY"
    warnings = [
        "bar_proxy_is_not_tick_equivalent",
        "mixed_intrabar_cross_volume_is_unobservable_and_rejected",
        "historical_trade_print_requirement_explicitly_waived",
    ]
    if not evidence.survivorship_free_universe:
        warnings.append("survivorship_bias_not_excluded")
    if not evidence.point_in_time_sector_membership:
        warnings.append("current_constituent_backfill_not_excluded")
    if not evidence.missing_data_retained_as_rejection:
        warnings.append("missing_data_deletion_bias_not_excluded")
    return BarProxyDataGateResult(
        eligibility=eligibility,
        execution_mode="BAR_CAUSAL_PROXY",
        waived_requirements=_BAR_PROXY_WAIVED_FIELDS,
        full_system_failures=full_failures,
        component_failures=component_failures,
        warnings=tuple(warnings),
        source_ranges=evidence.source_ranges,
        coverage=evidence.coverage,
        pnl_evaluation_allowed=(eligibility != "RESEARCH_ONLY"),
        full_system_pnl_allowed=(eligibility == "FULL_SYSTEM_ELIGIBLE"),
    )


__all__ = [
    "DataEligibility",
    "DataContractEvidence",
    "DataGateResult",
    "BarProxyDataGateResult",
    "audit_bar_proxy_data_contract",
    "audit_data_contract",
]
