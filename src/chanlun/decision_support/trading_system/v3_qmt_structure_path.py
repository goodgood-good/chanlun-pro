"""Causal 30m strategic and 5m/1m locating structure from one QMT base stream.

No signal rule is implemented here.  The module routes the three derived
frames through the existing append-only causal structure ledger, the frozen
30m/5m/1m alignment contract, and the shared V3 technical snapshot adapter.
The same output can therefore be consumed by both live observation and replay.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from decimal import Decimal
from typing import Literal, Sequence

from chanlun.decision_support.fingerprints import sha256_json
from chanlun.decision_support.trading_system.backtest.fixed_year import (
    CausalStructureEventLedger,
    final_confirmed_structure_events,
)
from chanlun.decision_support.trading_system.models import StructuralPoint
from chanlun.decision_support.trading_system.v3_etf_proxy_facts import FactBlocker
from chanlun.decision_support.trading_system.v3_qmt_same_base_stream import (
    QmtSameBaseStreamFrames,
)
from chanlun.decision_support.trading_system.v3_selection import (
    TechnicalEntrySnapshot,
)
from chanlun.decision_support.trading_system.v3_structure_adapter import (
    build_v3_independent_technical_entry_snapshot,
)
from chanlun.decision_support.trading_system.v3_timeframe_alignment import (
    AlignmentDecision,
    CompletedL1TrendFact,
    align_independent_entry_chains,
    completed_l1_trend_fact,
    independent_alignment_contract,
)
from chanlun.decision_support.trading_system.v3_timeframe_override import (
    independent_timeframe_override,
)


QmtStructureGrade = Literal["RESEARCH_ONLY", "UNRESOLVED"]


@dataclass(frozen=True, slots=True)
class QmtV3StructurePath:
    symbol: str
    source_base_stream_revision: str
    structure_snapshot_id: str
    thirty_minute_ledger: CausalStructureEventLedger
    five_minute_ledger: CausalStructureEventLedger
    one_minute_ledger: CausalStructureEventLedger
    l0_entry_points: tuple[StructuralPoint, ...]
    l1_completed_trends: tuple[CompletedL1TrendFact, ...]
    l2_locator_points: tuple[StructuralPoint, ...]
    alignment_decisions: tuple[AlignmentDecision, ...]
    technical_entries: tuple[TechnicalEntrySnapshot, ...]
    alignment_rejection_counts: tuple[tuple[str, int], ...]
    grade: QmtStructureGrade
    blockers: tuple[FactBlocker, ...]
    live_status: str = "LIVE_DISABLED"

    @property
    def aligned_entry_count(self) -> int:
        return len(self.technical_entries)

    @property
    def historical_aligned_chain_count(self) -> int:
        return sum(value.chain is not None for value in self.alignment_decisions)


def _empty_ledger() -> CausalStructureEventLedger:
    return CausalStructureEventLedger(points=(), completed_trends=())


def _validate_lineage(source: QmtSameBaseStreamFrames) -> None:
    expected = source.source_base_stream_revision
    price_basis = source.price_basis_revision
    for frequency, frame in (
        ("30m", source.thirty_minute),
        ("5m", source.five_minute),
        ("1m", source.one_minute),
    ):
        if frame.attrs.get("source_base_stream_revision") != expected:
            raise ValueError(f"{frequency} structure frame crossed base streams")
        if frame.attrs.get("derived_frequency") != frequency:
            raise ValueError(f"{frequency} structure frame identity is invalid")
        if frame.attrs.get("price_basis_revision") != price_basis:
            raise ValueError(f"{frequency} structure frame crossed price bases")


def build_qmt_v3_structure_path(
    *,
    source: QmtSameBaseStreamFrames,
    allowed_l2_second_buy_ids: Sequence[str] = (),
) -> QmtV3StructurePath:
    """Build the frozen 30m strategy / 5m+1m locator path.

    Independent physical charts are still explicitly labelled with the
    existing user-authorized alignment contract.  Sharing one 1m data stream
    proves data lineage; it does not falsely claim that their structure
    products are direct recursive parents.
    """

    _validate_lineage(source)
    blockers = list(source.blockers)
    frames = {
        "30m": source.thirty_minute,
        "5m": source.five_minute,
        "1m": source.one_minute,
    }
    for frequency, frame in frames.items():
        if frame.empty:
            blockers.append(
                FactBlocker(
                    f"{frequency}_structure",
                    "QMT_STRUCTURE_TIMEFRAME_EMPTY",
                    frequency,
                )
            )

    if any(frame.empty for frame in frames.values()) or source.price_basis_revision is None:
        thirty_ledger = _empty_ledger()
        five_ledger = _empty_ledger()
        one_ledger = _empty_ledger()
        l0_points: tuple[StructuralPoint, ...] = ()
        l1_trends: tuple[CompletedL1TrendFact, ...] = ()
        l2_points: tuple[StructuralPoint, ...] = ()
        decisions: tuple[AlignmentDecision, ...] = ()
    else:
        thirty_ledger = final_confirmed_structure_events(
            source.symbol,
            "30m",
            frames["30m"],
        )
        five_ledger = final_confirmed_structure_events(
            source.symbol,
            "5m",
            frames["5m"],
        )
        one_ledger = final_confirmed_structure_events(
            source.symbol,
            "1m",
            frames["1m"],
        )
        l0_points = tuple(
            point
            for point in thirty_ledger.points
            if point.recursive_level == 0
            and point.point_type == "3buy"
            and point.center_ordinal == 1
        )
        quantum = Decimal(str(frames["5m"].attrs["structure_price_quantum"]))
        l1_trends = tuple(
            completed_l1_trend_fact(trend, price_quantum=quantum)
            for trend in five_ledger.completed_trends
            if trend.structural_level == 0 and trend.complete
        )
        l2_points = tuple(
            point
            for point in one_ledger.points
            if point.recursive_level == 0 and point.point_type in {"1buy", "2buy"}
        )
        decisions = align_independent_entry_chains(
            l0_points=l0_points,
            l1_trends=l1_trends,
            l2_points=l2_points,
            allowed_l2_second_buy_ids=allowed_l2_second_buy_ids,
        )

    snapshot_id = sha256_json(
        {
            "schema": "chanlun-v3-qmt-physical-timeframe-structure/v1",
            "symbol": source.symbol,
            "observed_at": source.observed_at,
            "source_base_stream_revision": source.source_base_stream_revision,
            "alignment_contract": independent_alignment_contract().parameter_set_id,
            "decisions": tuple(value.document() for value in decisions),
        }
    )
    l0_by_id = {value.point_id: value for value in l0_points}
    l1_by_id = {value.trend_id: value for value in l1_trends}
    l2_by_id = {value.point_id: value for value in l2_points}
    latest_completed_minute = (
        None
        if source.one_minute.empty
        else source.one_minute["date"].iloc[-1].to_pydatetime()
    )
    # The terminal ledger may contain old, first-seen chains for audit.  A
    # live candidate event exists only at the exact prefix where the chain
    # first became available; otherwise an old 30m third buy could be reused
    # days or months later.  Replay obtains the same behavior by rebuilding
    # this adapter at each causal prefix.
    current_decisions = tuple(
        decision
        for decision in decisions
        if decision.chain is not None
        and decision.chain.decision_at == latest_completed_minute
    )
    technical_entries = tuple(
        build_v3_independent_technical_entry_snapshot(
            structure_snapshot_id=snapshot_id,
            observed_at=decision.chain.decision_at,
            chain=decision.chain,
            l0_three_buy=l0_by_id[decision.chain.l0_point_id],
            l1_departure=l1_by_id[decision.chain.l1_departure_trend_id],
            l1_first_return=l1_by_id[decision.chain.l1_return_trend_id],
            l2_locator=l2_by_id[decision.chain.l2_locator_point_id],
        )
        for decision in current_decisions
    )
    rejection_counts = Counter(
        reason for value in decisions for reason in value.reason_codes
    )
    override = independent_timeframe_override()
    if override.highest_status != "RESEARCH_ONLY" or override.live_status != "LIVE_DISABLED":
        raise RuntimeError("physical-timeframe safety contract changed unexpectedly")
    grade: QmtStructureGrade = (
        "UNRESOLVED"
        if any(value.code == "QMT_STRUCTURE_TIMEFRAME_EMPTY" for value in blockers)
        else "RESEARCH_ONLY"
    )
    return QmtV3StructurePath(
        symbol=source.symbol,
        source_base_stream_revision=source.source_base_stream_revision,
        structure_snapshot_id=snapshot_id,
        thirty_minute_ledger=thirty_ledger,
        five_minute_ledger=five_ledger,
        one_minute_ledger=one_ledger,
        l0_entry_points=l0_points,
        l1_completed_trends=l1_trends,
        l2_locator_points=l2_points,
        alignment_decisions=decisions,
        technical_entries=technical_entries,
        alignment_rejection_counts=tuple(sorted(rejection_counts.items())),
        grade=grade,
        blockers=tuple(blockers),
    )


__all__ = ("QmtV3StructurePath", "build_qmt_v3_structure_path")
