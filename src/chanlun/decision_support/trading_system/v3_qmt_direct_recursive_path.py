"""Primary QMT structure path: one 1m graph recursively mapped to 30m/5m/1m.

The physical 30m/5m/1m chart adapter remains available as an independent
cross-check.  Trading authority comes from this module: it consumes only the
normalized QMT one-minute base stream and preserves every raw recursive level
and source identity before the decision adapter assigns logical strategy
labels.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Sequence

from chanlun.decision_support.trading_system.backtest.fixed_year import (
    strict_state,
)
from chanlun.decision_support.trading_system.v3_direct_recursive_structure import (
    DirectRecursiveStructurePath,
    build_v3_direct_recursive_structure_path,
)
from chanlun.decision_support.trading_system.v3_etf_proxy_facts import FactBlocker
from chanlun.decision_support.trading_system.v3_qmt_same_base_stream import (
    QmtSameBaseStreamFrames,
)
from chanlun.decision_support.trading_system.v3_selection import (
    TechnicalEntrySnapshot,
)


QmtDirectRecursiveGrade = Literal["RESEARCH_ONLY", "UNRESOLVED"]


@dataclass(frozen=True, slots=True)
class QmtV3DirectRecursivePath:
    symbol: str
    source_base_stream_revision: str
    structure_revision: str | None
    direct_path: DirectRecursiveStructurePath | None
    current_technical_entries: tuple[TechnicalEntrySnapshot, ...]
    grade: QmtDirectRecursiveGrade
    blockers: tuple[FactBlocker, ...]
    live_status: str = "LIVE_DISABLED"

    @property
    def aligned_entry_count(self) -> int:
        """Entries born on the latest completed one-minute bar only."""

        return len(self.current_technical_entries)

    @property
    def historical_aligned_entry_count(self) -> int:
        return 0 if self.direct_path is None else self.direct_path.aligned_entry_count


def _validate_one_minute_lineage(source: QmtSameBaseStreamFrames) -> None:
    frame = source.one_minute
    if frame.empty:
        return
    if (
        frame.attrs.get("source_base_stream_revision")
        != source.source_base_stream_revision
    ):
        raise ValueError("QMT direct recursion crossed base streams")
    if frame.attrs.get("derived_frequency") != "1m":
        raise ValueError("QMT direct recursion requires the normalized 1m frame")
    if frame.attrs.get("price_basis_revision") != source.price_basis_revision:
        raise ValueError("QMT direct recursion crossed price bases")


def build_qmt_v3_direct_recursive_path(
    *,
    source: QmtSameBaseStreamFrames,
    allowed_l2_second_buy_ids: Sequence[str] = (),
) -> QmtV3DirectRecursivePath:
    """Build the primary direct-recursive structure path from QMT data."""

    _validate_one_minute_lineage(source)
    blockers = list(source.blockers)
    if source.one_minute.empty or source.price_basis_revision is None:
        blockers.append(
            FactBlocker(
                "direct_recursive_structure",
                "QMT_DIRECT_RECURSIVE_ONE_MINUTE_UNAVAILABLE",
                source.source_base_stream_revision,
            )
        )
        return QmtV3DirectRecursivePath(
            symbol=source.symbol,
            source_base_stream_revision=source.source_base_stream_revision,
            structure_revision=None,
            direct_path=None,
            current_technical_entries=(),
            grade="UNRESOLVED",
            blockers=tuple(blockers),
        )

    state = strict_state(source.symbol, "1m", source.one_minute)
    state.process_klines(source.one_minute)
    evidence = state.get_strict_evidence()
    if evidence.price_basis_revision != source.price_basis_revision:
        raise ValueError("QMT direct recursion evidence crossed price bases")
    direct = build_v3_direct_recursive_structure_path(
        evidence=evidence,
        code=source.symbol,
        allowed_l2_second_buy_ids=tuple(allowed_l2_second_buy_ids),
    )
    latest_completed = source.one_minute["date"].iloc[-1].to_pydatetime()
    current = tuple(
        entry
        for entry in direct.technical_entries
        if entry.observed_at == latest_completed
    )
    if direct.grade == "UNRESOLVED":
        blockers.append(
            FactBlocker(
                "direct_recursive_structure",
                "QMT_DIRECT_RECURSIVE_STRUCTURE_UNRESOLVED",
                direct.structure_snapshot_id,
            )
        )
    return QmtV3DirectRecursivePath(
        symbol=source.symbol,
        source_base_stream_revision=source.source_base_stream_revision,
        structure_revision=evidence.structure_revision,
        direct_path=direct,
        current_technical_entries=current,
        grade=(
            "UNRESOLVED"
            if direct.grade == "UNRESOLVED" or source.grade == "UNRESOLVED"
            else "RESEARCH_ONLY"
        ),
        blockers=tuple(blockers),
    )


__all__ = (
    "QmtV3DirectRecursivePath",
    "build_qmt_v3_direct_recursive_path",
)
