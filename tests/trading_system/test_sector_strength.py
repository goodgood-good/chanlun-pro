from __future__ import annotations

import copy
from dataclasses import replace
from datetime import date, datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest

from chanlun.decision_support.trading_system.sector_policy import rank_sectors
from chanlun.decision_support.trading_system.sector_strength import (
    SectorMemberCategoryFact,
    build_horizontal_sector_strength,
    build_horizontal_sector_strength_batch,
    build_horizontal_sector_strength_batch_from_categories,
    build_sector_member_history_diagnostics,
    sector_strength_batch_from_evidence_document,
)
from chanlun.decision_support.trading_system.etf_proxy_facts import DailyMarketBar
from chanlun.decision_support.trading_system.selection import (
    CompletedDailyClose,
    SectorMemberHistory,
    member_ma_strength_category,
)
from tests.trading_system.helpers import eligible_sector


CN = ZoneInfo("Asia/Shanghai")


def _known(session: date) -> datetime:
    return datetime(session.year, session.month, session.day, 15, tzinfo=CN)


def _market_bar(session: date, close: int) -> DailyMarketBar:
    value = Decimal(close)
    return DailyMarketBar(
        session=session,
        open=value,
        high=value + 1,
        low=value - 1,
        close=value,
        volume=Decimal("100"),
        known_at=_known(session),
    )


def _member(symbol: str, *, strong: bool, decision: date) -> SectorMemberHistory:
    start = decision - timedelta(days=259)
    rows = []
    for index in range(260):
        session = start + timedelta(days=index)
        # A decisive move after the common anchor conquers every visible MA;
        # the weak series never conquers even MA5.
        close = Decimal("1000") if strong and session >= date(2020, 1, 23) else Decimal("100")
        if not strong and session >= date(2020, 1, 23):
            close = Decimal("1")
        rows.append(CompletedDailyClose(session, close, _known(session)))
    return SectorMemberHistory(symbol, start, "COMPLETE", tuple(rows))


def test_common_anchor_all_member_strength_produces_distinct_ranks() -> None:
    values = (10, 11, 12, 11, 10, 9, 10, 11, 12, 13, 12, 11, 10, 9, 8, 9, 10, 11, 10, 9, 8, 7, 8, 9, 10, 11, 10)
    start = date(2020, 1, 1)
    benchmark = tuple(
        _market_bar(start + timedelta(days=index), value)
        for index, value in enumerate(values)
    )
    decision = benchmark[-1].known_at
    result = build_horizontal_sector_strength(
        decision_time=decision,
        benchmark_symbol="SH.000300",
        benchmark_daily=benchmark,
        members_by_sector={
            "strong": (_member("SH.600001", strong=True, decision=decision.date()),),
            "weak": (_member("SH.600002", strong=False, decision=decision.date()),),
        },
        membership_revision="sha256:test-membership",
    )

    assert result["strong"].anchor_session == date(2020, 1, 23)
    assert result["strong"].strength > result["weak"].strength
    assert result["strong"].rank == 1
    assert result["weak"].rank == 2

    base = eligible_sector()
    ranked = rank_sectors(
        (
            replace(
                base,
                sector_id="weak",
                horizontal_strength=result["weak"].strength,
                horizontal_rank=2,
                strength_anchor_session=result["weak"].anchor_session,
                strength_member_count=1,
                strength_source_revision=result["weak"].source_revision,
            ),
            replace(
                base,
                sector_id="strong",
                horizontal_strength=result["strong"].strength,
                horizontal_rank=1,
                strength_anchor_session=result["strong"].anchor_session,
                strength_member_count=1,
                strength_source_revision=result["strong"].source_revision,
            ),
        )
    )
    assert tuple(value.assessment.sector_id for value in ranked) == (
        "strong",
        "weak",
    )


def test_precomputed_categories_are_byte_identical_to_full_history_core() -> None:
    values = (
        10, 11, 12, 11, 10, 9, 10, 11, 12, 13, 12, 11, 10, 9,
        8, 9, 10, 11, 10, 9, 8, 7, 8, 9, 10, 11, 10,
    )
    start = date(2020, 1, 1)
    benchmark = tuple(
        _market_bar(start + timedelta(days=index), value)
        for index, value in enumerate(values)
    )
    decision = benchmark[-1].known_at
    histories = {
        "strong": (_member("SH.600001", strong=True, decision=decision.date()),),
        "weak": (_member("SH.600002", strong=False, decision=decision.date()),),
    }
    full = build_horizontal_sector_strength_batch(
        decision_time=decision,
        benchmark_symbol="SH.000300",
        benchmark_daily=benchmark,
        members_by_sector=histories,
        membership_revision="sha256:" + "6" * 64,
    )
    assert full["strong"].anchor_session is not None
    categories = {
        sector_id: tuple(
            SectorMemberCategoryFact(
                symbol=member.symbol,
                history_status=member.history_status,
                category=member_ma_strength_category(
                    member,
                    anchor_session=full["strong"].anchor_session,
                    decision_time=decision,
                ),
            )
            for member in members
        )
        for sector_id, members in histories.items()
    }
    optimized = build_horizontal_sector_strength_batch_from_categories(
        decision_time=decision,
        benchmark_symbol="SH.000300",
        benchmark_daily=benchmark,
        members_by_sector=categories,
        membership_revision="sha256:" + "6" * 64,
    )

    assert optimized == full
    assert optimized.evidence_json == full.evidence_json
    assert optimized.evidence_revision == full.evidence_revision


def test_full_history_batch_reports_progress_between_sectors() -> None:
    values = (
        10, 11, 12, 11, 10, 9, 10, 11, 12, 13, 12, 11,
        10, 9, 8, 9, 10, 11, 10, 9, 8, 7, 8, 9, 10, 11, 10,
    )
    start = date(2020, 1, 1)
    benchmark = tuple(
        _market_bar(start + timedelta(days=index), value)
        for index, value in enumerate(values)
    )
    decision = benchmark[-1].known_at
    progress: list[int] = []

    build_horizontal_sector_strength_batch(
        decision_time=decision,
        benchmark_symbol="SH.000300",
        benchmark_daily=benchmark,
        members_by_sector={
            "strong": (
                _member("SH.600001", strong=True, decision=decision.date()),
            ),
            "weak": (
                _member("SH.600002", strong=False, decision=decision.date()),
            ),
        },
        membership_revision="sha256:" + "7" * 64,
        progress_callback=lambda: progress.append(len(progress)),
    )

    assert len(progress) == 4


def test_unexplained_member_gap_never_receives_a_synthetic_rank() -> None:
    benchmark = tuple(
        _market_bar(date(2020, 1, 1) + timedelta(days=index), value)
        for index, value in enumerate((10, 11, 12, 11, 10, 9, 10, 11, 12, 13, 12, 11, 10, 9, 8, 9, 10, 11, 10, 9, 8, 7, 8, 9, 10, 11, 10))
    )
    missing = SectorMemberHistory(
        "SH.600003",
        date(2020, 1, 1),
        "UNEXPLAINED_GAP",
        (),
    )
    [result] = build_horizontal_sector_strength(
        decision_time=benchmark[-1].known_at,
        benchmark_symbol="SH.000300",
        benchmark_daily=benchmark,
        members_by_sector={"missing": (missing,)},
        membership_revision="sha256:test-membership",
    ).values()

    assert result.resolved is False
    assert result.rank is None
    assert "UNEXPLAINED_MEMBER_HISTORY:SH.600003" in result.reason_codes


def test_one_unexplained_member_gap_blocks_the_whole_sector_without_deletion() -> None:
    benchmark = tuple(
        _market_bar(date(2020, 1, 1) + timedelta(days=index), value)
        for index, value in enumerate(
            (
                10, 11, 12, 11, 10, 9, 10, 11, 12, 13, 12, 11, 10, 9,
                8, 9, 10, 11, 10, 9, 8, 7, 8, 9, 10, 11, 10,
            )
        )
    )
    decision = benchmark[-1].known_at
    verified = tuple(
        _member(
            f"SH.6000{index:02d}",
            strong=index < 5,
            decision=decision.date(),
        )
        for index in range(9)
    )
    missing = SectorMemberHistory(
        "SH.609999",
        date(2020, 1, 1),
        "UNEXPLAINED_GAP",
        (),
    )

    batch = build_horizontal_sector_strength_batch(
        decision_time=decision,
        benchmark_symbol="SH.000300",
        benchmark_daily=benchmark,
        members_by_sector={"partial": (*verified, missing)},
        membership_revision="sha256:" + "b" * 64,
    )
    result = batch["partial"]
    evidence = batch.evidence_document()["sectors"][0]

    assert result.resolved is False
    assert result.rank is None
    assert result.member_count == 10
    assert "UNEXPLAINED_MEMBER_HISTORY:SH.609999" in result.reason_codes
    assert evidence["total_member_count"] == 10
    assert evidence["usable_member_count"] == 9
    assert evidence["missing_members"] == ["SH.609999"]
    assert len(evidence["categories"]) == 10
    assert all(category == 1 for _symbol, category in evidence["categories"])
    assert sector_strength_batch_from_evidence_document(
        batch.evidence_document()
    ) == batch


def test_strength_batch_evidence_recomputes_values_ranks_and_source_hashes() -> None:
    benchmark = tuple(
        _market_bar(date(2020, 1, 1) + timedelta(days=index), value)
        for index, value in enumerate(
            (
                10, 11, 12, 11, 10, 9, 10, 11, 12, 13, 12, 11, 10, 9,
                8, 9, 10, 11, 10, 9, 8, 7, 8, 9, 10, 11, 10,
            )
        )
    )
    decision = benchmark[-1].known_at
    batch = build_horizontal_sector_strength_batch(
        decision_time=decision,
        benchmark_symbol="SH.000300",
        benchmark_daily=benchmark,
        members_by_sector={
            "strong": (_member("SH.600001", strong=True, decision=decision.date()),),
            "weak": (_member("SH.600002", strong=False, decision=decision.date()),),
        },
        membership_revision="sha256:" + "7" * 64,
    )

    restored = sector_strength_batch_from_evidence_document(
        batch.evidence_document()
    )
    assert restored == batch
    assert restored.evidence_revision == batch.evidence_revision
    assert restored["strong"].rank == 1
    assert restored["weak"].rank == 2

    forged = copy.deepcopy(batch.evidence_document())
    forged["sectors"][0]["strength"] = "99"
    with pytest.raises(ValueError, match="derived evidence"):
        sector_strength_batch_from_evidence_document(forged)


def test_member_history_diagnostics_are_recomputed_from_authenticated_evidence() -> None:
    benchmark = tuple(
        _market_bar(date(2020, 1, 1) + timedelta(days=index), value)
        for index, value in enumerate(
            (
                10, 11, 12, 11, 10, 9, 10, 11, 12, 13, 12, 11, 10, 9,
                8, 9, 10, 11, 10, 9, 8, 7, 8, 9, 10, 11, 10,
            )
        )
    )
    decision = benchmark[-1].known_at
    complete = _member("SH.600001", strong=True, decision=decision.date())
    suspended = replace(
        _member("SH.600002", strong=True, decision=decision.date()),
        history_status="SUSPENDED",
    )
    new_listing = replace(
        _member("SH.600003", strong=False, decision=decision.date()),
        history_status="NEW_LISTING",
    )
    unexplained = SectorMemberHistory(
        "SH.600004",
        date(2020, 1, 1),
        "UNEXPLAINED_GAP",
        (),
    )
    batch = build_horizontal_sector_strength_batch(
        decision_time=decision,
        benchmark_symbol="SH.000300",
        benchmark_daily=benchmark,
        members_by_sector={
            "alpha": (complete, suspended, unexplained),
            "beta": (suspended, new_listing),
        },
        membership_revision="sha256:" + "d" * 64,
    )

    diagnostics = build_sector_member_history_diagnostics(batch)

    assert diagnostics == {
        "schema": "chanlun-sector-member-history-diagnostics",
        "evidence_revision": batch.evidence_revision,
        "sector_count": 2,
        "sector_member_relation_count": 5,
        "unique_symbol_count": 4,
        "relation_status_counts": {
            "COMPLETE": 1,
            "NEW_LISTING": 1,
            "SUSPENDED": 2,
            "UNEXPLAINED_GAP": 1,
        },
        "unique_symbol_status_counts": {
            "COMPLETE": 1,
            "NEW_LISTING": 1,
            "SUSPENDED": 1,
            "UNEXPLAINED_GAP": 1,
        },
        "affected_sector_counts": {
            "COMPLETE": 1,
            "NEW_LISTING": 1,
            "SUSPENDED": 2,
            "UNEXPLAINED_GAP": 1,
        },
        "new_listing_symbols": ["SH.600003"],
        "suspended_symbols": ["SH.600002"],
        "unexplained_gap_symbols": ["SH.600004"],
        "unexplained_gap_sector_ids": ["alpha"],
    }


def test_member_history_diagnostics_reject_cross_sector_status_conflict() -> None:
    benchmark = tuple(
        _market_bar(date(2020, 1, 1) + timedelta(days=index), value)
        for index, value in enumerate(
            (
                10, 11, 12, 11, 10, 9, 10, 11, 12, 13, 12, 11, 10, 9,
                8, 9, 10, 11, 10, 9, 8, 7, 8, 9, 10, 11, 10,
            )
        )
    )
    decision = benchmark[-1].known_at
    complete = _member("SH.600001", strong=True, decision=decision.date())
    suspended = replace(complete, history_status="SUSPENDED")
    batch = build_horizontal_sector_strength_batch(
        decision_time=decision,
        benchmark_symbol="SH.000300",
        benchmark_daily=benchmark,
        members_by_sector={"alpha": (complete,), "beta": (suspended,)},
        membership_revision="sha256:" + "e" * 64,
    )

    with pytest.raises(ValueError, match="conflicts across sectors"):
        build_sector_member_history_diagnostics(batch)
