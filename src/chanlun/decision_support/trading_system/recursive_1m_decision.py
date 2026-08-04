from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from chanlun.core.strict_structure.models import CenterState, StrictStructureResult
from chanlun.core.strict_structure.upgrade_evidence import (
    UpgradeEvidenceKind,
    collect_recursive_upgrade_evidence,
)
from chanlun.decision_support.fingerprints import normalize_datetime
from chanlun.decision_support.trading_system.models import StructuralPoint
from chanlun.decision_support.trading_system.recursive_1m_research import (
    RESEARCH_STATUS,
    Recursive1mResearchParameters,
)
from chanlun.decision_support.trading_system.v3_parameters import LIVE_STATUS


@dataclass(frozen=True, slots=True)
class Recursive1mGateCheck:
    gate: str
    passed: bool
    code: str
    detail: str


@dataclass(frozen=True, slots=True)
class Recursive1mDataFacts:
    """Point-in-time data facts consumed by both replay and paper decisions.

    Structural eligibility is not enough when the price basis was assembled
    without an effective-dated corporate-action ledger.  Keeping this fact in
    the shared decision input prevents a batch prescreen from admitting a
    technically valid point that the historical data gate has already rejected.
    """

    complete_contiguous_interval: bool
    point_in_time_adjustment_complete: bool
    missing_data_inferred: bool
    source_fact_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        values = tuple(self.source_fact_ids)
        object.__setattr__(self, "source_fact_ids", values)
        if not values or any(not value.strip() for value in values):
            raise ValueError("recursive 1m data facts require source identities")
        if len(values) != len(set(values)):
            raise ValueError("recursive 1m data fact identities must be unique")


@dataclass(frozen=True, slots=True)
class Recursive1mEntryDecision:
    point_id: str
    decision_at: datetime
    parameter_set_id: str
    component_eligible: bool
    full_system_eligible: bool
    checks: tuple[Recursive1mGateCheck, ...]
    l1_context_ids: tuple[str, ...]
    l2_context_ids: tuple[str, ...]
    active_expansion_ids: tuple[str, ...]
    unresolved_components: tuple[str, ...]
    highest_status: str = RESEARCH_STATUS
    live_status: str = LIVE_STATUS

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "decision_at",
            normalize_datetime(self.decision_at, "decision_at"),
        )
        for field in (
            "l1_context_ids",
            "l2_context_ids",
            "active_expansion_ids",
            "unresolved_components",
        ):
            values = tuple(getattr(self, field))
            object.__setattr__(self, field, values)
            if len(values) != len(set(values)):
                raise ValueError(f"{field} must be unique")
        if self.full_system_eligible:
            raise ValueError("recursive 1m research cannot claim full-system eligibility")
        if self.highest_status != RESEARCH_STATUS or self.live_status != LIVE_STATUS:
            raise ValueError("recursive 1m decision cannot enable live trading")
        if self.component_eligible != all(check.passed for check in self.checks):
            raise ValueError("component eligibility must equal all gate checks")

    @property
    def passed_reason_codes(self) -> tuple[str, ...]:
        return tuple(item.code for item in self.checks if item.passed)

    @property
    def rejected_reason_codes(self) -> tuple[str, ...]:
        return tuple(item.code for item in self.checks if not item.passed)


@dataclass(frozen=True, slots=True)
class Recursive1mExitDecision:
    point_id: str
    cycle_id: str
    decision_at: datetime
    parameter_set_id: str
    exit_eligible: bool
    quantity: int
    persistence: Literal["NONE", "PERSISTENT_EXIT"]
    checks: tuple[Recursive1mGateCheck, ...]
    unresolved_components: tuple[str, ...]
    highest_status: str = RESEARCH_STATUS
    live_status: str = LIVE_STATUS

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "decision_at",
            normalize_datetime(self.decision_at, "decision_at"),
        )
        object.__setattr__(
            self,
            "unresolved_components",
            tuple(self.unresolved_components),
        )
        if self.quantity < 0:
            raise ValueError("recursive 1m exit quantity cannot be negative")
        if self.exit_eligible != all(check.passed for check in self.checks):
            raise ValueError("exit eligibility must equal all gate checks")
        if self.exit_eligible != (
            self.quantity > 0 and self.persistence == "PERSISTENT_EXIT"
        ):
            raise ValueError("eligible recursive 1m exit must be persistent")
        if self.full_system_eligible:
            raise ValueError("recursive 1m exit cannot claim full-system eligibility")
        if self.highest_status != RESEARCH_STATUS or self.live_status != LIVE_STATUS:
            raise ValueError("recursive 1m exit cannot enable live trading")

    @property
    def full_system_eligible(self) -> bool:
        return False

    @property
    def passed_reason_codes(self) -> tuple[str, ...]:
        return tuple(item.code for item in self.checks if item.passed)

    @property
    def rejected_reason_codes(self) -> tuple[str, ...]:
        return tuple(item.code for item in self.checks if not item.passed)


def _check(
    gate: str,
    passed: bool,
    pass_code: str,
    fail_code: str,
    detail: str,
) -> Recursive1mGateCheck:
    return Recursive1mGateCheck(
        gate=gate,
        passed=passed,
        code=pass_code if passed else fail_code,
        detail=detail,
    )


def evaluate_recursive_1m_entry(
    *,
    point: StructuralPoint,
    structure: StrictStructureResult,
    observed_at: datetime,
    parameters: Recursive1mResearchParameters,
    data_facts: Recursive1mDataFacts,
) -> Recursive1mEntryDecision:
    """Evaluate one L0=1m candidate with the shared causal research core.

    Both historical replay and future paper trading call this function.  The
    higher-level context is evaluated at the point's own availability time,
    never at ``observed_at`` or the final structure timestamp, preventing late
    L1/L2 evidence from retrospectively admitting an old L0 point.
    """

    observed = normalize_datetime(observed_at, "observed_at")
    decision_at = point.available_at
    if decision_at > observed:
        raise ValueError("recursive 1m point is not yet observable")
    if parameters.source_frequency != "1m":
        raise ValueError("recursive 1m research source frequency changed")

    visible_levels = structure.levels
    l0_centers = (
        visible_levels[0].center_result.centers if visible_levels else ()
    )
    point_center = next(
        (
            center
            for center in l0_centers
            if center.center_id == point.center_id
            and center.state is CenterState.COMPLETED
            and center.available_at <= decision_at
        ),
        None,
    )
    l1_centers = (
        tuple(
            center
            for center in visible_levels[1].center_result.centers
            if center.state is CenterState.COMPLETED
            and center.available_at <= decision_at
        )
        if len(visible_levels) >= 2
        else ()
    )
    l2_standard = (
        tuple(
            center
            for center in visible_levels[2].center_result.centers
            if center.state is CenterState.COMPLETED
            and center.available_at <= decision_at
        )
        if len(visible_levels) >= 3
        else ()
    )
    upgrades = collect_recursive_upgrade_evidence(
        structure,
        as_of=decision_at,
    )
    l2_derived = tuple(
        item
        for item in upgrades
        if item.kind is UpgradeEvidenceKind.NINE_SEGMENT_DERIVATION
        and item.target_level == 2
    )
    expansions = tuple(
        item
        for item in upgrades
        if item.kind is UpgradeEvidenceKind.CENTER_EXPANSION
        and (
            # L1→L2 is the active higher-level ambiguity and blocks every
            # new lower-level entry.  L0→L1 only invalidates this candidate
            # when the candidate's own center participates in the expansion;
            # an unrelated L0 pair must not veto the entire symbol forever.
            item.source_level == 1
            or (
                item.source_level == 0
                and point.center_id in item.source_center_ids
            )
        )
    )
    l1_context_ids = tuple(center.center_id for center in l1_centers)
    l2_context_ids = tuple(center.center_id for center in l2_standard) + tuple(
        item.evidence_id for item in l2_derived
    )
    expansion_ids = tuple(item.evidence_id for item in expansions)

    checks = (
        _check(
            "research_contract",
            not parameters.full_system_eligible
            and parameters.live_status == LIVE_STATUS,
            "PASS_RESEARCH_ONLY_LIVE_DISABLED",
            "REJECT_RESEARCH_CONTRACT_CHANGED",
            parameters.research_id,
        ),
        _check(
            "price_basis",
            point.price_basis_revision == structure.price_basis_revision,
            "PASS_SINGLE_PRICE_BASIS",
            "REJECT_PRICE_BASIS_MISMATCH",
            point.price_basis_revision,
        ),
        _check(
            "complete_contiguous_interval",
            data_facts.complete_contiguous_interval,
            "PASS_COMPLETE_CONTIGUOUS_INTERVAL",
            "REJECT_INCOMPLETE_OR_JOINED_INTERVAL",
            ",".join(data_facts.source_fact_ids),
        ),
        _check(
            "point_in_time_adjustment",
            data_facts.point_in_time_adjustment_complete,
            "PASS_PIT_ADJUSTMENT_LEDGER",
            "REJECT_PIT_ADJUSTMENT_UNAVAILABLE",
            ",".join(data_facts.source_fact_ids),
        ),
        _check(
            "missing_data_inference",
            not data_facts.missing_data_inferred,
            "PASS_NO_MISSING_DATA_INFERENCE",
            "REJECT_MISSING_DATA_WAS_INFERRED",
            str(data_facts.missing_data_inferred).lower(),
        ),
        _check(
            "l0_identity",
            point.confirmed
            and point.source_frequency == "1m"
            and point.recursive_level == 0,
            "PASS_L0_1M_CONFIRMED_POINT",
            "REJECT_NOT_L0_1M_CONFIRMED_POINT",
            f"{point.source_frequency}/level-{point.recursive_level}/{point.status}",
        ),
        _check(
            "l0_first_center_third_buy",
            point.point_type == "3buy"
            and point.side == "buy"
            and point.center_ordinal == 1
            and point_center is not None,
            "PASS_L0_FIRST_CENTER_THIRD_BUY",
            "REJECT_L0_FIRST_CENTER_THIRD_BUY",
            f"type={point.point_type}; ordinal={point.center_ordinal}",
        ),
        _check(
            "l1_context",
            bool(l1_context_ids),
            "PASS_L1_CONTEXT_VISIBLE_BEFORE_ENTRY",
            "REJECT_L1_CONTEXT_MISSING_AT_ENTRY",
            str(len(l1_context_ids)),
        ),
        _check(
            "l2_context",
            bool(l2_context_ids),
            "PASS_L2_CONTEXT_VISIBLE_BEFORE_ENTRY",
            "REJECT_L2_CONTEXT_MISSING_AT_ENTRY",
            f"standard={len(l2_standard)}; nine={len(l2_derived)}",
        ),
        _check(
            "expansion_state",
            not expansion_ids,
            "PASS_NO_ACTIVE_EXPANSION_RECLASSIFICATION",
            "REJECT_ACTIVE_EXPANSION_RECLASSIFYING",
            ",".join(expansion_ids) or "none",
        ),
    )
    unresolved = (
        "UNRESOLVED_LOWER_LEVEL_LOCATOR_BELOW_L0_1M",
        "UNRESOLVED_TACTICAL_LAYER_BELOW_L0_1M",
    )
    return Recursive1mEntryDecision(
        point_id=point.point_id,
        decision_at=decision_at,
        parameter_set_id=parameters.parameter_set_id,
        component_eligible=all(item.passed for item in checks),
        full_system_eligible=False,
        checks=checks,
        l1_context_ids=l1_context_ids,
        l2_context_ids=l2_context_ids,
        active_expansion_ids=expansion_ids,
        unresolved_components=unresolved,
    )


def evaluate_recursive_1m_exit(
    *,
    point: StructuralPoint,
    observed_at: datetime,
    parameters: Recursive1mResearchParameters,
    data_facts: Recursive1mDataFacts,
    cycle_id: str,
    position_opened_at: datetime,
    position_price_basis_revision: str,
    position_quantity: int,
) -> Recursive1mExitDecision:
    """Promote only a completed L0=1m third sell to a persistent exit.

    The other frozen V3 strategic exits require entry-relative L1 cycle or
    divergence facts that the research hierarchy does not yet produce.  They
    remain explicit unresolved components instead of being approximated from
    prices.  Replay and future paper adapters call this same pure function.
    """

    observed = normalize_datetime(observed_at, "observed_at")
    opened = normalize_datetime(position_opened_at, "position_opened_at")
    decision_at = point.available_at
    if decision_at > observed:
        raise ValueError("recursive 1m exit point is not yet observable")
    if parameters.source_frequency != "1m":
        raise ValueError("recursive 1m research source frequency changed")

    checks = (
        _check(
            "research_contract",
            not parameters.full_system_eligible
            and parameters.live_status == LIVE_STATUS,
            "PASS_RESEARCH_ONLY_LIVE_DISABLED",
            "REJECT_RESEARCH_CONTRACT_CHANGED",
            parameters.research_id,
        ),
        _check(
            "complete_contiguous_interval",
            data_facts.complete_contiguous_interval,
            "PASS_COMPLETE_CONTIGUOUS_INTERVAL",
            "REJECT_INCOMPLETE_OR_JOINED_INTERVAL",
            ",".join(data_facts.source_fact_ids),
        ),
        _check(
            "point_in_time_adjustment",
            data_facts.point_in_time_adjustment_complete,
            "PASS_PIT_ADJUSTMENT_LEDGER",
            "REJECT_PIT_ADJUSTMENT_UNAVAILABLE",
            ",".join(data_facts.source_fact_ids),
        ),
        _check(
            "missing_data_inference",
            not data_facts.missing_data_inferred,
            "PASS_NO_MISSING_DATA_INFERENCE",
            "REJECT_MISSING_DATA_WAS_INFERRED",
            str(data_facts.missing_data_inferred).lower(),
        ),
        _check(
            "active_position",
            bool(cycle_id) and position_quantity > 0 and opened < decision_at,
            "PASS_ACTIVE_POSITION_PRECEDES_EXIT",
            "REJECT_NO_CAUSAL_ACTIVE_POSITION",
            f"cycle={cycle_id}; quantity={position_quantity}; opened={opened.isoformat()}",
        ),
        _check(
            "position_price_basis",
            point.price_basis_revision == position_price_basis_revision,
            "PASS_POSITION_PRICE_BASIS_MATCH",
            "REJECT_POSITION_PRICE_BASIS_MISMATCH",
            point.price_basis_revision,
        ),
        _check(
            "l0_third_sell",
            point.confirmed
            and point.source_frequency == "1m"
            and point.recursive_level == 0
            and point.point_type == "3sell"
            and point.side == "sell",
            "PASS_L0_THIRD_SELL_FULL_EXIT",
            "REJECT_NOT_L0_THIRD_SELL",
            f"{point.source_frequency}/level-{point.recursive_level}/{point.point_type}",
        ),
    )
    eligible = all(item.passed for item in checks)
    return Recursive1mExitDecision(
        point_id=point.point_id,
        cycle_id=cycle_id,
        decision_at=decision_at,
        parameter_set_id=parameters.parameter_set_id,
        exit_eligible=eligible,
        quantity=position_quantity if eligible else 0,
        persistence="PERSISTENT_EXIT" if eligible else "NONE",
        checks=checks,
        unresolved_components=(
            "UNRESOLVED_FIRST_UP_LEG_FAILED",
            "UNRESOLVED_SECOND_SELL_CONFIRM",
            "UNRESOLVED_L0_UPMOVE_DIVERGENCE",
            "UNRESOLVED_TACTICAL_LAYER_BELOW_L0_1M",
        ),
    )


__all__ = (
    "Recursive1mDataFacts",
    "Recursive1mEntryDecision",
    "Recursive1mExitDecision",
    "Recursive1mGateCheck",
    "evaluate_recursive_1m_entry",
    "evaluate_recursive_1m_exit",
)
