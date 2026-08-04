"""Causal, replay-efficient horizontal sector strength preparation.

The live/page path can calculate one current snapshot directly from full daily
histories.  A one-year replay needs the same answer at hundreds of completed
daily cutoffs.  Re-scanning every prefix for every intraday decision would be
needlessly quadratic, so this module precomputes strict ``close > SMA`` prefix
counts once per member and feeds the resulting categories back into the shared
``sector_strength`` evidence/ranking core.

No missing daily bar is guessed to be a suspension.  Without an explicit
historical status fact the affected member is ``UNEXPLAINED_GAP`` and its whole
sector is unresolved at that cutoff.
"""

from __future__ import annotations

from bisect import bisect_left, bisect_right
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Mapping, Sequence

from chanlun.decision_support.fingerprints import normalize_datetime
from chanlun.decision_support.trading_system.backtest.pit_metadata import (
    SecurityMasterRecord,
)
from chanlun.decision_support.trading_system.sector_strength import (
    SectorMemberCategoryFact,
    SectorStrengthBatch,
    build_horizontal_sector_strength_batch_from_categories,
)
from chanlun.decision_support.trading_system.v3_etf_proxy_facts import (
    DailyMarketBar,
    latest_completed_bottom_fractal_anchor,
)
from chanlun.decision_support.trading_system.v3_selection import (
    CompletedDailyClose,
)


_MA_PERIODS = (5, 13, 21, 34, 55, 89, 144, 233)


@dataclass(frozen=True, slots=True)
class ReplaySectorMemberDailySeries:
    symbol: str
    security: SecurityMasterRecord
    closes: tuple[CompletedDailyClose, ...]
    source_revision: str

    def __post_init__(self) -> None:
        if self.symbol != self.security.code or not self.source_revision.startswith(
            "sha256:"
        ):
            raise ValueError("replay sector member provenance is invalid")
        sessions = tuple(value.session for value in self.closes)
        if sessions != tuple(sorted(set(sessions))):
            raise ValueError("replay member daily closes must be chronological")


@dataclass(frozen=True, slots=True)
class _PreparedMemberSeries:
    source: ReplaySectorMemberDailySeries
    sessions: tuple[date, ...]
    closes: tuple[Decimal, ...]
    attacked_prefixes: tuple[tuple[int, ...], ...]

    def category(self, *, anchor_session: date, required_session: date) -> int:
        end = bisect_right(self.sessions, required_session)
        if end < 5:
            return 1
        start = bisect_left(self.sessions, anchor_session, 0, end)
        for ordinal, prefix in enumerate(self.attacked_prefixes, start=1):
            if prefix[end] == prefix[start]:
                return ordinal
        return 9


def _prepare_member(
    source: ReplaySectorMemberDailySeries,
) -> _PreparedMemberSeries:
    sessions = tuple(value.session for value in source.closes)
    closes = tuple(value.close for value in source.closes)
    prefixes: list[tuple[int, ...]] = []
    for period in _MA_PERIODS:
        rolling = Decimal("0")
        prefix = [0]
        for index, close in enumerate(closes):
            rolling += close
            if index >= period:
                rolling -= closes[index - period]
            attacked = (
                index + 1 >= period
                and close > rolling / Decimal(period)
            )
            prefix.append(prefix[-1] + int(attacked))
        prefixes.append(tuple(prefix))
    return _PreparedMemberSeries(source, sessions, closes, tuple(prefixes))


def _member_fact_at(
    prepared: _PreparedMemberSeries,
    *,
    observed_at: datetime,
    required_session: date,
    anchor_session: date | None,
    market_sessions: tuple[date, ...],
) -> SectorMemberCategoryFact:
    source = prepared.source
    security = source.security
    end = bisect_right(prepared.sessions, required_session)
    visible_sessions = prepared.sessions[:end]
    if required_session < security.listed_from:
        return SectorMemberCategoryFact(source.symbol, "NEW_LISTING", 1)
    if security.listed_through is not None and required_session > security.listed_through:
        return SectorMemberCategoryFact(source.symbol, "UNEXPLAINED_GAP", None)
    reaches_cutoff = bool(visible_sessions) and visible_sessions[-1] == required_session
    if not reaches_cutoff:
        return SectorMemberCategoryFact(source.symbol, "UNEXPLAINED_GAP", None)
    if end < 5:
        expected = tuple(
            value
            for value in market_sessions
            if security.listed_from <= value <= required_session
        )
        if visible_sessions != expected:
            return SectorMemberCategoryFact(source.symbol, "UNEXPLAINED_GAP", None)
        return SectorMemberCategoryFact(source.symbol, "NEW_LISTING", 1)
    if anchor_session is None:
        # The shared composer will publish the benchmark-anchor blocker.  A
        # category is still required by the immutable category fact contract;
        # one is a neutral placeholder that is never used in this branch.
        return SectorMemberCategoryFact(source.symbol, "COMPLETE", 1)
    if source.closes[end - 1].known_at > observed_at:
        raise ValueError("member daily close is not visible at the replay cutoff")
    return SectorMemberCategoryFact(
        source.symbol,
        "COMPLETE",
        prepared.category(
            anchor_session=anchor_session,
            required_session=required_session,
        ),
    )


def build_replay_sector_strength_batches(
    *,
    evaluation_times: Sequence[datetime],
    benchmark_symbol: str,
    benchmark_daily: Sequence[DailyMarketBar],
    members_by_sector: Mapping[str, Sequence[str]],
    member_series: Mapping[str, ReplaySectorMemberDailySeries],
    market_sessions: Sequence[date],
    input_revision: str,
) -> tuple[SectorStrengthBatch, ...]:
    """Build one shared-core strength batch per completed daily cutoff."""

    times = tuple(
        normalize_datetime(value, "evaluation_time")
        for value in evaluation_times
    )
    if times != tuple(sorted(set(times))):
        raise ValueError("sector strength evaluation times must be chronological")
    calendar = tuple(market_sessions)
    if calendar != tuple(sorted(set(calendar))) or not calendar:
        raise ValueError("sector strength market calendar is invalid")
    if not input_revision.startswith("sha256:"):
        raise ValueError("sector strength replay input revision is required")
    normalized_members = {
        sector_id: tuple(sorted(set(values)))
        for sector_id, values in members_by_sector.items()
    }
    if any(
        not sector_id
        or len(values) != len(tuple(members_by_sector[sector_id]))
        for sector_id, values in normalized_members.items()
    ):
        raise ValueError("sector strength replay members must be unique")
    required_symbols = {
        symbol for values in normalized_members.values() for symbol in values
    }
    if required_symbols != set(member_series):
        raise ValueError("sector strength replay member series scope changed")
    prepared = {
        symbol: _prepare_member(member_series[symbol])
        for symbol in sorted(member_series)
    }
    benchmark = tuple(benchmark_daily)
    batches: list[SectorStrengthBatch] = []
    for observed_at in times:
        visible_benchmark = tuple(
            value
            for value in benchmark
            if value.completed
            and value.known_at <= observed_at
            and value.session <= observed_at.date()
        )
        required_session = (
            None if not visible_benchmark else visible_benchmark[-1].session
        )
        anchor = latest_completed_bottom_fractal_anchor(
            benchmark,
            decision_time=observed_at,
            symbol=benchmark_symbol,
        )
        anchor_session = anchor.anchor_session if anchor.resolved else None
        categories = {
            sector_id: tuple(
                (
                    SectorMemberCategoryFact(
                        symbol,
                        "UNEXPLAINED_GAP",
                        None,
                    )
                    if required_session is None
                    else _member_fact_at(
                        prepared[symbol],
                        observed_at=observed_at,
                        required_session=required_session,
                        anchor_session=anchor_session,
                        market_sessions=calendar,
                    )
                )
                for symbol in members
            )
            for sector_id, members in normalized_members.items()
        }
        batches.append(
            build_horizontal_sector_strength_batch_from_categories(
                decision_time=observed_at,
                benchmark_symbol=benchmark_symbol,
                benchmark_daily=benchmark,
                members_by_sector=categories,
                membership_revision=input_revision,
            )
        )
    return tuple(batches)


__all__ = (
    "ReplaySectorMemberDailySeries",
    "build_replay_sector_strength_batches",
)
