from __future__ import annotations

from datetime import date, datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

from chanlun.decision_support.trading_system.backtest.pit_metadata import (
    SecurityMasterRecord,
)
from chanlun.decision_support.trading_system.sector_strength import (
    build_horizontal_sector_strength_batch,
)
from chanlun.decision_support.trading_system.etf_proxy_facts import DailyMarketBar
from chanlun.decision_support.trading_system.sector_strength_replay import (
    ReplaySectorMemberDailySeries,
    build_replay_sector_strength_batches,
)
from chanlun.decision_support.trading_system.selection import (
    CompletedDailyClose,
    SectorMemberHistory,
)


CN = ZoneInfo("Asia/Shanghai")
HASH = "sha256:" + "1" * 64


def _known(session: date) -> datetime:
    return datetime.combine(session, datetime.min.time(), tzinfo=CN) + timedelta(
        hours=15
    )


def _benchmark() -> tuple[DailyMarketBar, ...]:
    closes = (
        10, 11, 12, 11, 10, 9, 10, 11, 12, 13, 12, 11, 10, 9,
        8, 9, 10, 11, 10, 9, 8, 7, 8, 9, 10, 11, 10,
    )
    start = date(2020, 1, 1)
    return tuple(
        DailyMarketBar(
            session=start + timedelta(days=index),
            open=Decimal(value),
            high=Decimal(value + 1),
            low=Decimal(value - 1),
            close=Decimal(value),
            volume=Decimal("100"),
            known_at=_known(start + timedelta(days=index)),
        )
        for index, value in enumerate(closes)
    )


def _series(
    symbol: str,
    *,
    sessions: tuple[date, ...],
    strong: bool,
    omit_last: bool = False,
) -> ReplaySectorMemberDailySeries:
    values = sessions[:-1] if omit_last else sessions
    closes = tuple(
        CompletedDailyClose(
            session=session,
            close=(
                Decimal("1000")
                if strong and session >= date(2020, 1, 23)
                else Decimal("1")
                if not strong and session >= date(2020, 1, 23)
                else Decimal("100")
            ),
            known_at=_known(session),
        )
        for session in values
    )
    return ReplaySectorMemberDailySeries(
        symbol=symbol,
        security=SecurityMasterRecord(symbol, symbol, sessions[0], None),
        closes=closes,
        source_revision=HASH,
    )


def test_replay_categories_match_full_history_evidence_exactly() -> None:
    benchmark = _benchmark()
    sessions = tuple(value.session for value in benchmark)
    observed = benchmark[-1].known_at
    strong = _series("SH.600001", sessions=sessions, strong=True)
    weak = _series("SH.600002", sessions=sessions, strong=False)
    gap = _series(
        "SH.600003",
        sessions=sessions,
        strong=True,
        omit_last=True,
    )
    replay = build_replay_sector_strength_batches(
        evaluation_times=(observed,),
        benchmark_symbol="SH.000300",
        benchmark_daily=benchmark,
        members_by_sector={
            "gap": (gap.symbol,),
            "strong": (strong.symbol,),
            "weak": (weak.symbol,),
        },
        member_series={
            strong.symbol: strong,
            weak.symbol: weak,
            gap.symbol: gap,
        },
        market_sessions=sessions,
        input_revision=HASH,
    )[0]
    full = build_horizontal_sector_strength_batch(
        decision_time=observed,
        benchmark_symbol="SH.000300",
        benchmark_daily=benchmark,
        members_by_sector={
            "gap": (
                SectorMemberHistory(
                    gap.symbol,
                    gap.security.listed_from,
                    "UNEXPLAINED_GAP",
                    gap.closes,
                ),
            ),
            "strong": (
                SectorMemberHistory(
                    strong.symbol,
                    strong.security.listed_from,
                    "COMPLETE",
                    strong.closes,
                ),
            ),
            "weak": (
                SectorMemberHistory(
                    weak.symbol,
                    weak.security.listed_from,
                    "COMPLETE",
                    weak.closes,
                ),
            ),
        },
        membership_revision=HASH,
    )

    assert replay == full
    assert replay.evidence_json == full.evidence_json
    assert replay["strong"].strength > replay["weak"].strength
    assert replay["gap"].resolved is False


def test_future_daily_rows_do_not_change_an_earlier_strength_batch() -> None:
    benchmark = _benchmark()
    sessions = tuple(value.session for value in benchmark)
    observed = benchmark[-1].known_at
    base = _series("SH.600001", sessions=sessions, strong=True)
    future_session = sessions[-1] + timedelta(days=1)
    extended = ReplaySectorMemberDailySeries(
        symbol=base.symbol,
        security=base.security,
        closes=(
            *base.closes,
            CompletedDailyClose(
                future_session,
                Decimal("9999"),
                _known(future_session),
            ),
        ),
        source_revision=HASH,
    )

    def build(series: ReplaySectorMemberDailySeries):
        return build_replay_sector_strength_batches(
            evaluation_times=(observed,),
            benchmark_symbol="SH.000300",
            benchmark_daily=(
                *benchmark,
                DailyMarketBar(
                    future_session,
                    Decimal("99"),
                    Decimal("100"),
                    Decimal("98"),
                    Decimal("99"),
                    Decimal("100"),
                    _known(future_session),
                ),
            ),
            members_by_sector={"sector": (series.symbol,)},
            member_series={series.symbol: series},
            market_sessions=(*sessions, future_session),
            input_revision=HASH,
        )[0]

    assert build(base) == build(extended)


def test_fewer_than_five_complete_new_listing_sessions_remains_category_one() -> None:
    benchmark = _benchmark()
    sessions = tuple(value.session for value in benchmark)
    observed = benchmark[-1].known_at
    short_sessions = sessions[-4:]
    series = _series("SH.600001", sessions=short_sessions, strong=True)
    [batch] = build_replay_sector_strength_batches(
        evaluation_times=(observed,),
        benchmark_symbol="SH.000300",
        benchmark_daily=benchmark,
        members_by_sector={"new": (series.symbol,)},
        member_series={series.symbol: series},
        market_sessions=sessions,
        input_revision=HASH,
    )

    assert batch["new"].resolved is True
    assert batch["new"].strength == Decimal("1")
