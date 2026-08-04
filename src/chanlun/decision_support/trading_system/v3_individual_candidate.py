"""One auditable individual-stock candidate path into the shared V3 core."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from chanlun.decision_support.fingerprints import normalize_datetime
from chanlun.decision_support.trading_system.v3_etf_proxy_facts import FactBlocker
from chanlun.decision_support.trading_system.v3_individual_research import (
    IndividualSelectionFacts,
)
from chanlun.decision_support.trading_system.v3_parameters import (
    StrategyV3Parameters,
    individual_parameter_snapshot,
)
from chanlun.decision_support.trading_system.v3_qmt_higher_timeframe import (
    QmtHigherTimeframeRiskEnvelope,
)
from chanlun.decision_support.trading_system.v3_sector_trigger import (
    SectorTriggerSnapshot,
)
from chanlun.decision_support.trading_system.v3_selection import (
    AccountEntryGate,
    CandidateDecision,
    CandidateSnapshot,
    SectorStrengthSnapshot,
    TechnicalEntrySnapshot,
    TradeabilitySnapshot,
    evaluate_candidate,
)


@dataclass(frozen=True, slots=True)
class IndividualCandidateDecisionFacts:
    selection: IndividualSelectionFacts
    sector_trigger: SectorTriggerSnapshot
    market_risk: QmtHigherTimeframeRiskEnvelope
    sector_risk: QmtHigherTimeframeRiskEnvelope
    symbol_risk: QmtHigherTimeframeRiskEnvelope
    candidate_snapshot: CandidateSnapshot | None
    decision: CandidateDecision | None
    grade: str
    blockers: tuple[FactBlocker, ...]

    @property
    def full_system_eligible(self) -> bool:
        return (
            self.grade == "FULL_SYSTEM_ELIGIBLE"
            and self.decision is not None
            and self.decision.accepted
        )


def _research_blockers(selection: IndividualSelectionFacts) -> tuple[FactBlocker, ...]:
    return tuple(
        FactBlocker(value.field, value.code, value.detail)
        for value in selection.blockers
    )


def build_individual_candidate_decision(
    *,
    decision_time: datetime,
    selection: IndividualSelectionFacts,
    sector_trigger: SectorTriggerSnapshot,
    market_risk: QmtHigherTimeframeRiskEnvelope,
    sector_risk: QmtHigherTimeframeRiskEnvelope,
    symbol_risk: QmtHigherTimeframeRiskEnvelope,
    tradeability: TradeabilitySnapshot,
    sector_strength: SectorStrengthSnapshot,
    technical: TechnicalEntrySnapshot,
    account: AccountEntryGate,
    parameters: StrategyV3Parameters | None = None,
    market: str = "A",
) -> IndividualCandidateDecisionFacts:
    """Join outer facts, then call the same pure V3 candidate evaluator."""

    decision_time = normalize_datetime(decision_time, "decision_time")
    actual_parameters = parameters or individual_parameter_snapshot()
    blockers: list[FactBlocker] = [
        *_research_blockers(selection),
        *market_risk.blockers,
        *sector_risk.blockers,
        *symbol_risk.blockers,
    ]
    if actual_parameters.selection_path != "INDIVIDUAL_THREE_PROGRAM":
        blockers.append(
            FactBlocker(
                "selection_path",
                "INDIVIDUAL_PARAMETER_SNAPSHOT_REQUIRED",
                actual_parameters.selection_path,
            )
        )
    if selection.snapshot is None:
        blockers.append(
            FactBlocker(
                "selection_snapshot",
                "INDIVIDUAL_THREE_PROGRAM_SNAPSHOT_UNAVAILABLE",
                selection.evidence_bundle_id,
            )
        )
    if not sector_trigger.passes(decision_time):
        blockers.append(
            FactBlocker(
                "sector_trigger",
                "QMT_SECTOR_TRIGGER_NOT_ELIGIBLE",
                sector_trigger.snapshot_id,
            )
        )
    for label, envelope in (
        ("market", market_risk),
        ("sector", sector_risk),
        ("symbol", symbol_risk),
    ):
        if envelope.risk.snapshot is None:
            blockers.append(
                FactBlocker(
                    f"{label}_risk",
                    f"{label.upper()}_HIGHER_TIMEFRAME_RISK_UNAVAILABLE",
                    envelope.inputs.source_revision,
                )
            )
    symbol = None if selection.snapshot is None else selection.snapshot.symbol
    if symbol is not None and (
        tradeability.symbol != symbol
        or sector_trigger.symbol != symbol
        or symbol_risk.inputs.symbol != symbol
    ):
        blockers.append(
            FactBlocker(
                "symbol_identity",
                "INDIVIDUAL_CANDIDATE_SYMBOL_MISMATCH",
                (
                    f"selection={symbol}; tradeability={tradeability.symbol}; "
                    f"trigger={sector_trigger.symbol}; risk={symbol_risk.inputs.symbol}"
                ),
            )
        )
    if sector_strength.sector_id != sector_trigger.sector_id:
        blockers.append(
            FactBlocker(
                "sector_identity",
                "INDIVIDUAL_CANDIDATE_SECTOR_MISMATCH",
                (
                    f"trigger={sector_trigger.sector_id}; "
                    f"strength={sector_strength.sector_id}"
                ),
            )
        )

    candidate = None
    evaluated = None
    can_evaluate = (
        actual_parameters.selection_path == "INDIVIDUAL_THREE_PROGRAM"
        and selection.snapshot is not None
        and market_risk.risk.snapshot is not None
        and sector_risk.risk.snapshot is not None
        and symbol_risk.risk.snapshot is not None
        and sector_trigger.passes(decision_time)
        and symbol == tradeability.symbol == sector_trigger.symbol == symbol_risk.inputs.symbol
        and sector_strength.sector_id == sector_trigger.sector_id
    )
    if can_evaluate:
        candidate = CandidateSnapshot(
            symbol=symbol,
            market=market,
            sector_id=sector_trigger.sector_id,
            decision_time=decision_time,
            research=selection.snapshot,
            tradeability=tradeability,
            market_risk=market_risk.risk.snapshot,
            sector_risk=sector_risk.risk.snapshot,
            symbol_risk=symbol_risk.risk.snapshot,
            sector_strength=sector_strength,
            technical=technical,
            account=account,
            sector_trigger=sector_trigger,
        )
        evaluated = evaluate_candidate(candidate, actual_parameters)

    all_component_grades_full = all(
        value == "FULL_SYSTEM_ELIGIBLE"
        for value in (
            selection.grade,
            market_risk.grade,
            sector_risk.grade,
            symbol_risk.grade,
        )
    )
    if evaluated is None:
        grade = "UNRESOLVED"
    elif evaluated.accepted and all_component_grades_full and not blockers:
        grade = "FULL_SYSTEM_ELIGIBLE"
    else:
        grade = "RESEARCH_ONLY"
    return IndividualCandidateDecisionFacts(
        selection=selection,
        sector_trigger=sector_trigger,
        market_risk=market_risk,
        sector_risk=sector_risk,
        symbol_risk=symbol_risk,
        candidate_snapshot=candidate,
        decision=evaluated,
        grade=grade,
        blockers=tuple(blockers),
    )


__all__ = (
    "IndividualCandidateDecisionFacts",
    "build_individual_candidate_decision",
)
