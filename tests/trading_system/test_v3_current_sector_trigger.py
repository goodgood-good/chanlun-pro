from datetime import date, datetime, timedelta
from decimal import Decimal
import json
from zoneinfo import ZoneInfo

from chanlun.decision_support.trading_system.backtest.fixed_year import (
    SECTOR_FACT_SCHEMA,
    SectorResearchFacts,
)
from chanlun.decision_support.trading_system.backtest.pit_metadata import (
    SecurityMasterRecord,
)
from chanlun.decision_support.trading_system.models import SectorAssessment
from chanlun.decision_support.trading_system.sector_strength import (
    SECTOR_STRENGTH_EVIDENCE_SCHEMA,
    SectorStrengthBatch,
    SectorStrengthEvidence,
)
from chanlun.decision_support.trading_system.v3_recent_year_research import (
    RECENT_YEAR_SELECTION_PATH,
)
from chanlun.decision_support.trading_system.v3_sector_first_trigger_plan import (
    FORWARD_SAME_SESSION_MEMBERSHIP_MODE,
    build_current_sector_trigger_ledger,
    sector_trigger_windows_for_current_member,
)


CN = ZoneInfo("Asia/Shanghai")
HASH = "sha256:" + "1" * 64


def _facts(sector_id: str, observed_at: datetime) -> SectorResearchFacts:
    assessment = SectorAssessment(
        sector_id=sector_id,
        sector_name=sector_id,
        eligible=True,
        hard_block=False,
        regime="strong",
        rank_components=(("strength", 10),),
        reason_codes=("PASS_TEST_SECTOR",),
    )
    return SectorResearchFacts(
        schema=SECTOR_FACT_SCHEMA,
        algorithm_revision=HASH,
        source_revision="sha256:" + "2" * 64,
        sector_id=sector_id,
        sector_name=sector_id,
        member_count=2,
        row_count=10,
        thirty_points=(),
        assessments=((observed_at, assessment),),
    )


def test_current_sector_trigger_backfills_members_but_keeps_listing_gate() -> None:
    first = datetime(2025, 8, 1, 10, 0, tzinfo=CN)
    second = first + timedelta(minutes=30)
    sector_id = "qmt-gics3:test"
    early = SecurityMasterRecord("SH.600000", "early", date(2000, 1, 1), None)
    future = SecurityMasterRecord("SH.600001", "future", date(2025, 8, 2), None)
    facts = _facts(sector_id, first)
    second_facts = _facts(sector_id, second)
    combined = SectorResearchFacts(
        schema=facts.schema,
        algorithm_revision=facts.algorithm_revision,
        source_revision=facts.source_revision,
        sector_id=facts.sector_id,
        sector_name=facts.sector_name,
        member_count=facts.member_count,
        row_count=facts.row_count,
        thirty_points=(),
        assessments=facts.assessments + second_facts.assessments,
    )

    ledger = build_current_sector_trigger_ledger(
        sector_facts={sector_id: combined},
        sector_members={sector_id: (early.code, future.code)},
        securities=(early, future),
        observed_times=(first, second),
        algorithm_revision=HASH,
        catalog_entry_sha256="sha256:" + "3" * 64,
        security_snapshot_sha256="sha256:" + "4" * 64,
    )

    assert ledger.selection_path == RECENT_YEAR_SELECTION_PATH
    assert ledger.events[0].candidate_symbol_count == 1
    assert ledger.events[0].candidate_count_by_sector == ((sector_id, 1),)
    windows = sector_trigger_windows_for_current_member(
        ledger=ledger,
        security=early,
        sector_id=sector_id,
    )
    assert windows == ((first, second),)
    assert ledger.live_status == "LIVE_DISABLED"


def test_forward_current_sector_trigger_uses_same_session_pit_labels() -> None:
    observed = datetime(2026, 7, 28, 10, 0, tzinfo=CN)
    sector_id = "qmt-gics3:test"
    security = SecurityMasterRecord("SH.600000", "test", date(2000, 1, 1), None)

    ledger = build_current_sector_trigger_ledger(
        sector_facts={sector_id: _facts(sector_id, observed)},
        sector_members={sector_id: (security.code,)},
        securities=(security,),
        observed_times=(observed,),
        algorithm_revision=HASH,
        catalog_entry_sha256="sha256:" + "3" * 64,
        security_snapshot_sha256="sha256:" + "4" * 64,
        membership_mode=FORWARD_SAME_SESSION_MEMBERSHIP_MODE,
    )

    assert ledger.selection_order[:2] == (
        "QMT_PIT_SECTOR_TRIGGER",
        "QMT_PIT_MEMBERS_CAPTURED_SAME_SESSION",
    )
    assert ledger.taxonomy == "QMT_GICS3_FORWARD_PIT"
    assert ledger.events[0].candidate_symbol_count == 1


def test_current_trigger_uses_real_strength_and_excludes_unresolved_sectors() -> None:
    observed = datetime(2026, 7, 23, 15, 0, tzinfo=CN)
    decision = observed + timedelta(hours=19)
    sector_ids = ("qmt-gics3:gap", "qmt-gics3:strong", "qmt-gics3:weak")
    securities = tuple(
        SecurityMasterRecord(
            f"SH.60000{index}",
            sector_id,
            date(2000, 1, 1),
            None,
        )
        for index, sector_id in enumerate(sector_ids)
    )
    strengths = (
        SectorStrengthEvidence(
            sector_id="qmt-gics3:gap",
            observed_at=observed,
            anchor_session=date(2026, 7, 20),
            member_count=1,
            strength=None,
            rank=None,
            source_revision="sha256:" + "5" * 64,
            reason_codes=("UNEXPLAINED_MEMBER_HISTORY:SH.600000",),
        ),
        SectorStrengthEvidence(
            sector_id="qmt-gics3:strong",
            observed_at=observed,
            anchor_session=date(2026, 7, 20),
            member_count=1,
            strength=Decimal("8"),
            rank=1,
            source_revision="sha256:" + "6" * 64,
        ),
        SectorStrengthEvidence(
            sector_id="qmt-gics3:weak",
            observed_at=observed,
            anchor_session=date(2026, 7, 20),
            member_count=1,
            strength=Decimal("2"),
            rank=2,
            source_revision="sha256:" + "7" * 64,
        ),
    )
    batch = SectorStrengthBatch(
        strengths=strengths,
        evidence_json=json.dumps(
            {"schema": SECTOR_STRENGTH_EVIDENCE_SCHEMA},
            sort_keys=True,
            separators=(",", ":"),
        ),
    )

    ledger = build_current_sector_trigger_ledger(
        sector_facts={sector_id: _facts(sector_id, decision) for sector_id in sector_ids},
        sector_members={
            sector_id: (security.code,)
            for sector_id, security in zip(sector_ids, securities, strict=True)
        },
        securities=securities,
        observed_times=(decision,),
        algorithm_revision=HASH,
        catalog_entry_sha256="sha256:" + "3" * 64,
        security_snapshot_sha256="sha256:" + "4" * 64,
        sector_strength_batches=(batch,),
    )

    event = ledger.events[0]
    assert tuple(row.sector_id for row in event.ranked_sectors) == (
        "qmt-gics3:strong",
        "qmt-gics3:weak",
    )
    assert tuple(row.horizontal_strength for row in event.ranked_sectors) == (
        Decimal("8"),
        Decimal("2"),
    )
    assert event.unresolved_strength_sector_ids == ("qmt-gics3:gap",)
    assert event.candidate_symbol_count == 2
    assert event.sector_strength_evidence_revision == batch.evidence_revision
    document = ledger.document()
    assert document["content_sha256"].startswith("sha256:")
    assert document["events"][0]["ranked_sectors"][0][
        "strength_anchor_session"
    ] == "2026-07-20"
