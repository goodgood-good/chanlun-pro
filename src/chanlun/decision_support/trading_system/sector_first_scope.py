"""严格策略个股路径使用的时点板块优先 A 股范围。

QMT 实时 GICS 目录只适合前瞻筛选，没有带生效日期的历史。历史评估因此使用
``PITMetadataSnapshot`` 中的 QMT 证券主数据和巨潮 SW1 成分变更。本模块只负责
选股范围，不生成技术信号、研究结论或订单。
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import date, datetime, time
from typing import Mapping

from chanlun.decision_support.fingerprints import sha256_json
from chanlun.decision_support.trading_system.backtest.pit_metadata import (
    CN,
    PITMetadataIndex,
    PITMetadataSnapshot,
)


SECTOR_FIRST_SCOPE_SCHEMA = "chanlun-sector-first-scope"


def _hashable(value: object) -> object:
    """Project plain ``date`` values into the canonical hash vocabulary."""

    if isinstance(value, date) and not isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Mapping):
        return {str(key): _hashable(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return tuple(_hashable(item) for item in value)
    return value


@dataclass(frozen=True, slots=True)
class SectorFirstSymbolScope:
    code: str
    name: str
    listed_from: date
    listed_through: date | None
    intersects_requested_range: bool
    classified_for_requested_range: bool
    sector_history: tuple[tuple[str, datetime, str], ...]
    reason_codes: tuple[str, ...]

    def __post_init__(self) -> None:
        if not self.code or not self.name:
            raise ValueError("sector-first symbol identity is required")
        if self.sector_history != tuple(
            sorted(set(self.sector_history), key=lambda value: (value[1], value[0]))
        ):
            raise ValueError("sector history must be unique and chronological")
        if self.reason_codes != tuple(sorted(set(self.reason_codes))):
            raise ValueError("scope reasons must be unique and sorted")
        if self.classified_for_requested_range == bool(self.reason_codes):
            raise ValueError("classified symbol cannot carry scope rejection reasons")


@dataclass(frozen=True, slots=True)
class SectorFirstScope:
    requested_start: date
    requested_end: date
    source_start: date
    source_end: date
    snapshot_captured_at: datetime
    source_hashes: tuple[tuple[str, str], ...]
    sector_names: tuple[tuple[str, str], ...]
    symbols: tuple[SectorFirstSymbolScope, ...]
    selected_symbols: tuple[str, ...]
    rejected_symbols: tuple[str, ...]
    start_members_by_sector: tuple[tuple[str, tuple[str, ...]], ...]
    end_members_by_sector: tuple[tuple[str, tuple[str, ...]], ...]
    membership_change_count: int
    selection_path: str = "INDIVIDUAL_THREE_PROGRAM"
    taxonomy: str = "SW1"
    source: str = "QMT_SECURITY_MASTER_PLUS_CNINFO_EFFECTIVE_DATED_SW1"
    etf_proxy_role: str = "SEPARATE_COMPONENT_CONTROL_ONLY"
    highest_status: str = "RESEARCH_ONLY"
    live_status: str = "LIVE_DISABLED"

    def __post_init__(self) -> None:
        if not self.source_start <= self.requested_start <= self.requested_end <= self.source_end:
            raise ValueError("requested scope is outside the PIT snapshot")
        if self.selection_path != "INDIVIDUAL_THREE_PROGRAM":
            raise ValueError("main sector-first scope must use the individual path")
        if self.taxonomy != "SW1" or self.live_status != "LIVE_DISABLED":
            raise ValueError("sector-first scope contract changed")
        codes = tuple(value.code for value in self.symbols)
        if codes != tuple(sorted(set(codes))):
            raise ValueError("scope symbols must be unique and sorted")
        selected = tuple(value.code for value in self.symbols if value.classified_for_requested_range)
        rejected = tuple(value.code for value in self.symbols if not value.classified_for_requested_range)
        if self.selected_symbols != selected or self.rejected_symbols != rejected:
            raise ValueError("scope decision lists do not match symbol facts")

    @property
    def content_sha256(self) -> str:
        return sha256_json(_hashable(
            {
                "schema": SECTOR_FIRST_SCOPE_SCHEMA,
                **asdict(self),
            }
        ))

    def document(self) -> dict[str, object]:
        stable: dict[str, object] = {
            "schema": SECTOR_FIRST_SCOPE_SCHEMA,
            **asdict(self),
            "counts": {
                "sector_count": len(self.sector_names),
                "intersecting_security_count": len(self.symbols),
                "selected_symbol_count": len(self.selected_symbols),
                "rejected_symbol_count": len(self.rejected_symbols),
                "membership_change_count": self.membership_change_count,
                "start_classified_symbol_count": sum(
                    len(members) for _sector, members in self.start_members_by_sector
                ),
                "end_classified_symbol_count": sum(
                    len(members) for _sector, members in self.end_members_by_sector
                ),
            },
            "pipeline": (
                "POINT_IN_TIME_SECTOR_TRIGGER",
                "POINT_IN_TIME_SECTOR_MEMBERS",
                "INDIVIDUAL_THREE_PROGRAM",
                "MARKET_SECTOR_SYMBOL_HIGHER_TIMEFRAME_RISK",
                "PHYSICAL_5M_SETUP_1M_TRIGGER_UNIFIED_POINT_CLASSES",
                "SHARED_PORTFOLIO_AND_EXECUTION_CORE",
            ),
        }
        return {**stable, "content_sha256": sha256_json(_hashable(stable))}


def _members_at(
    snapshot: PITMetadataSnapshot,
    index: PITMetadataIndex,
    observed_at: datetime,
) -> tuple[tuple[str, tuple[str, ...]], ...]:
    members: dict[str, list[str]] = {
        sector_id: [] for sector_id, _name in snapshot.qmt_sw1_sector_names
    }
    for security in snapshot.securities:
        if not security.listed_on(observed_at.date()):
            continue
        membership = index.membership_at(security.code, observed_at)
        if membership is not None:
            members.setdefault(membership.sector_id, []).append(security.code)
    return tuple(
        (sector_id, tuple(sorted(values)))
        for sector_id, values in sorted(members.items())
    )


def build_sector_first_scope(
    snapshot: PITMetadataSnapshot,
    *,
    requested_start: date,
    requested_end: date,
) -> SectorFirstScope:
    """Freeze every period-intersecting stock before any sector/stock signal.

    A security is selected into the data universe when it intersects the
    requested range and has at least one effective-dated SW1 membership fact.
    The actual sector used at a decision is still resolved by ``known_at``;
    the first or terminal sector is never backfilled across the full period.
    """

    if not snapshot.source_start <= requested_start <= requested_end <= snapshot.source_end:
        raise ValueError("requested range is outside the PIT metadata snapshot")
    index = PITMetadataIndex(snapshot)
    rows: list[SectorFirstSymbolScope] = []
    for security in sorted(snapshot.securities, key=lambda value: value.code):
        intersects = security.intersects(requested_start, requested_end)
        if not intersects:
            continue
        memberships = index.memberships_for(security.code)
        history = tuple(
            (value.sector_id, value.known_at, value.sector_name)
            for value in memberships
        )
        classified = bool(history)
        rows.append(
            SectorFirstSymbolScope(
                code=security.code,
                name=security.name,
                listed_from=security.listed_from,
                listed_through=security.listed_through,
                intersects_requested_range=True,
                classified_for_requested_range=classified,
                sector_history=history,
                reason_codes=(
                    () if classified else ("POINT_IN_TIME_SW1_MEMBERSHIP_MISSING",)
                ),
            )
        )
    symbols = tuple(rows)
    start_at = datetime.combine(requested_start, time(15, 0), tzinfo=CN)
    end_at = datetime.combine(requested_end, time(15, 0), tzinfo=CN)
    return SectorFirstScope(
        requested_start=requested_start,
        requested_end=requested_end,
        source_start=snapshot.source_start,
        source_end=snapshot.source_end,
        snapshot_captured_at=snapshot.captured_at,
        source_hashes=snapshot.source_hashes,
        sector_names=snapshot.qmt_sw1_sector_names,
        symbols=symbols,
        selected_symbols=tuple(
            value.code for value in symbols if value.classified_for_requested_range
        ),
        rejected_symbols=tuple(
            value.code for value in symbols if not value.classified_for_requested_range
        ),
        start_members_by_sector=_members_at(snapshot, index, start_at),
        end_members_by_sector=_members_at(snapshot, index, end_at),
        membership_change_count=len(snapshot.memberships),
    )


def current_gics_diagnostic_summary(
    ledger: Mapping[str, object],
) -> dict[str, object]:
    """Summarize, but never promote, the latest current-only GICS capture."""

    entries = ledger.get("entries")
    if not isinstance(entries, (list, tuple)) or not entries:
        return {
            "available": False,
            "role": "CURRENT_ONLY_DIAGNOSTIC_NOT_HISTORICAL_UNIVERSE",
        }
    latest = entries[-1]
    if not isinstance(latest, Mapping):
        raise ValueError("current GICS ledger tail is invalid")
    sectors = latest.get("sectors")
    if not isinstance(sectors, (list, tuple)):
        raise ValueError("current GICS sectors are unavailable")
    members = tuple(
        str(code)
        for sector in sectors
        if isinstance(sector, Mapping)
        for code in tuple(sector.get("member_codes") or ())
    )
    return {
        "available": True,
        "role": "CURRENT_ONLY_DIAGNOSTIC_NOT_HISTORICAL_UNIVERSE",
        "captured_at": latest.get("captured_at"),
        "sector_count": len(sectors),
        "membership_edge_count": len(members),
        "unique_member_count": len(set(members)),
        "historical_backfill_allowed": False,
    }


__all__ = (
    "SECTOR_FIRST_SCOPE_SCHEMA",
    "SectorFirstScope",
    "SectorFirstSymbolScope",
    "build_sector_first_scope",
    "current_gics_diagnostic_summary",
)
