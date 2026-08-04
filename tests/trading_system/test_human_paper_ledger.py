from __future__ import annotations

from dataclasses import asdict, replace
from datetime import date, datetime, time, timedelta
from decimal import Decimal
import json
from pathlib import Path
from typing import Mapping
from zoneinfo import ZoneInfo

import pytest

from chanlun.decision_support.fingerprints import sha256_json
from chanlun.decision_support.trading_system.human_paper_accounting import (
    assess_human_paper_portfolio_fill,
    audit_human_paper_capital_decisions,
    audit_human_paper_portfolio_decisions,
    audit_human_paper_portfolio_fill_decisions,
    load_human_paper_accounting_parameters,
    rebuild_human_paper_accounting,
)
from chanlun.decision_support.trading_system.human_paper_ledger import (
    HumanPaperCapitalRejection,
    HumanPaperEntrySelectionEvidence,
    HumanPaperMinuteBar,
    HumanPaperOperationsCancellation,
    audit_human_paper_entry_boundary_attestations,
    audit_human_paper_entry_selection_attestations,
    audit_human_paper_entry_selection_source_bindings,
    audit_human_paper_pending_continuity,
    append_human_paper_intent,
    build_human_paper_intent,
    human_paper_capital_rejected_intent_ids,
    human_paper_consumed_signal_lifecycle_ids,
    human_paper_oldest_open_lot_sessions,
    human_paper_pending_sell_quantities,
    human_paper_position_quantities,
    human_paper_portfolio_rejected_intent_ids,
    human_paper_terminal_intent_ids,
    latest_human_paper_pending_continuity,
    load_human_paper_ledger,
    reconcile_human_paper_feedback,
    settle_human_paper_intents,
    settle_human_paper_intents_with_capital_controls,
    settle_human_paper_intents_with_portfolio_controls,
)
from chanlun.decision_support.trading_system.models import EntryExecutionBoundary
from chanlun.decision_support.trading_system.v3_human_review_screening import (
    HumanReviewAlert,
    HumanReviewFeedback,
    SectorRankingReviewEvidence,
    human_review_screening_parameters,
)
from chanlun.decision_support.trading_system.v3_qmt_sector_ledger import (
    catalog_capture_entry,
)
from chanlun.decision_support.trading_system.v3_technical_approximation import (
    technical_approximation_parameters,
)


TZ = ZoneInfo("Asia/Shanghai")
REVIEWED_AT = datetime(2026, 7, 28, 10, 0, tzinfo=TZ)
PARAMETER_SNAPSHOT = (
    Path(__file__).resolve().parents[2]
    / "audit"
    / "chanlun_trading_system_backtest"
    / "recent_year_current_sector_no3p"
    / "parameter_snapshot_human_review.json"
)
ACCOUNTING_PARAMETERS = load_human_paper_accounting_parameters(PARAMETER_SNAPSHOT)


def _alert(
    *,
    market_gate: str = "GREEN",
    sector_gate: str = "GREEN",
    symbol_gate: str = "GREEN",
    alert_type: str = "POSSIBLE_30M_BUY",
) -> HumanReviewAlert:
    signal_at = REVIEWED_AT - timedelta(minutes=30)
    return HumanReviewAlert(
        symbol="SH.600000",
        alert_type=alert_type,
        signal_at=signal_at,
        review_available_at=REVIEWED_AT,
        source_point_id="sha256:" + "1" * 64,
        structure_snapshot_id="sha256:" + "2" * 64,
        sector_id="qmt-gics3:" + "3" * 64,
        confidence="MEDIUM",
        review_priority=70,
        reference_price=Decimal("10.00"),
        structural_invalidation_price=Decimal("9.50"),
        market_risk_gate=market_gate,
        sector_risk_gate=sector_gate,
        symbol_risk_gate=symbol_gate,
        warning_codes=(),
        source_fact_ids=("sha256:" + "4" * 64,),
        screening_parameter_set_id=(
            human_review_screening_parameters().parameter_set_id
        ),
        technical_approximation_parameter_set_id=(
            technical_approximation_parameters().parameter_set_id
        ),
        # Ledger-focused fixtures use a deliberately wide attested boundary so
        # cash, slot, T+1 and restart tests remain independent of the live
        # gateway's exact one-minute TTL.  Dedicated tests below pin the real
        # short-lived boundary and price-cap behavior.
        entry_confirmation_bar_closed_at=signal_at,
        entry_price_cap=Decimal("100000.00"),
        entry_valid_until=REVIEWED_AT + timedelta(days=30),
        entry_boundary_evidence_id="sha256:" + "b" * 64,
    )


def _feedback(alert: HumanReviewAlert) -> HumanReviewFeedback:
    return HumanReviewFeedback(
        candidate_id=alert.candidate_id,
        source_screen_content_sha256="sha256:" + "5" * 64,
        reviewer="reviewer-1",
        reviewed_at=REVIEWED_AT,
        center_judgement="CONFIRMED",
        trend_judgement="UP",
        level_judgement="30M",
        point_judgement="BUY_3",
        disposition="PAPER_OBSERVE",
        signal_lifecycle_id=alert.signal_lifecycle_id,
    )


def _live_ranked_alert_and_catalog() -> tuple[
    HumanReviewAlert,
    dict[str, object],
]:
    captured_at = REVIEWED_AT - timedelta(minutes=45)
    sectors = (
        {
            "sector_id": _alert().sector_id,
            "name": "银行",
            "source_key": "GICS3银行",
            "member_codes": (_alert().symbol,),
        },
    )
    revision = sha256_json(
        {"schema": "chanlun-qmt-gics3-catalog/v1", "sectors": sectors}
    )
    catalog = catalog_capture_entry(
        {
            "source": "qmt_gics3_components",
            "captured_at": captured_at.isoformat(),
            "point_in_time_scope": "CURRENT_CAPTURE_ONLY",
            "catalog_revision": revision,
            "sectors": sectors,
        },
        previous_entry_sha256=None,
    )
    base = _alert()
    ranking = SectorRankingReviewEvidence(
        source_profile="LIVE_FULL_RANKING",
        sector_id=base.sector_id,
        sector_name="银行",
        observed_at=base.signal_at,
        eligible=True,
        hard_block=False,
        regime="supportive",
        ordinal=1,
        rank_score=45,
        rank_components=(("neutral_access", 5), ("thirty_support", 40)),
        reason_codes=("structural_ranking_only",),
        horizontal_strength=Decimal("7.5"),
        horizontal_rank=1,
        strength_observed_at=base.signal_at,
        strength_anchor_session=base.signal_at.date() - timedelta(days=1),
        strength_member_count=1,
        strength_source_revision="sha256:" + "7" * 64,
        strength_evidence_revision="sha256:" + "8" * 64,
        sector_catalog_revision=revision,
    )
    return (
        replace(
            base,
            sector_ranking_evidence=ranking,
            source_fact_ids=(*base.source_fact_ids, ranking.evidence_id),
        ),
        catalog,
    )


def _entry_selection_evidence(
    alert: HumanReviewAlert,
    catalog: Mapping[str, object],
) -> HumanPaperEntrySelectionEvidence:
    ranking = alert.sector_ranking_evidence
    assert ranking is not None
    feedback = _feedback(alert)
    return HumanPaperEntrySelectionEvidence(
        feedback_id=feedback.feedback_id,
        candidate_id=feedback.candidate_id,
        source_screen_content_sha256=feedback.source_screen_content_sha256,
        symbol=alert.symbol,
        sector_id=ranking.sector_id,
        sector_name=ranking.sector_name,
        sector_ranking_evidence_id=ranking.evidence_id,
        sector_ranking_observed_at=ranking.observed_at,
        sector_catalog_revision=str(catalog["catalog_revision"]),
        sector_catalog_entry_sha256=str(catalog["entry_sha256"]),
        sector_catalog_captured_at=datetime.fromisoformat(
            str(catalog["captured_at"])
        ),
        attested_at=feedback.reviewed_at,
    )


def _sell_feedback(alert: HumanReviewAlert) -> HumanReviewFeedback:
    return replace(
        _feedback(alert),
        reviewed_at=REVIEWED_AT + timedelta(hours=1),
        trend_judgement="DOWN",
        point_judgement="SELL_1",
    )


def _bar(
    opened_at: datetime,
    *,
    complete: bool = True,
    suspended: bool = False,
    limit_up_locked: bool = False,
    limit_down_locked: bool = False,
    security_status_complete: bool = True,
    corporate_action_state_complete: bool = True,
) -> HumanPaperMinuteBar:
    return HumanPaperMinuteBar(
        symbol="SH.600000",
        opened_at=opened_at,
        closed_at=opened_at + timedelta(minutes=1),
        open=Decimal("10.10"),
        high=Decimal("10.20"),
        low=Decimal("10.00"),
        close=Decimal("10.15"),
        volume=Decimal("10000"),
        complete=complete,
        suspended=suspended,
        limit_up_locked=limit_up_locked,
        limit_down_locked=limit_down_locked,
        buy_eligible=True,
        sell_eligible=True,
        security_status_complete=security_status_complete,
        corporate_action_state_complete=corporate_action_state_complete,
        execution_snapshot_sha256="sha256:" + "6" * 64,
    )


@pytest.mark.parametrize(
    ("opened_at", "closed_at"),
    (
        (
            datetime(2026, 7, 28, 10, 0, tzinfo=TZ),
            datetime(2026, 7, 28, 10, 0, 30, tzinfo=TZ),
        ),
        (
            datetime(2026, 7, 28, 10, 0, tzinfo=TZ),
            datetime(2026, 7, 28, 10, 5, tzinfo=TZ),
        ),
        (
            datetime(2026, 7, 28, 10, 0, 30, tzinfo=TZ),
            datetime(2026, 7, 28, 10, 1, 30, tzinfo=TZ),
        ),
        (
            datetime(2026, 7, 28, 11, 59, tzinfo=TZ),
            datetime(2026, 7, 28, 12, 0, tzinfo=TZ),
        ),
        (
            datetime(2026, 7, 28, 9, 29, tzinfo=TZ),
            datetime(2026, 7, 28, 9, 30, tzinfo=TZ),
        ),
    ),
)
def test_paper_execution_bar_requires_exact_exchange_aligned_one_minute(
    opened_at: datetime,
    closed_at: datetime,
) -> None:
    with pytest.raises(ValueError, match="paper execution"):
        replace(
            _bar(REVIEWED_AT),
            opened_at=opened_at,
            closed_at=closed_at,
        )


@pytest.mark.parametrize(
    ("opened_at", "closed_at"),
    (
        (
            datetime(2026, 7, 28, 9, 30, tzinfo=TZ),
            datetime(2026, 7, 28, 9, 31, tzinfo=TZ),
        ),
        (
            datetime(2026, 7, 28, 11, 29, tzinfo=TZ),
            datetime(2026, 7, 28, 11, 30, tzinfo=TZ),
        ),
        (
            datetime(2026, 7, 28, 13, 0, tzinfo=TZ),
            datetime(2026, 7, 28, 13, 1, tzinfo=TZ),
        ),
        (
            datetime(2026, 7, 28, 14, 59, tzinfo=TZ),
            datetime(2026, 7, 28, 15, 0, tzinfo=TZ),
        ),
    ),
)
def test_paper_execution_bar_accepts_continuous_auction_boundaries(
    opened_at: datetime,
    closed_at: datetime,
) -> None:
    value = replace(
        _bar(REVIEWED_AT),
        opened_at=opened_at,
        closed_at=closed_at,
    )
    assert value.opened_at == opened_at
    assert value.closed_at == closed_at


def _accounting_fill(
    symbol: str,
    *,
    price: Decimal,
    filled_at: datetime,
) -> dict[str, object]:
    return {
        "kind": "FILL",
        "payload": {
            "symbol": symbol,
            "side": "BUY",
            "quantity": 100,
            "price": format(price, "f"),
            "filled_at": filled_at.isoformat(),
        },
    }


def test_portfolio_gate_enforces_exact_slot_fraction_boundary() -> None:
    exact = assess_human_paper_portfolio_fill(
        (),
        parameters=ACCOUNTING_PARAMETERS,
        symbol="SH.600000",
        quantity=100,
        price=Decimal("1800"),
        session=REVIEWED_AT.date(),
        position_marks={},
    )
    assert Decimal(str(exact["slot_notional_cap"])) == Decimal("180000")
    assert exact["notional"] == "180000.00"
    assert exact["allowed"] is True

    above = assess_human_paper_portfolio_fill(
        (),
        parameters=ACCOUNTING_PARAMETERS,
        symbol="SH.600000",
        quantity=100,
        price=Decimal("1800.01"),
        session=REVIEWED_AT.date(),
        position_marks={},
    )
    assert above["allowed"] is False
    assert above["reason_codes"] == [
        "VIRTUAL_ENTRY_EXCEEDS_ONE_SLOT_NOTIONAL_CAP"
    ]


def test_portfolio_gate_enforces_exact_ninety_percent_exposure_boundary() -> None:
    events = tuple(
        _accounting_fill(
            f"SH.60000{index}",
            price=Decimal("1800"),
            filled_at=REVIEWED_AT + timedelta(minutes=index),
        )
        for index in range(4)
    )
    marks = {f"SH.60000{index}": Decimal("1800") for index in range(4)}
    probe = assess_human_paper_portfolio_fill(
        events,
        parameters=ACCOUNTING_PARAMETERS,
        symbol="SH.600009",
        quantity=100,
        price=Decimal("1"),
        session=REVIEWED_AT.date(),
        position_marks=marks,
    )
    exact_notional = (
        Decimal(str(probe["account_exposure_notional_cap"]))
        - Decimal(str(probe["current_market_value"]))
    )
    exact_price = exact_notional / Decimal("100")
    exact = assess_human_paper_portfolio_fill(
        events,
        parameters=ACCOUNTING_PARAMETERS,
        symbol="SH.600009",
        quantity=100,
        price=exact_price,
        session=REVIEWED_AT.date(),
        position_marks=marks,
    )
    assert exact["post_trade_gross_market_value"] == exact[
        "account_exposure_notional_cap"
    ]
    assert exact["allowed"] is True

    above = assess_human_paper_portfolio_fill(
        events,
        parameters=ACCOUNTING_PARAMETERS,
        symbol="SH.600009",
        quantity=100,
        price=exact_price + Decimal("0.0001"),
        session=REVIEWED_AT.date(),
        position_marks=marks,
    )
    assert above["allowed"] is False
    assert above["reason_codes"] == [
        "VIRTUAL_ACCOUNT_EXPOSURE_CAP_EXCEEDED"
    ]


def test_green_human_confirmation_fills_only_on_later_completed_1m_bar(
    tmp_path: Path,
) -> None:
    path = tmp_path / "paper.json"
    alert = _alert()
    intent = build_human_paper_intent(feedback=_feedback(alert), alert=alert)
    assert intent is not None
    assert intent.status == "PENDING"

    first, _event = append_human_paper_intent(path, intent)
    duplicate, _event = append_human_paper_intent(path, intent)
    assert len(first["events"]) == len(duplicate["events"]) == 1

    settled = settle_human_paper_intents(
        path,
        bars_by_symbol={
            alert.symbol: (
                # The signal/feedback bar opened before the decision and is
                # therefore never eligible.  A bar opening exactly at the
                # decision boundary is the first later full 1m interval.
                _bar(REVIEWED_AT - timedelta(minutes=1)),
                _bar(REVIEWED_AT + timedelta(minutes=1), complete=False),
                _bar(REVIEWED_AT + timedelta(minutes=2), suspended=True),
                _bar(REVIEWED_AT + timedelta(minutes=3), limit_up_locked=True),
                _bar(REVIEWED_AT + timedelta(minutes=4)),
            )
        },
    )
    fills = [event for event in settled["events"] if event["kind"] == "FILL"]
    assert len(fills) == 1
    assert fills[0]["payload"]["price"] == "10.20"
    assert fills[0]["payload"]["filled_at"] == (
        REVIEWED_AT + timedelta(minutes=5)
    ).isoformat()
    assert fills[0]["payload"]["tick_data_used"] is False
    assert fills[0]["payload"]["virtual_only"] is True
    assert fills[0]["payload"]["execution_snapshot_sha256"] == (
        "sha256:" + "6" * 64
    )

    repeated = settle_human_paper_intents(
        path,
        bars_by_symbol={alert.symbol: (_bar(REVIEWED_AT + timedelta(minutes=4)),)},
    )
    assert repeated == settled


def test_monitor_only_buy_cannot_create_a_pending_virtual_entry() -> None:
    """Sector-first selection cannot be bypassed through human paper feedback."""

    alert = replace(
        _alert(),
        warning_codes=("MONITOR_ONLY_NOT_CURRENT_SECTOR_TRIGGER",),
    )

    intent = build_human_paper_intent(feedback=_feedback(alert), alert=alert)

    assert intent is not None
    assert intent.status == "OBSERVATION_ONLY"
    assert intent.reason_codes == (
        "BUY_NOT_TRIGGERED_BY_CURRENT_QMT_SECTOR",
        "MONITOR_ONLY_NEW_ENTRY_PROHIBITED",
    )


def test_monitor_only_sell_keeps_virtual_position_exit_available() -> None:
    """A sector-entry gate must never strand an existing virtual position."""

    alert = replace(
        _alert(alert_type="POSSIBLE_30M_EXIT"),
        warning_codes=("MONITOR_ONLY_NOT_CURRENT_SECTOR_TRIGGER",),
        entry_confirmation_bar_closed_at=None,
        entry_price_cap=None,
        entry_valid_until=None,
        entry_boundary_evidence_id=None,
    )

    intent = build_human_paper_intent(
        feedback=_sell_feedback(alert),
        alert=alert,
        virtual_position_quantity=100,
    )

    assert intent is not None
    assert intent.status == "PENDING"
    assert intent.side == "SELL"
    assert intent.reason_codes == ("HUMAN_CONFIRMED_VIRTUAL_EXIT",)


def test_strategic_buy_without_exact_confirmation_boundary_is_review_only() -> None:
    alert = replace(
        _alert(),
        entry_confirmation_bar_closed_at=None,
        entry_price_cap=None,
        entry_valid_until=None,
        entry_boundary_evidence_id=None,
    )

    intent = build_human_paper_intent(feedback=_feedback(alert), alert=alert)

    assert intent is not None
    assert intent.status == "OBSERVATION_ONLY"
    assert intent.reason_codes == (
        "BUY_EXECUTION_BOUNDARY_EVIDENCE_MISSING",
        "STRUCTURE_ANCHOR_IS_NOT_A_BUY_PRICE_CAP",
    )


def test_strategic_buy_expired_before_human_confirmation_is_review_only() -> None:
    alert = replace(
        _alert(),
        entry_confirmation_bar_closed_at=REVIEWED_AT - timedelta(minutes=1),
        entry_valid_until=REVIEWED_AT,
    )

    intent = build_human_paper_intent(feedback=_feedback(alert), alert=alert)

    assert intent is not None
    assert intent.status == "OBSERVATION_ONLY"
    assert intent.reason_codes == (
        "BUY_ENTRY_TTL_EXPIRED_BEFORE_HUMAN_CONFIRMATION",
        "NEW_STRUCTURE_REQUIRED_NO_PRICE_CHASING",
    )


def test_strategic_buy_without_a_remaining_causal_minute_is_review_only() -> None:
    """A partial minute after review cannot be replayed as a full 1m fill bar."""

    alert = replace(
        _alert(),
        signal_at=REVIEWED_AT,
        entry_confirmation_bar_closed_at=REVIEWED_AT,
        entry_valid_until=REVIEWED_AT + timedelta(minutes=1),
    )
    feedback = replace(
        _feedback(alert),
        reviewed_at=REVIEWED_AT + timedelta(seconds=30),
    )

    intent = build_human_paper_intent(feedback=feedback, alert=alert)

    assert intent is not None
    assert intent.status == "OBSERVATION_ONLY"
    assert intent.reason_codes == (
        "NO_CAUSAL_1M_EXECUTION_BAR_REMAINS_BEFORE_TTL",
        "NEW_STRUCTURE_REQUIRED_NO_PRICE_CHASING",
    )


def test_strategic_buy_intent_retains_full_entry_boundary_attestation() -> None:
    boundary = EntryExecutionBoundary(
        symbol="SH.600000",
        point_id="sha256:" + "9" * 64,
        source_frequency="1m",
        confirmation_bar_closed_at=REVIEWED_AT,
        raw_open=Decimal("10.00"),
        raw_high=Decimal("10.05"),
        raw_low=Decimal("9.98"),
        raw_close=Decimal("10.03"),
        raw_volume=Decimal("10000"),
        entry_valid_until=REVIEWED_AT + timedelta(minutes=1),
        raw_price_basis_revision="qmt-none-test-v1",
    )
    alert = replace(
        _alert(),
        signal_at=REVIEWED_AT,
        source_fact_ids=(*_alert().source_fact_ids, boundary.evidence_id),
        entry_confirmation_bar_closed_at=boundary.confirmation_bar_closed_at,
        entry_price_cap=boundary.raw_high,
        entry_valid_until=boundary.entry_valid_until,
        entry_boundary_evidence_id=boundary.evidence_id,
        entry_execution_boundary=boundary,
    )

    intent = build_human_paper_intent(feedback=_feedback(alert), alert=alert)

    assert intent is not None and intent.status == "PENDING"
    assert intent.entry_execution_boundary == boundary
    assert intent.entry_execution_boundary.evidence_id == (
        intent.entry_boundary_evidence_id
    )

    attestation = audit_human_paper_entry_boundary_attestations(
        (
            {
                "kind": "INTENT",
                "payload": intent.document(),
            },
        )
    )
    assert attestation["status"] == "COMPLETE"
    assert attestation["verified_full_boundary_count"] == 1

    forged = json.loads(json.dumps(intent.document()))
    forged["entry_execution_boundary"]["raw_high"] = "10.06"
    forged_audit = audit_human_paper_entry_boundary_attestations(
        ({"kind": "INTENT", "payload": forged},)
    )
    assert forged_audit["status"] == "INVALID"
    assert forged_audit["verified_full_boundary_count"] == 0


def test_live_ranked_buy_retains_replayable_exact_qmt_selection_evidence(
    tmp_path: Path,
) -> None:
    alert, catalog = _live_ranked_alert_and_catalog()
    feedback = _feedback(alert)

    # The shared ledger core—not only the page endpoint—fails closed when a
    # live-ranked strategic buy has no exact QMT catalog proof.
    assert build_human_paper_intent(feedback=feedback, alert=alert) is None

    evidence = _entry_selection_evidence(alert, catalog)
    intent = build_human_paper_intent(
        feedback=feedback,
        alert=alert,
        entry_selection_evidence=evidence,
    )
    assert intent is not None and intent.status == "PENDING"
    assert intent.entry_selection_evidence == evidence
    assert intent.document()["entry_selection_evidence"] == evidence.document()

    audit = audit_human_paper_entry_selection_attestations(
        ({"kind": "INTENT", "payload": intent.document()},),
        sector_catalog_entries=(catalog,),
    )
    assert audit["status"] == "COMPLETE"
    assert audit["attested_buy_intent_count"] == 1
    assert audit["verified_catalog_binding_count"] == 1
    assert audit["selection_evidence_ids"] == [evidence.evidence_id]
    assert audit["catalog_entry_sha256s"] == [catalog["entry_sha256"]]
    source_audit = audit_human_paper_entry_selection_source_bindings(
        ({"kind": "INTENT", "payload": intent.document()},),
        alerts_by_source_content_sha256={
            intent.source_screen_content_sha256: (alert,)
        },
    )
    assert source_audit["status"] == "COMPLETE"
    assert source_audit["verified_required_buy_intent_ids"] == [
        intent.intent_id
    ]

    # Restart/loading keeps the nested proof and its identity intact.
    path = tmp_path / "paper.json"
    append_human_paper_intent(path, intent)
    loaded = load_human_paper_ledger(path)
    assert loaded["events"][0]["payload"] == intent.document()


def test_entry_selection_audit_rejects_missing_member_and_tampering() -> None:
    alert, catalog = _live_ranked_alert_and_catalog()
    feedback = _feedback(alert)
    evidence = _entry_selection_evidence(alert, catalog)
    intent = build_human_paper_intent(
        feedback=feedback,
        alert=alert,
        entry_selection_evidence=evidence,
    )
    assert intent is not None

    unavailable = audit_human_paper_entry_selection_attestations(
        ({"kind": "INTENT", "payload": intent.document()},)
    )
    assert unavailable["status"] == "INCOMPLETE_CATALOG_ARCHIVE"
    assert unavailable["catalog_unavailable_intent_ids"] == [intent.intent_id]

    bad_sectors = (
        {
            "sector_id": alert.sector_id,
            "name": "银行",
            "source_key": "GICS3银行",
            "member_codes": ("SZ.000001",),
        },
    )
    bad_revision = sha256_json(
        {
            "schema": "chanlun-qmt-gics3-catalog/v1",
            "sectors": bad_sectors,
        }
    )
    bad_catalog = catalog_capture_entry(
        {
            "source": "qmt_gics3_components",
            "captured_at": catalog["captured_at"],
            "point_in_time_scope": "CURRENT_CAPTURE_ONLY",
            "catalog_revision": bad_revision,
            "sectors": bad_sectors,
        },
        previous_entry_sha256=None,
    )
    bad_evidence = replace(
        evidence,
        sector_catalog_revision=bad_revision,
        sector_catalog_entry_sha256=str(bad_catalog["entry_sha256"]),
    )
    forged = intent.document()
    forged["entry_selection_evidence"] = bad_evidence.document()
    rejected = audit_human_paper_entry_selection_attestations(
        ({"kind": "INTENT", "payload": forged},),
        sector_catalog_entries=(bad_catalog,),
    )
    assert rejected["status"] == "INVALID"
    assert rejected["verified_catalog_binding_count"] == 0

    identity_tampering = json.loads(json.dumps(intent.document()))
    identity_tampering["entry_selection_evidence"]["sector_name"] = "伪造板块"
    tampered = audit_human_paper_entry_selection_attestations(
        ({"kind": "INTENT", "payload": identity_tampering},),
        sector_catalog_entries=(catalog,),
    )
    assert tampered["status"] == "INVALID"
    assert "identity changed" in tampered["invalid_attestations"][0]["reason"]


def test_entry_selection_evidence_binds_source_and_chronology() -> None:
    alert, catalog = _live_ranked_alert_and_catalog()
    evidence = _entry_selection_evidence(alert, catalog)

    with pytest.raises(ValueError, match="chronology"):
        replace(
            evidence,
            sector_catalog_captured_at=(
                evidence.sector_ranking_observed_at + timedelta(minutes=1)
            ),
        )

    with pytest.raises(ValueError, match="differs from source"):
        build_human_paper_intent(
            feedback=_feedback(alert),
            alert=alert,
            entry_selection_evidence=replace(evidence, sector_name="伪造板块"),
        )


def test_pre_attestation_intent_identity_and_document_remain_unchanged() -> None:
    intent = build_human_paper_intent(feedback=_feedback(_alert()), alert=_alert())
    assert intent is not None
    document = intent.document()
    assert "entry_selection_evidence" not in document
    stable = asdict(intent)
    stable.pop("entry_selection_evidence")
    stable.pop("entry_execution_boundary")
    assert intent.intent_id == sha256_json(stable)


def test_first_executable_bar_above_cap_is_terminal_no_chase(
    tmp_path: Path,
) -> None:
    path = tmp_path / "paper.json"
    alert = replace(
        _alert(),
        signal_at=REVIEWED_AT,
        entry_confirmation_bar_closed_at=REVIEWED_AT,
        entry_price_cap=Decimal("10.05"),
        entry_valid_until=REVIEWED_AT + timedelta(minutes=1),
    )
    intent = build_human_paper_intent(feedback=_feedback(alert), alert=alert)
    assert intent is not None and intent.status == "PENDING"
    append_human_paper_intent(path, intent)

    rejected = settle_human_paper_intents(
        path,
        bars_by_symbol={
            alert.symbol: (
                replace(
                    _bar(REVIEWED_AT),
                    open=Decimal("10.10"),
                    high=Decimal("10.20"),
                    low=Decimal("10.06"),
                    close=Decimal("10.15"),
                    # Price-cap rejection is independently observable even
                    # when the completed bar cannot prove fill capacity.
                    volume=Decimal("0"),
                ),
            )
        },
    )
    assert [event["kind"] for event in rejected["events"]] == [
        "INTENT",
        "EXECUTION_REJECT",
    ]
    assert rejected["events"][-1]["payload"]["reason_code"] == (
        "BUY_PRICE_CAP_EXCEEDED_AT_FIRST_EXECUTABLE_BAR"
    )

    cheaper = replace(
        _bar(REVIEWED_AT + timedelta(minutes=1)),
        open=Decimal("10.00"),
        high=Decimal("10.04"),
        low=Decimal("9.99"),
        close=Decimal("10.02"),
    )
    repeated = settle_human_paper_intents(
        path,
        bars_by_symbol={alert.symbol: (cheaper,)},
    )
    assert repeated == rejected


@pytest.mark.parametrize(
    ("high", "volume", "expected_kind", "expected_reason"),
    (
        (
            Decimal("10.20"),
            Decimal("10000"),
            "EXECUTION_REJECT",
            "BUY_ORDER_TTL_EXPIRED_WITHOUT_FILL",
        ),
        (
            Decimal("10.25"),
            Decimal("10000"),
            "EXECUTION_REJECT",
            "BUY_ORDER_TTL_EXPIRED_WITHOUT_FILL",
        ),
        (
            Decimal("10.19"),
            Decimal("1900"),
            "EXECUTION_REJECT",
            "BUY_ORDER_TTL_EXPIRED_WITHOUT_FILL",
        ),
        (Decimal("10.19"), Decimal("2000"), "FILL", None),
    ),
)
def test_buy_requires_whole_bar_strict_cross_and_five_percent_capacity(
    tmp_path: Path,
    high: Decimal,
    volume: Decimal,
    expected_kind: str,
    expected_reason: str | None,
) -> None:
    """A completed 1m proxy may never infer fills from touch/mixed volume."""

    path = tmp_path / f"paper-{high}-{volume}.json"
    alert = replace(
        _alert(),
        signal_at=REVIEWED_AT,
        entry_confirmation_bar_closed_at=REVIEWED_AT,
        entry_price_cap=Decimal("10.20"),
        entry_valid_until=REVIEWED_AT + timedelta(minutes=1),
    )
    intent = build_human_paper_intent(feedback=_feedback(alert), alert=alert)
    assert intent is not None and intent.status == "PENDING"
    append_human_paper_intent(path, intent)
    candidate = replace(
        _bar(REVIEWED_AT),
        high=high,
        volume=volume,
    )

    settled = settle_human_paper_intents(
        path,
        bars_by_symbol={alert.symbol: (candidate,)},
    )

    terminal = settled["events"][-1]
    assert terminal["kind"] == expected_kind
    if expected_reason is not None:
        assert terminal["payload"]["reason_code"] == expected_reason


def test_buy_fill_uses_adverse_completed_bar_price_and_close_time(
    tmp_path: Path,
) -> None:
    """Whole-bar facts cannot create an open-time fill at the open price."""

    path = tmp_path / "paper-causal-price-time.json"
    alert = replace(
        _alert(),
        signal_at=REVIEWED_AT,
        entry_confirmation_bar_closed_at=REVIEWED_AT,
        entry_price_cap=Decimal("10.20"),
        entry_valid_until=REVIEWED_AT + timedelta(minutes=1),
    )
    intent = build_human_paper_intent(feedback=_feedback(alert), alert=alert)
    assert intent is not None and intent.status == "PENDING"
    append_human_paper_intent(path, intent)
    candidate = replace(
        _bar(REVIEWED_AT),
        open=Decimal("10.10"),
        high=Decimal("10.19"),
        low=Decimal("10.00"),
        close=Decimal("10.15"),
        volume=Decimal("2000"),
    )

    settled = settle_human_paper_intents(
        path,
        bars_by_symbol={alert.symbol: (candidate,)},
    )

    fill = settled["events"][-1]
    assert fill["kind"] == "FILL"
    assert fill["payload"]["price"] == "10.19"
    assert fill["payload"]["filled_at"] == candidate.closed_at.isoformat()
    assert fill["payload"]["source_bar_closed_at"] == (
        candidate.closed_at.isoformat()
    )


def test_buy_without_eligible_bar_is_terminal_at_ttl(tmp_path: Path) -> None:
    path = tmp_path / "paper.json"
    alert = replace(
        _alert(),
        signal_at=REVIEWED_AT,
        entry_confirmation_bar_closed_at=REVIEWED_AT,
        entry_price_cap=Decimal("10.20"),
        entry_valid_until=REVIEWED_AT + timedelta(minutes=1),
    )
    intent = build_human_paper_intent(feedback=_feedback(alert), alert=alert)
    assert intent is not None and intent.status == "PENDING"
    append_human_paper_intent(path, intent)

    rejected = settle_human_paper_intents(
        path,
        bars_by_symbol={
            alert.symbol: (_bar(REVIEWED_AT, limit_up_locked=True),)
        },
    )

    assert rejected["events"][-1]["kind"] == "EXECUTION_REJECT"
    assert rejected["events"][-1]["payload"]["reason_code"] == (
        "BUY_ORDER_TTL_EXPIRED_WITHOUT_FILL"
    )


def test_later_watch_feedback_cancels_pending_intent_idempotently(
    tmp_path: Path,
) -> None:
    path = tmp_path / "paper.json"
    alert = _alert()
    first_feedback = _feedback(alert)
    first, first_event, cancellations, changed = reconcile_human_paper_feedback(
        path,
        feedback=first_feedback,
        alert=alert,
    )
    assert changed is True
    assert first_event is not None
    assert cancellations == ()
    intent_id = str(first_event["payload"]["intent_id"])

    later = replace(
        first_feedback,
        reviewed_at=REVIEWED_AT + timedelta(minutes=5),
        disposition="WATCH",
        notes="结构尚未确认，撤销待模拟成交",
    )
    cancelled, replacement, cancellations, changed = (
        reconcile_human_paper_feedback(path, feedback=later, alert=alert)
    )
    assert changed is True
    assert replacement is None
    assert [event["kind"] for event in cancelled["events"]] == ["INTENT", "CANCEL"]
    assert len(cancellations) == 1
    assert cancellations[0]["payload"]["intent_id"] == intent_id
    assert cancellations[0]["payload"]["status"] == "CANCELLED"
    assert cancellations[0]["payload"]["superseding_feedback_id"] == (
        later.feedback_id
    )

    retried, retry_replacement, retry_cancellations, retry_changed = (
        reconcile_human_paper_feedback(path, feedback=later, alert=alert)
    )
    assert retried == cancelled
    assert retry_replacement is None
    assert retry_cancellations == ()
    assert retry_changed is False

    settled = settle_human_paper_intents(
        path,
        bars_by_symbol={
            alert.symbol: (_bar(REVIEWED_AT + timedelta(minutes=10)),)
        },
    )
    assert [event["kind"] for event in settled["events"]] == ["INTENT", "CANCEL"]
    continuity = audit_human_paper_pending_continuity(
        tuple(settled["events"]),
        forward_root=tmp_path,
        current_session=REVIEWED_AT.date(),
        trading_sessions=(),
    )
    assert continuity["status"] == "NO_PENDING_INTENTS"


def test_later_paper_feedback_replaces_instead_of_double_filling(
    tmp_path: Path,
) -> None:
    path = tmp_path / "paper.json"
    alert = _alert()
    first_feedback = _feedback(alert)
    _first, first_event, _cancellations, _changed = (
        reconcile_human_paper_feedback(path, feedback=first_feedback, alert=alert)
    )
    assert first_event is not None
    first_intent_id = str(first_event["payload"]["intent_id"])
    later = replace(
        first_feedback,
        reviewed_at=REVIEWED_AT + timedelta(minutes=5),
        notes="按最新人工复核重新建立模拟观察",
    )

    replaced, replacement, cancellations, changed = reconcile_human_paper_feedback(
        path,
        feedback=later,
        alert=alert,
    )

    assert changed is True
    assert replacement is not None
    replacement_id = str(replacement["payload"]["intent_id"])
    assert replacement_id != first_intent_id
    assert [event["kind"] for event in replaced["events"]] == [
        "INTENT",
        "CANCEL",
        "INTENT",
    ]
    assert cancellations[0]["payload"]["intent_id"] == first_intent_id

    settled = settle_human_paper_intents(
        path,
        bars_by_symbol={
            alert.symbol: (_bar(REVIEWED_AT + timedelta(minutes=10)),)
        },
    )
    fills = [event for event in settled["events"] if event["kind"] == "FILL"]
    assert len(fills) == 1
    assert fills[0]["payload"]["intent_id"] == replacement_id


def test_later_feedback_never_cancels_an_already_filled_intent(
    tmp_path: Path,
) -> None:
    path = tmp_path / "paper.json"
    alert = _alert()
    first_feedback = _feedback(alert)
    reconcile_human_paper_feedback(path, feedback=first_feedback, alert=alert)
    filled = settle_human_paper_intents(
        path,
        bars_by_symbol={
            alert.symbol: (_bar(REVIEWED_AT + timedelta(minutes=1)),)
        },
    )
    assert [event["kind"] for event in filled["events"]] == ["INTENT", "FILL"]
    later = replace(
        first_feedback,
        reviewed_at=REVIEWED_AT + timedelta(minutes=5),
        disposition="REJECT",
        notes="成交后的新判断只能追加反馈，不能撤销历史成交",
    )

    reconciled, replacement, cancellations, changed = (
        reconcile_human_paper_feedback(path, feedback=later, alert=alert)
    )

    assert replacement is None
    assert cancellations == ()
    assert changed is False
    assert reconciled == filled


def test_production_settlement_rejects_buy_without_cash_including_fees(
    tmp_path: Path,
) -> None:
    path = tmp_path / "paper.json"
    alert = _alert()
    intent = build_human_paper_intent(feedback=_feedback(alert), alert=alert)
    assert intent is not None
    append_human_paper_intent(path, intent)
    expensive = replace(
        _bar(REVIEWED_AT + timedelta(minutes=1)),
        open=Decimal("10000.00"),
        high=Decimal("10001.00"),
        low=Decimal("9999.00"),
        close=Decimal("10000.00"),
    )

    rejected, evaluations = settle_human_paper_intents_with_capital_controls(
        path,
        bars_by_symbol={alert.symbol: (expensive,)},
        accounting_parameters=ACCOUNTING_PARAMETERS,
    )

    assert [event["kind"] for event in rejected["events"]] == [
        "INTENT",
        "CAPITAL_REJECT",
    ]
    rejection = rejected["events"][-1]["payload"]
    assert rejection["status"] == "CAPITAL_REJECTED"
    assert rejection["reason_codes"] == [
        "INSUFFICIENT_VIRTUAL_CASH_INCLUDING_FEES"
    ]
    assert rejection["required_cash"] == "1000410.03"
    assert human_paper_capital_rejected_intent_ids(rejected["events"]) == {
        intent.intent_id
    }
    assert evaluations[0]["result"] == "CAPITAL_REJECTED"
    assert evaluations[0]["fixed_one_lot_diagnostic"] is True
    assert evaluations[0]["slot_fraction_notional_gate_evaluable"] is False
    assert human_paper_position_quantities(rejected["events"]) == {}
    decision_audit = audit_human_paper_capital_decisions(
        rejected["events"],
        parameters=ACCOUNTING_PARAMETERS,
    )
    assert decision_audit["status"] == "COMPLETE"
    assert decision_audit["verified_rejection_count"] == 1
    assert audit_human_paper_pending_continuity(
        rejected["events"],
        forward_root=tmp_path,
        current_session=REVIEWED_AT.date(),
        trading_sessions=(),
    )["status"] == "NO_PENDING_INTENTS"

    retried, retry_evaluations = (
        settle_human_paper_intents_with_capital_controls(
            path,
            bars_by_symbol={alert.symbol: (expensive,)},
            accounting_parameters=ACCOUNTING_PARAMETERS,
        )
    )
    assert retried == rejected
    assert retry_evaluations == ()


def test_production_settlement_fills_only_five_distinct_symbol_slots(
    tmp_path: Path,
) -> None:
    path = tmp_path / "paper.json"
    base_alert = _alert()
    base_intent = build_human_paper_intent(
        feedback=_feedback(base_alert),
        alert=base_alert,
    )
    assert base_intent is not None
    bars_by_symbol: dict[str, tuple[HumanPaperMinuteBar, ...]] = {}
    symbols = tuple(f"SH.60000{index}" for index in range(6))
    for index, symbol in enumerate(symbols):
        intent = replace(
            base_intent,
            symbol=symbol,
            feedback_id="sha256:" + f"{index + 1:x}" * 64,
            signal_lifecycle_id=sha256_json({"symbol": symbol}),
        )
        append_human_paper_intent(path, intent)
        bars_by_symbol[symbol] = (
            replace(
                _bar(REVIEWED_AT + timedelta(minutes=1)),
                symbol=symbol,
            ),
        )

    settled, evaluations = settle_human_paper_intents_with_capital_controls(
        path,
        bars_by_symbol=bars_by_symbol,
        accounting_parameters=ACCOUNTING_PARAMETERS,
    )

    fills = [event for event in settled["events"] if event["kind"] == "FILL"]
    rejections = [
        event for event in settled["events"] if event["kind"] == "CAPITAL_REJECT"
    ]
    assert len(fills) == 5
    assert len(rejections) == 1
    assert rejections[0]["payload"]["symbol"] == symbols[-1]
    assert rejections[0]["payload"]["reason_codes"] == [
        "NO_FREE_VIRTUAL_STRATEGIC_SLOT"
    ]
    assert len(human_paper_position_quantities(settled["events"])) == 5
    assert len(evaluations) == 6
    assert [value["result"] for value in evaluations].count("FILL_ALLOWED") == 5
    assert [value["result"] for value in evaluations].count("CAPITAL_REJECTED") == 1


def test_production_settlement_allocates_cash_in_global_bar_time_order(
    tmp_path: Path,
) -> None:
    """Ledger insertion order must not overtake an earlier executable 1m bar."""

    path = tmp_path / "paper.json"
    alert = _alert()
    base = build_human_paper_intent(feedback=_feedback(alert), alert=alert)
    assert base is not None
    late_symbol = "SH.600001"
    early_symbol = "SH.600002"
    late = replace(
        base,
        symbol=late_symbol,
        feedback_id="sha256:" + "7" * 64,
        signal_lifecycle_id=sha256_json({"symbol": late_symbol}),
    )
    early = replace(
        base,
        symbol=early_symbol,
        feedback_id="sha256:" + "8" * 64,
        signal_lifecycle_id=sha256_json({"symbol": early_symbol}),
    )
    # Deliberately append the later market opportunity first.
    append_human_paper_intent(path, late)
    append_human_paper_intent(path, early)

    def expensive_bar(symbol: str, opened_at: datetime) -> HumanPaperMinuteBar:
        return replace(
            _bar(opened_at),
            symbol=symbol,
            open=Decimal("6000.00"),
            high=Decimal("6001.00"),
            low=Decimal("5999.00"),
            close=Decimal("6000.00"),
        )

    settled, evaluations = settle_human_paper_intents_with_capital_controls(
        path,
        bars_by_symbol={
            late_symbol: (
                expensive_bar(
                    late_symbol,
                    REVIEWED_AT + timedelta(minutes=10),
                ),
            ),
            early_symbol: (
                expensive_bar(
                    early_symbol,
                    REVIEWED_AT + timedelta(minutes=5),
                ),
            ),
        },
        accounting_parameters=ACCOUNTING_PARAMETERS,
    )

    terminal = [
        event for event in settled["events"] if event["kind"] != "INTENT"
    ]
    assert [(event["kind"], event["payload"]["symbol"]) for event in terminal] == [
        ("FILL", early_symbol),
        ("CAPITAL_REJECT", late_symbol),
    ]
    assert [value["result"] for value in evaluations] == [
        "FILL_ALLOWED",
        "CAPITAL_REJECTED",
    ]
    assert evaluations[0]["candidate_bar_opened_at"] < evaluations[1][
        "candidate_bar_opened_at"
    ]
    audit = audit_human_paper_capital_decisions(
        settled["events"],
        parameters=ACCOUNTING_PARAMETERS,
    )
    assert audit["status"] == "COMPLETE"


def test_portfolio_settlement_rejects_one_lot_above_eighteen_percent(
    tmp_path: Path,
) -> None:
    path = tmp_path / "paper.json"
    alert = _alert()
    intent = build_human_paper_intent(feedback=_feedback(alert), alert=alert)
    assert intent is not None
    append_human_paper_intent(path, intent)
    expensive = replace(
        _bar(REVIEWED_AT + timedelta(minutes=1)),
        open=Decimal("2000"),
        high=Decimal("2001"),
        low=Decimal("1999"),
        close=Decimal("2000"),
    )

    rejected, evaluations = settle_human_paper_intents_with_portfolio_controls(
        path,
        bars_by_symbol={alert.symbol: (expensive,)},
        accounting_parameters=ACCOUNTING_PARAMETERS,
    )

    assert [event["kind"] for event in rejected["events"]] == [
        "INTENT",
        "PORTFOLIO_REJECT",
    ]
    rejection = rejected["events"][-1]["payload"]
    assert rejection["reason_codes"] == [
        "VIRTUAL_ENTRY_EXCEEDS_ONE_SLOT_NOTIONAL_CAP"
    ]
    assert rejection["position_marks"] == []
    assert human_paper_portfolio_rejected_intent_ids(rejected["events"]) == {
        intent.intent_id
    }
    assert evaluations[0]["result"] == "PORTFOLIO_REJECTED"
    assert evaluations[0]["slot_fraction_notional_gate_evaluable"] is True
    decision_audit = audit_human_paper_portfolio_decisions(
        rejected["events"],
        parameters=ACCOUNTING_PARAMETERS,
    )
    assert decision_audit["status"] == "COMPLETE"
    assert decision_audit["verified_rejection_count"] == 1

    retried, retry_evaluations = settle_human_paper_intents_with_portfolio_controls(
        path,
        bars_by_symbol={alert.symbol: (expensive,)},
        accounting_parameters=ACCOUNTING_PARAMETERS,
    )
    assert retried == rejected
    assert retry_evaluations == ()


def test_portfolio_allowed_fill_atomically_records_and_audits_approval(
    tmp_path: Path,
) -> None:
    path = tmp_path / "paper.json"
    alert = _alert()
    intent = build_human_paper_intent(feedback=_feedback(alert), alert=alert)
    assert intent is not None
    append_human_paper_intent(path, intent)

    settled, evaluations = settle_human_paper_intents_with_portfolio_controls(
        path,
        bars_by_symbol={
            alert.symbol: (_bar(REVIEWED_AT + timedelta(minutes=1)),)
        },
        accounting_parameters=ACCOUNTING_PARAMETERS,
    )

    assert [event["kind"] for event in settled["events"]] == ["INTENT", "FILL"]
    fill = settled["events"][-1]["payload"]
    assert fill["side"] == "BUY"
    assert fill["portfolio_decision_sha256"] == evaluations[0][
        "content_sha256"
    ]
    assert fill["accounting_contract_id"] == (
        ACCOUNTING_PARAMETERS.accounting_contract_id
    )
    assert fill["available_cash"] == "1000000.00"
    assert fill["position_marks"] == []
    assert evaluations[0]["result"] == "FILL_ALLOWED"
    audit = audit_human_paper_portfolio_fill_decisions(
        settled["events"],
        parameters=ACCOUNTING_PARAMETERS,
    )
    assert audit["status"] == "COMPLETE"
    assert audit["approved_fill_count"] == 1
    assert audit["verified_approved_fill_count"] == 1
    assert load_human_paper_ledger(path) == settled


def test_portfolio_settlement_rejects_a_second_buy_for_the_same_symbol(
    tmp_path: Path,
) -> None:
    path = tmp_path / "paper.json"
    alert = _alert()
    first = build_human_paper_intent(feedback=_feedback(alert), alert=alert)
    assert first is not None
    append_human_paper_intent(path, first)
    settle_human_paper_intents_with_portfolio_controls(
        path,
        bars_by_symbol={
            alert.symbol: (_bar(REVIEWED_AT + timedelta(minutes=1)),)
        },
        accounting_parameters=ACCOUNTING_PARAMETERS,
    )

    next_day = REVIEWED_AT + timedelta(days=1)
    # Simulate a legacy/pre-existing pending intent.  New feedback creation is
    # already observation-only, but settlement must independently fail closed.
    duplicate = replace(
        first,
        feedback_id="sha256:" + "e" * 64,
        created_at=next_day,
        earliest_fill_at=next_day,
        signal_lifecycle_id="sha256:" + "e" * 64,
    )
    append_human_paper_intent(path, duplicate)
    candidate_and_mark = _bar(next_day + timedelta(minutes=1))

    rejected, evaluations = settle_human_paper_intents_with_portfolio_controls(
        path,
        bars_by_symbol={alert.symbol: (candidate_and_mark,)},
        accounting_parameters=ACCOUNTING_PARAMETERS,
    )

    assert rejected["events"][-1]["kind"] == "PORTFOLIO_REJECT"
    rejection = rejected["events"][-1]["payload"]
    assert rejection["reason_codes"] == [
        "VIRTUAL_SYMBOL_ALREADY_OCCUPIES_STRATEGIC_SLOT"
    ]
    assert rejection["position_marks"] == [
        {
            "symbol": alert.symbol,
            "quantity": 100,
                "price": "10.15",
                "market_value": "1015.00",
        }
    ]
    assert evaluations[0]["result"] == "PORTFOLIO_REJECTED"
    assert audit_human_paper_portfolio_decisions(
        rejected["events"],
        parameters=ACCOUNTING_PARAMETERS,
    )["status"] == "COMPLETE"


def test_legacy_fill_remains_readable_and_is_not_claimed_as_v2_approval(
    tmp_path: Path,
) -> None:
    path = tmp_path / "paper.json"
    alert = _alert()
    intent = build_human_paper_intent(feedback=_feedback(alert), alert=alert)
    assert intent is not None
    append_human_paper_intent(path, intent)

    settled = settle_human_paper_intents(
        path,
        bars_by_symbol={
            alert.symbol: (_bar(REVIEWED_AT + timedelta(minutes=1)),)
        },
    )

    fill = settled["events"][-1]["payload"]
    assert "portfolio_decision_sha256" not in fill
    assert load_human_paper_ledger(path) == settled
    audit = audit_human_paper_portfolio_fill_decisions(
        settled["events"],
        parameters=ACCOUNTING_PARAMETERS,
    )
    assert audit["status"] == "NO_APPROVED_FILLS"
    assert audit["approved_fill_count"] == 0


def test_portfolio_fill_decision_audit_rejects_embedded_cash_tampering(
    tmp_path: Path,
) -> None:
    path = tmp_path / "paper.json"
    alert = _alert()
    intent = build_human_paper_intent(feedback=_feedback(alert), alert=alert)
    assert intent is not None
    append_human_paper_intent(path, intent)
    settled, _ = settle_human_paper_intents_with_portfolio_controls(
        path,
        bars_by_symbol={
            alert.symbol: (_bar(REVIEWED_AT + timedelta(minutes=1)),)
        },
        accounting_parameters=ACCOUNTING_PARAMETERS,
    )
    tampered = json.loads(json.dumps(settled["events"]))
    tampered[-1]["payload"]["available_cash"] = "999999.99"

    audit = audit_human_paper_portfolio_fill_decisions(
        tampered,
        parameters=ACCOUNTING_PARAMETERS,
    )
    assert audit["status"] == "INVALID"
    assert audit["verified_approved_fill_count"] == 0


def test_ledger_loader_rejects_locally_rehashed_invalid_portfolio_fill(
    tmp_path: Path,
) -> None:
    path = tmp_path / "paper.json"
    alert = _alert()
    intent = build_human_paper_intent(feedback=_feedback(alert), alert=alert)
    assert intent is not None
    append_human_paper_intent(path, intent)
    settle_human_paper_intents_with_portfolio_controls(
        path,
        bars_by_symbol={
            alert.symbol: (_bar(REVIEWED_AT + timedelta(minutes=1)),)
        },
        accounting_parameters=ACCOUNTING_PARAMETERS,
    )
    document = json.loads(path.read_text(encoding="utf-8"))
    fill_event = document["events"][-1]
    fill_payload = fill_event["payload"]
    fill_payload["available_cash"] = "999999.99"
    fill_stable = dict(fill_payload)
    fill_stable.pop("fill_id")
    fill_payload["fill_id"] = sha256_json(fill_stable)
    event_stable = dict(fill_event)
    event_stable.pop("event_id")
    fill_event["event_id"] = sha256_json(event_stable)
    document_stable = dict(document)
    document_stable.pop("content_sha256")
    document["content_sha256"] = sha256_json(document_stable)
    path.write_text(json.dumps(document), encoding="utf-8")

    with pytest.raises(
        ValueError,
        match="human paper fill payload is invalid",
    ):
        load_human_paper_ledger(path)


def test_portfolio_settlement_defers_buy_until_all_position_marks_resolve(
    tmp_path: Path,
) -> None:
    path = tmp_path / "paper.json"
    base_alert = _alert()
    first = build_human_paper_intent(
        feedback=_feedback(base_alert),
        alert=base_alert,
    )
    assert first is not None
    append_human_paper_intent(path, first)
    first_bar = _bar(REVIEWED_AT + timedelta(minutes=1))
    settled, _ = settle_human_paper_intents_with_portfolio_controls(
        path,
        bars_by_symbol={base_alert.symbol: (first_bar,)},
        accounting_parameters=ACCOUNTING_PARAMETERS,
    )
    assert human_paper_position_quantities(settled["events"]) == {
        base_alert.symbol: 100
    }

    second_time = REVIEWED_AT + timedelta(days=1)
    second_symbol = "SH.600001"
    second = replace(
        first,
        symbol=second_symbol,
        feedback_id="sha256:" + "9" * 64,
        created_at=second_time,
        earliest_fill_at=second_time,
        signal_lifecycle_id=sha256_json({"symbol": second_symbol}),
    )
    append_human_paper_intent(path, second)
    candidate = replace(
        _bar(second_time + timedelta(minutes=1)),
        symbol=second_symbol,
    )

    deferred, evaluations = settle_human_paper_intents_with_portfolio_controls(
        path,
        bars_by_symbol={second_symbol: (candidate,)},
        accounting_parameters=ACCOUNTING_PARAMETERS,
    )
    assert [event["kind"] for event in deferred["events"]] == [
        "INTENT",
        "FILL",
        "INTENT",
    ]
    assert evaluations[0]["result"] == "PORTFOLIO_MARKS_UNRESOLVED"
    assert evaluations[0]["unresolved_position_marks"] == [
        {
            "symbol": base_alert.symbol,
            "reason": "EXACT_SYNCHRONOUS_1M_BAR_NOT_UNIQUE",
        }
    ]
    assert second.intent_id not in human_paper_portfolio_rejected_intent_ids(
        deferred["events"]
    )

    repeated, repeated_evaluations = (
        settle_human_paper_intents_with_portfolio_controls(
            path,
            bars_by_symbol={second_symbol: (candidate,)},
            accounting_parameters=ACCOUNTING_PARAMETERS,
        )
    )
    assert repeated == deferred
    assert repeated_evaluations[0]["result"] == "PORTFOLIO_MARKS_UNRESOLVED"

    position_mark = replace(
        _bar(candidate.opened_at),
        symbol=base_alert.symbol,
    )
    filled, resolved = settle_human_paper_intents_with_portfolio_controls(
        path,
        bars_by_symbol={
            base_alert.symbol: (position_mark,),
            second_symbol: (candidate,),
        },
        accounting_parameters=ACCOUNTING_PARAMETERS,
    )
    assert [event["kind"] for event in filled["events"]] == [
        "INTENT",
        "FILL",
        "INTENT",
        "FILL",
    ]
    assert resolved[0]["result"] == "FILL_ALLOWED"
    assert resolved[0]["position_marks"] == [
        {
            "symbol": base_alert.symbol,
            "quantity": 100,
                "price": "10.15",
                "market_value": "1015.00",
        }
    ]


def test_unresolved_buy_marks_do_not_block_later_persistent_exit(
    tmp_path: Path,
) -> None:
    path = tmp_path / "paper.json"
    alert = _alert()
    first = build_human_paper_intent(feedback=_feedback(alert), alert=alert)
    assert first is not None
    append_human_paper_intent(path, first)
    settle_human_paper_intents_with_portfolio_controls(
        path,
        bars_by_symbol={
            alert.symbol: (_bar(REVIEWED_AT + timedelta(minutes=1)),)
        },
        accounting_parameters=ACCOUNTING_PARAMETERS,
    )

    next_day = REVIEWED_AT + timedelta(days=1)
    earlier = replace(
        first,
        feedback_id="sha256:" + "a" * 64,
        symbol="SH.600001",
        created_at=next_day,
        earliest_fill_at=next_day,
        signal_lifecycle_id=sha256_json({"symbol": "SH.600001"}),
    )
    persistent_exit = replace(
        first,
        feedback_id="sha256:" + "b" * 64,
        side="SELL",
        created_at=next_day,
        earliest_fill_at=next_day,
        signal_lifecycle_id=sha256_json({"symbol": alert.symbol, "side": "SELL"}),
        entry_confirmation_bar_closed_at=None,
        entry_price_cap=None,
        entry_valid_until=None,
        entry_boundary_evidence_id=None,
    )
    append_human_paper_intent(path, earlier)
    append_human_paper_intent(path, persistent_exit)
    earlier_bar = replace(
        _bar(next_day + timedelta(minutes=1)),
        symbol=earlier.symbol,
    )
    exit_bar = replace(
        _bar(next_day + timedelta(minutes=2)),
        symbol=persistent_exit.symbol,
    )
    # The existing position has no synchronous mark at the optional BUY's
    # earlier candidate, but its later persistent exit has complete facts.

    document, evaluations = settle_human_paper_intents_with_portfolio_controls(
        path,
        bars_by_symbol={
            alert.symbol: (exit_bar,),
            earlier.symbol: (earlier_bar,),
        },
        accounting_parameters=ACCOUNTING_PARAMETERS,
    )

    assert evaluations[0]["result"] == "PORTFOLIO_MARKS_UNRESOLVED"
    assert evaluations[0][
        "optional_buy_deferred_for_unresolved_marks"
    ] is True
    assert evaluations[0]["persistent_exit_processing_continues"] is True
    terminal_sides = [
        event["payload"]["side"]
        for event in document["events"]
        if event["kind"] == "FILL"
    ]
    assert terminal_sides == ["BUY", "SELL"]


def test_entry_provenance_blocked_buy_does_not_block_persistent_exit(
    tmp_path: Path,
) -> None:
    path = tmp_path / "paper.json"
    alert = _alert()
    first = build_human_paper_intent(feedback=_feedback(alert), alert=alert)
    assert first is not None
    append_human_paper_intent(path, first)
    settle_human_paper_intents_with_portfolio_controls(
        path,
        bars_by_symbol={
            alert.symbol: (_bar(REVIEWED_AT + timedelta(minutes=1)),)
        },
        accounting_parameters=ACCOUNTING_PARAMETERS,
    )

    next_day = REVIEWED_AT + timedelta(days=1)
    blocked_buy = replace(
        first,
        feedback_id="sha256:" + "a" * 64,
        symbol="SH.600001",
        created_at=next_day,
        earliest_fill_at=next_day,
        signal_lifecycle_id=sha256_json(
            {"symbol": "SH.600001", "side": "BUY"}
        ),
    )
    persistent_exit = replace(
        first,
        feedback_id="sha256:" + "b" * 64,
        side="SELL",
        created_at=next_day,
        earliest_fill_at=next_day,
        signal_lifecycle_id=sha256_json(
            {"symbol": alert.symbol, "side": "SELL"}
        ),
        entry_confirmation_bar_closed_at=None,
        entry_price_cap=None,
        entry_valid_until=None,
        entry_boundary_evidence_id=None,
    )
    append_human_paper_intent(path, blocked_buy)
    append_human_paper_intent(path, persistent_exit)

    blocked_bar = replace(
        _bar(next_day + timedelta(minutes=1)),
        symbol=blocked_buy.symbol,
    )
    exit_bar = _bar(next_day + timedelta(minutes=2))
    document, evaluations = settle_human_paper_intents_with_portfolio_controls(
        path,
        bars_by_symbol={
            blocked_buy.symbol: (blocked_bar,),
            persistent_exit.symbol: (exit_bar,),
        },
        accounting_parameters=ACCOUNTING_PARAMETERS,
        entry_provenance_blocked_intent_ids=(blocked_buy.intent_id,),
    )

    assert evaluations == ()
    fills = [
        event["payload"]
        for event in document["events"]
        if event["kind"] == "FILL"
    ]
    assert [(value["symbol"], value["side"]) for value in fills] == [
        (alert.symbol, "BUY"),
        (alert.symbol, "SELL"),
    ]
    assert blocked_buy.intent_id not in human_paper_terminal_intent_ids(
        document["events"]
    )

    with pytest.raises(ValueError, match="pending BUY"):
        settle_human_paper_intents_with_portfolio_controls(
            path,
            bars_by_symbol={},
            accounting_parameters=ACCOUNTING_PARAMETERS,
            entry_provenance_blocked_intent_ids=(persistent_exit.intent_id,),
        )


def test_portfolio_decision_audit_rejects_prefix_cash_tampering(
    tmp_path: Path,
) -> None:
    path = tmp_path / "paper.json"
    alert = _alert()
    intent = build_human_paper_intent(feedback=_feedback(alert), alert=alert)
    assert intent is not None
    append_human_paper_intent(path, intent)
    expensive = replace(
        _bar(REVIEWED_AT + timedelta(minutes=1)),
        open=Decimal("2000"),
        high=Decimal("2001"),
        low=Decimal("1999"),
        close=Decimal("2000"),
    )
    rejected, _ = settle_human_paper_intents_with_portfolio_controls(
        path,
        bars_by_symbol={alert.symbol: (expensive,)},
        accounting_parameters=ACCOUNTING_PARAMETERS,
    )
    tampered = json.loads(json.dumps(rejected["events"]))
    tampered[-1]["payload"]["available_cash"] = "999999.99"

    audit = audit_human_paper_portfolio_decisions(
        tampered,
        parameters=ACCOUNTING_PARAMETERS,
    )
    assert audit["status"] == "INVALID"
    assert audit["verified_rejection_count"] == 0


def test_cancelling_pending_sell_releases_virtual_position_reservation(
    tmp_path: Path,
) -> None:
    path = tmp_path / "paper.json"
    buy_alert = _alert()
    reconcile_human_paper_feedback(
        path,
        feedback=_feedback(buy_alert),
        alert=buy_alert,
    )
    bought = settle_human_paper_intents(
        path,
        bars_by_symbol={
            buy_alert.symbol: (_bar(REVIEWED_AT + timedelta(minutes=1)),)
        },
    )
    assert human_paper_position_quantities(bought["events"]) == {
        buy_alert.symbol: 100
    }

    sell_alert = _alert(alert_type="POSSIBLE_30M_EXIT")
    sell_feedback = _sell_feedback(sell_alert)
    pending, sell_event, cancellations, changed = reconcile_human_paper_feedback(
        path,
        feedback=sell_feedback,
        alert=sell_alert,
    )
    assert changed is True
    assert sell_event is not None
    assert cancellations == ()
    assert sell_event["payload"]["side"] == "SELL"
    assert sell_event["payload"]["status"] == "PENDING"
    assert human_paper_pending_sell_quantities(pending["events"]) == {
        buy_alert.symbol: 100
    }

    later_watch = replace(
        sell_feedback,
        reviewed_at=sell_feedback.reviewed_at + timedelta(minutes=5),
        disposition="WATCH",
        notes="卖点尚未确认，释放虚拟卖出预留量",
    )
    cancelled, replacement, cancellations, changed = (
        reconcile_human_paper_feedback(
            path,
            feedback=later_watch,
            alert=sell_alert,
        )
    )
    assert changed is True
    assert replacement is None
    assert len(cancellations) == 1
    assert human_paper_pending_sell_quantities(cancelled["events"]) == {}
    assert human_paper_position_quantities(cancelled["events"]) == {
        buy_alert.symbol: 100
    }

    after_bar = settle_human_paper_intents(
        path,
        bars_by_symbol={
            buy_alert.symbol: (_bar(REVIEWED_AT + timedelta(days=1)),)
        },
    )
    assert [event["kind"] for event in after_bar["events"]].count("FILL") == 1
    assert human_paper_position_quantities(after_bar["events"]) == {
        buy_alert.symbol: 100
    }


def test_feedback_for_other_signal_lifecycle_does_not_cancel_pending_intent(
    tmp_path: Path,
) -> None:
    path = tmp_path / "paper.json"
    first_alert = _alert()
    first, event, _cancellations, _changed = reconcile_human_paper_feedback(
        path,
        feedback=_feedback(first_alert),
        alert=first_alert,
    )
    assert event is not None
    first_intent_id = str(event["payload"]["intent_id"])

    other_alert = replace(
        first_alert,
        source_point_id="sha256:" + "7" * 64,
        signal_at=first_alert.signal_at + timedelta(minutes=1),
    )
    other_feedback = replace(
        _feedback(other_alert),
        reviewed_at=REVIEWED_AT + timedelta(minutes=5),
        disposition="WATCH",
    )
    unchanged, replacement, cancellations, changed = reconcile_human_paper_feedback(
        path,
        feedback=other_feedback,
        alert=other_alert,
    )

    assert first_alert.signal_lifecycle_id != other_alert.signal_lifecycle_id
    assert replacement is None
    assert cancellations == ()
    assert changed is False
    assert unchanged == first
    assert first_intent_id not in {
        event["payload"]["intent_id"]
        for event in unchanged["events"]
        if event["kind"] == "CANCEL"
    }


def test_backdated_supersession_cannot_cancel_a_newer_pending_intent(
    tmp_path: Path,
) -> None:
    path = tmp_path / "paper.json"
    alert = _alert()
    first_feedback = replace(
        _feedback(alert),
        reviewed_at=REVIEWED_AT + timedelta(minutes=5),
    )
    before, event, _cancellations, _changed = reconcile_human_paper_feedback(
        path,
        feedback=first_feedback,
        alert=alert,
    )
    assert event is not None
    backdated = replace(
        first_feedback,
        reviewed_at=REVIEWED_AT + timedelta(minutes=4),
        disposition="WATCH",
    )

    with pytest.raises(ValueError, match="predates a pending virtual intent"):
        reconcile_human_paper_feedback(path, feedback=backdated, alert=alert)

    assert load_human_paper_ledger(path) == before


def test_paper_intent_rejects_backdated_human_feedback() -> None:
    alert = _alert()
    feedback = replace(
        _feedback(alert),
        reviewed_at=alert.review_available_at - timedelta(seconds=1),
    )

    with pytest.raises(ValueError, match="predates available evidence"):
        build_human_paper_intent(feedback=feedback, alert=alert)


def test_non_green_risk_gate_never_creates_a_virtual_fill(tmp_path: Path) -> None:
    path = tmp_path / "paper.json"
    alert = _alert(market_gate="AMBER")
    intent = build_human_paper_intent(feedback=_feedback(alert), alert=alert)
    assert intent is not None
    assert intent.status == "BLOCKED_BY_RISK_GATE"
    append_human_paper_intent(path, intent)

    settled = settle_human_paper_intents(
        path,
        bars_by_symbol={alert.symbol: (_bar(REVIEWED_AT + timedelta(minutes=1)),)},
    )
    assert [event["kind"] for event in settled["events"]] == ["INTENT"]


def test_non_green_sector_gate_blocks_virtual_buy_intent() -> None:
    alert = _alert(sector_gate="UNRESOLVED")
    intent = build_human_paper_intent(feedback=_feedback(alert), alert=alert)

    assert intent is not None
    assert intent.status == "BLOCKED_BY_RISK_GATE"
    assert "SECTOR_GATE_UNRESOLVED" in intent.reason_codes


@pytest.mark.parametrize(
    ("missing_field", "bar_kwargs"),
    (
        ("security_status", {"security_status_complete": False}),
        ("corporate_action", {"corporate_action_state_complete": False}),
    ),
)
def test_virtual_fill_fails_closed_when_execution_fact_is_missing(
    tmp_path: Path,
    missing_field: str,
    bar_kwargs: dict[str, bool],
) -> None:
    path = tmp_path / f"paper-{missing_field}.json"
    alert = _alert()
    intent = build_human_paper_intent(feedback=_feedback(alert), alert=alert)
    assert intent is not None
    append_human_paper_intent(path, intent)

    settled = settle_human_paper_intents(
        path,
        bars_by_symbol={
            alert.symbol: (_bar(REVIEWED_AT + timedelta(minutes=4), **bar_kwargs),)
        },
    )

    assert [event["kind"] for event in settled["events"]] == ["INTENT"]


def test_virtual_sell_requires_position_and_obeys_t_plus_one(tmp_path: Path) -> None:
    path = tmp_path / "paper.json"
    alert = _alert()
    buy = build_human_paper_intent(feedback=_feedback(alert), alert=alert)
    assert buy is not None
    append_human_paper_intent(path, buy)
    bought = settle_human_paper_intents(
        path,
        bars_by_symbol={alert.symbol: (_bar(REVIEWED_AT + timedelta(minutes=4)),)},
    )
    positions = human_paper_position_quantities(bought["events"])
    assert positions == {alert.symbol: 100}

    sell_alert = _alert(alert_type="POSSIBLE_30M_EXIT")
    sell = build_human_paper_intent(
        feedback=_sell_feedback(sell_alert),
        alert=sell_alert,
        virtual_position_quantity=positions[alert.symbol],
    )
    assert sell is not None and sell.status == "PENDING" and sell.side == "SELL"
    append_human_paper_intent(path, sell)
    pending_document = load_human_paper_ledger(path)
    reserved = human_paper_pending_sell_quantities(pending_document["events"])
    assert reserved == {alert.symbol: 100}
    duplicate_review = build_human_paper_intent(
        feedback=replace(
            _sell_feedback(sell_alert),
            reviewed_at=REVIEWED_AT + timedelta(hours=1, minutes=1),
        ),
        alert=sell_alert,
        virtual_position_quantity=max(0, positions[alert.symbol] - reserved[alert.symbol]),
    )
    assert duplicate_review is not None
    assert duplicate_review.status == "OBSERVATION_ONLY"

    same_day = _bar(REVIEWED_AT + timedelta(hours=3))
    next_day = _bar(REVIEWED_AT + timedelta(days=1, minutes=1))
    settled = settle_human_paper_intents(
        path,
        bars_by_symbol={alert.symbol: (same_day, next_day)},
    )
    fills = [event["payload"] for event in settled["events"] if event["kind"] == "FILL"]
    assert [fill["side"] for fill in fills] == ["BUY", "SELL"]
    assert fills[-1]["filled_at"] == next_day.closed_at.isoformat()
    assert human_paper_position_quantities(settled["events"]) == {}
    assert human_paper_pending_sell_quantities(settled["events"]) == {}


def test_oldest_open_lot_resets_after_closed_cycle_and_new_buy(
    tmp_path: Path,
) -> None:
    """Company-action provenance follows the oldest remaining FIFO lot.

    An action that happened during a fully closed strategic cycle must not
    contaminate a later cycle in the same symbol.
    """

    path = tmp_path / "paper.json"
    alert = _alert()
    first_buy = build_human_paper_intent(feedback=_feedback(alert), alert=alert)
    assert first_buy is not None
    append_human_paper_intent(path, first_buy)
    first_fill_at = REVIEWED_AT + timedelta(minutes=4)
    bought = settle_human_paper_intents(
        path,
        bars_by_symbol={alert.symbol: (_bar(first_fill_at),)},
    )
    assert human_paper_oldest_open_lot_sessions(bought["events"]) == {
        alert.symbol: first_fill_at.date()
    }

    sell_alert = _alert(alert_type="POSSIBLE_30M_EXIT")
    sell = build_human_paper_intent(
        feedback=_sell_feedback(sell_alert),
        alert=sell_alert,
        virtual_position_quantity=100,
    )
    assert sell is not None
    append_human_paper_intent(path, sell)
    closed = settle_human_paper_intents(
        path,
        bars_by_symbol={
            alert.symbol: (_bar(REVIEWED_AT + timedelta(days=1, minutes=1)),)
        },
    )
    assert human_paper_oldest_open_lot_sessions(closed["events"]) == {}

    second_session = REVIEWED_AT + timedelta(days=2)
    second_buy = replace(
        first_buy,
        feedback_id="sha256:" + "c" * 64,
        candidate_id="sha256:" + "d" * 64,
        signal_lifecycle_id="sha256:" + "e" * 64,
        created_at=second_session,
        earliest_fill_at=second_session,
        entry_confirmation_bar_closed_at=second_session,
        entry_valid_until=second_session + timedelta(days=1),
        entry_boundary_evidence_id="sha256:" + "f" * 64,
    )
    append_human_paper_intent(path, second_buy)
    second_fill_at = second_session + timedelta(minutes=1)
    reopened = settle_human_paper_intents(
        path,
        bars_by_symbol={alert.symbol: (_bar(second_fill_at),)},
    )

    assert human_paper_position_quantities(reopened["events"]) == {
        alert.symbol: 100
    }
    assert human_paper_oldest_open_lot_sessions(reopened["events"]) == {
        alert.symbol: second_fill_at.date()
    }


def test_human_paper_accounting_uses_frozen_minimum_fee_stamp_and_fifo(
    tmp_path: Path,
) -> None:
    """虚拟账本复用冻结费率；已实现盈亏不是伪造的逐日组合绩效。"""

    path = tmp_path / "paper.json"
    alert = _alert()
    buy = build_human_paper_intent(feedback=_feedback(alert), alert=alert)
    assert buy is not None
    append_human_paper_intent(path, buy)
    bought = settle_human_paper_intents(
        path,
        bars_by_symbol={alert.symbol: (_bar(REVIEWED_AT + timedelta(minutes=4)),)},
    )
    buy_accounting = rebuild_human_paper_accounting(
        bought["events"],
        parameters=load_human_paper_accounting_parameters(PARAMETER_SNAPSHOT),
        execution_evidence_status="COMPLETE",
    )
    assert buy_accounting["status"] == "OPEN_POSITIONS_UNMARKED"
    assert buy_accounting["cash_balance"] == "998974.99"
    assert buy_accounting["total_fees"] == "5.01"
    assert buy_accounting["remaining_cost_basis"] == "1025.01"
    assert buy_accounting["positions"][alert.symbol] == {
        "quantity": 100,
        "remaining_cost_basis": "1025.01",
        "average_cost": "10.2501",
        "oldest_acquired_session": "2026-07-28",
    }
    assert buy_accounting["cash_ledger_complete"] is False
    assert buy_accounting["performance_evaluable"] is False

    sell_alert = _alert(alert_type="POSSIBLE_30M_EXIT")
    sell = build_human_paper_intent(
        feedback=_sell_feedback(sell_alert),
        alert=sell_alert,
        virtual_position_quantity=100,
    )
    assert sell is not None
    append_human_paper_intent(path, sell)
    sell_bar = replace(
        _bar(REVIEWED_AT + timedelta(days=1, minutes=1)),
        open=Decimal("11.00"),
        high=Decimal("11.10"),
        low=Decimal("10.90"),
        close=Decimal("11.05"),
    )
    closed = settle_human_paper_intents(
        path,
        bars_by_symbol={alert.symbol: (sell_bar,)},
    )
    accounting = rebuild_human_paper_accounting(
        closed["events"],
        parameters=load_human_paper_accounting_parameters(PARAMETER_SNAPSHOT),
        execution_evidence_status="COMPLETE",
    )
    # 买入按整柱最高价10.20，卖出按整柱最低价10.90；费用仍复用冻结费率。
    assert accounting["status"] == "CLOSED_BOOK_NO_DAILY_EQUITY"
    assert accounting["cash_balance"] == "1000059.43"
    assert accounting["total_fees"] == "10.57"
    assert accounting["turnover_notional"] == "2110.00"
    assert accounting["realized_pnl"] == "59.43"
    assert accounting["closed_cycle_count"] == 1
    assert accounting["positions"] == {}
    assert accounting["cash_ledger_complete"] is True
    assert accounting["equity_curve_available"] is False
    assert accounting["performance_evaluable"] is False


def test_human_paper_accounting_no_fill_and_unverified_evidence_are_explicit(
    tmp_path: Path,
) -> None:
    parameters = load_human_paper_accounting_parameters(PARAMETER_SNAPSHOT)
    empty = rebuild_human_paper_accounting(
        (),
        parameters=parameters,
        execution_evidence_status="NO_FILLS",
    )
    assert empty["status"] == "NO_FILLS"
    assert empty["accounting_valid"] is True
    assert empty["cash_balance"] == "1000000.00"
    assert empty["fee_model_attached"] is True
    assert empty["cash_ledger_attached"] is True
    assert empty["performance_evaluable"] is False

    path = tmp_path / "paper.json"
    alert = _alert()
    intent = build_human_paper_intent(feedback=_feedback(alert), alert=alert)
    assert intent is not None
    append_human_paper_intent(path, intent)
    filled = settle_human_paper_intents(
        path,
        bars_by_symbol={alert.symbol: (_bar(REVIEWED_AT + timedelta(minutes=4)),)},
    )
    unverified = rebuild_human_paper_accounting(
        filled["events"],
        parameters=parameters,
        execution_evidence_status="MISSING",
    )
    assert unverified["status"] == "EXECUTION_EVIDENCE_UNVERIFIED"
    assert unverified["accounting_valid"] is False
    assert "EXECUTION_EVIDENCE_NOT_COMPLETE" in unverified["reason_codes"]


def test_human_paper_accounting_rejects_fee_snapshot_mutation(tmp_path: Path) -> None:
    payload = json.loads(PARAMETER_SNAPSHOT.read_text(encoding="utf-8"))
    payload["fee_schedule"]["commission_rate"] = "0.0002"
    changed = tmp_path / "changed-parameters.json"
    changed.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    with pytest.raises(ValueError, match="fee schedule changed"):
        load_human_paper_accounting_parameters(changed)


def _write_continuity_evidence(
    *,
    forward_root: Path,
    session: datetime,
    intent_id: str,
    symbol: str,
    omit_close: time | None = None,
    eligible: bool = True,
    executable: bool = True,
    virtual_position_quantity: int = 0,
    oldest_virtual_acquired_session: date | None = None,
) -> tuple[Path, Path]:
    session_root = forward_root / "sessions" / session.date().isoformat()
    captured_at = session.replace(hour=15, minute=20).isoformat()
    facts_stable = {
        "schema": "chanlun-human-paper-execution-facts/v1",
        "session": session.date().isoformat(),
        "captured_at": captured_at,
        "symbols": [
            {
                "symbol": symbol,
                "native_code": "600000.SH",
                "session": session.date().isoformat(),
                "trading_day": session.date().isoformat(),
                "instrument_name": "test",
                "instrument_status": 0 if eligible else 1,
                "is_trading": False,
                "suspended": not eligible,
                "expired": False,
                "expiry_date": None,
                "is_st": False,
                "pre_close": "10",
                "limit_up": "11",
                "limit_down": "9",
                "price_tick": "0.01",
                "corporate_actions": [],
                "source_methods": [
                    "QMT_GET_INSTRUMENT_DETAIL",
                    "QMT_GET_DIVID_FACTORS",
                ],
                "tick_data_used": False,
                "account_api_used": False,
                "factor_start": (
                    session.date()
                    if oldest_virtual_acquired_session is None
                    else oldest_virtual_acquired_session
                ).isoformat(),
                "virtual_position_quantity": virtual_position_quantity,
                "oldest_virtual_acquired_session": (
                    None
                    if oldest_virtual_acquired_session is None
                    else oldest_virtual_acquired_session.isoformat()
                ),
                "position_corporate_action_conflict": False,
                "buy_eligible": eligible,
                "sell_eligible": eligible,
                "security_status_complete": True,
                "corporate_action_state_complete": True,
            }
        ],
        "errors": [],
        "requested_symbol_count": 1,
        "complete_symbol_count": 1,
        "all_complete": True,
        "source": "QMT_READ_ONLY_INSTRUMENT_DETAIL_AND_DIVID_FACTORS",
        "minimum_market_data_frequency": "1m",
        "tick_data_used": False,
        "account_api_used": False,
        "broker_transport_available": False,
        "live_status": "LIVE_DISABLED",
    }
    fact_id = sha256_json(facts_stable)
    fact_path = (
        session_root
        / "objects"
        / "paper_execution_facts"
        / f"{fact_id[7:]}.json"
    )
    fact_path.parent.mkdir(parents=True, exist_ok=True)
    fact_path.write_text(
        json.dumps({**facts_stable, "content_sha256": fact_id}),
        encoding="utf-8",
    )
    completed_closes = (
        *tuple(
            session.replace(hour=9, minute=31) + timedelta(minutes=index)
            for index in range(120)
        ),
        *tuple(
            session.replace(hour=13, minute=1) + timedelta(minutes=index)
            for index in range(120)
        ),
    )
    bars = [
        {
            "symbol": symbol,
            "opened_at": (close_at - timedelta(minutes=1)).isoformat(),
            "closed_at": close_at.isoformat(),
            "open": "10",
            "high": "10.1",
            "low": "9.9",
            "close": "10",
            "volume": "2000" if executable else "0",
            "complete": True,
            "suspended": False,
            "limit_up_locked": False,
            "limit_down_locked": False,
            "buy_eligible": True,
            "sell_eligible": True,
            "security_status_complete": True,
            "corporate_action_state_complete": True,
        }
        for close_at in completed_closes
        if eligible and (omit_close is None or close_at.time() != omit_close)
    ]
    grid_status = (
        "COMPLETE" if eligible else "NOT_REQUIRED_INSTRUMENT_INELIGIBLE"
    )
    evidence_stable = {
        "schema": "chanlun-human-paper-execution-evidence/v1",
        "session": session.date().isoformat(),
        "captured_at": captured_at,
        "execution_fact_snapshot_sha256": fact_id,
        "pending_intent_ids": [intent_id],
        "bars_by_symbol": {symbol: bars},
        "bar_grid_audits": [
            {
                "symbol": symbol,
                "status": grid_status,
                "native_row_count": len(bars),
                "normalized_row_count": len(bars),
                "complete_sessions": (
                    [session.date().isoformat()] if eligible else []
                ),
                "session_issues": [],
                "source_base_stream_revision": (
                    "sha256:" + "9" * 64 if eligible else None
                ),
            }
        ],
        "all_required_bar_grids_complete": True,
        "fill_model": "ADVERSE_OBSERVED_BAR_EXTREME_WITHIN_LIMIT",
        "fill_timestamp_rule": "COMPLETED_BAR_CLOSE",
        "buy_strict_cross_rule": "ENTIRE_BAR_RANGE_STRICTLY_THROUGH_LIMIT",
        "buy_max_bar_volume_participation": "0.05",
        "minimum_market_data_frequency": "1m",
        "tick_data_used": False,
        "account_api_used": False,
        "broker_transport_available": False,
        "live_status": "LIVE_DISABLED",
    }
    evidence_id = sha256_json(evidence_stable)
    evidence_path = (
        session_root
        / "objects"
        / "paper_execution_evidence"
        / f"{evidence_id[7:]}.json"
    )
    evidence_path.parent.mkdir(parents=True, exist_ok=True)
    evidence_path.write_text(
        json.dumps({**evidence_stable, "content_sha256": evidence_id}),
        encoding="utf-8",
    )
    return fact_path, evidence_path


def test_pending_continuity_rejects_internal_minute_gap_even_with_close(
    tmp_path: Path,
) -> None:
    path = tmp_path / "paper.json"
    alert = _alert()
    intent = build_human_paper_intent(feedback=_feedback(alert), alert=alert)
    assert intent is not None
    document, _event = append_human_paper_intent(path, intent)
    forward_root = tmp_path / "forward"
    _write_continuity_evidence(
        forward_root=forward_root,
        session=REVIEWED_AT,
        intent_id=intent.intent_id,
        symbol=alert.symbol,
        omit_close=time(10, 0),
    )

    audit = audit_human_paper_pending_continuity(
        document["events"],
        forward_root=forward_root,
        current_session=(REVIEWED_AT + timedelta(days=1)).date(),
        trading_sessions=(REVIEWED_AT.date(),),
    )

    assert audit["status"] == "CAUSAL_GAPS"
    assert audit["covered_intent_session_count"] == 0
    assert audit["gaps"][0]["reason"] == "FULL_SESSION_EXECUTION_EVIDENCE_MISSING"


def test_pending_continuity_requires_terminal_cancel_for_ineligible_buy(
    tmp_path: Path,
) -> None:
    path = tmp_path / "paper.json"
    alert = _alert()
    intent = build_human_paper_intent(feedback=_feedback(alert), alert=alert)
    assert intent is not None
    document, _event = append_human_paper_intent(path, intent)
    forward_root = tmp_path / "forward"
    _write_continuity_evidence(
        forward_root=forward_root,
        session=REVIEWED_AT,
        intent_id=intent.intent_id,
        symbol=alert.symbol,
        eligible=False,
    )

    audit = audit_human_paper_pending_continuity(
        document["events"],
        forward_root=forward_root,
        current_session=(REVIEWED_AT + timedelta(days=1)).date(),
        trading_sessions=(REVIEWED_AT.date(),),
    )

    assert audit["status"] == "CAUSAL_GAPS"
    assert audit["covered_intent_session_count"] == 0
    assert audit["gap_intent_count"] == 1
    assert audit["gaps"][0]["reason"] == (
        "TERMINAL_OUTCOME_MISSING_FROM_LEDGER"
    )


def test_pending_continuity_rejects_executable_buy_without_terminal_event(
    tmp_path: Path,
) -> None:
    path = tmp_path / "paper.json"
    alert = _alert()
    intent = build_human_paper_intent(feedback=_feedback(alert), alert=alert)
    assert intent is not None
    document, _event = append_human_paper_intent(path, intent)
    forward_root = tmp_path / "forward"
    _write_continuity_evidence(
        forward_root=forward_root,
        session=REVIEWED_AT,
        intent_id=intent.intent_id,
        symbol=alert.symbol,
    )

    audit = audit_human_paper_pending_continuity(
        document["events"],
        forward_root=forward_root,
        current_session=(REVIEWED_AT + timedelta(days=1)).date(),
        trading_sessions=(REVIEWED_AT.date(),),
    )

    assert audit["status"] == "CAUSAL_GAPS"
    assert audit["covered_intent_session_count"] == 0
    assert audit["gaps"][0]["reason"] == (
        "TERMINAL_OUTCOME_MISSING_FROM_LEDGER"
    )


def test_pending_continuity_treats_zero_volume_bar_above_cap_as_terminal(
    tmp_path: Path,
) -> None:
    """A proven no-chase boundary may not be hidden by missing capacity."""

    path = tmp_path / "paper.json"
    alert = replace(_alert(), entry_price_cap=Decimal("9.50"))
    intent = build_human_paper_intent(feedback=_feedback(alert), alert=alert)
    assert intent is not None
    document, _event = append_human_paper_intent(path, intent)
    forward_root = tmp_path / "forward"
    _write_continuity_evidence(
        forward_root=forward_root,
        session=REVIEWED_AT,
        intent_id=intent.intent_id,
        symbol=alert.symbol,
        executable=False,
    )

    audit = audit_human_paper_pending_continuity(
        document["events"],
        forward_root=forward_root,
        current_session=(REVIEWED_AT + timedelta(days=1)).date(),
        trading_sessions=(REVIEWED_AT.date(),),
    )

    assert audit["status"] == "CAUSAL_GAPS"
    assert audit["gaps"][0]["reason"] == (
        "TERMINAL_OUTCOME_MISSING_FROM_LEDGER"
    )


def test_pending_buy_continuity_allows_unresolved_open_position_mark(
    tmp_path: Path,
) -> None:
    alert = _alert()
    buy = build_human_paper_intent(feedback=_feedback(alert), alert=alert)
    assert buy is not None
    position_symbol = "SH.600001"
    acquired_at = REVIEWED_AT - timedelta(days=1)
    events = (
        {
            "kind": "FILL",
            "payload": {
                "intent_id": "sha256:" + "a" * 64,
                "symbol": position_symbol,
                "side": "BUY",
                "quantity": 100,
                "filled_at": acquired_at.isoformat(),
            },
        },
        {"kind": "INTENT", "payload": buy.document()},
    )
    forward_root = tmp_path / "unresolved-mark"
    fact_path, evidence_path = _write_continuity_evidence(
        forward_root=forward_root,
        session=REVIEWED_AT,
        intent_id=buy.intent_id,
        symbol=alert.symbol,
    )
    facts = json.loads(fact_path.read_text(encoding="utf-8"))
    facts.pop("content_sha256")
    facts["errors"] = [
        {
            "symbol": position_symbol,
            "reason": "QMT_EXECUTION_FACT_CAPTURE_FAILED",
            "detail": "RuntimeError: unavailable",
        }
    ]
    facts["requested_symbol_count"] = 2
    facts["all_complete"] = False
    fact_id = sha256_json(facts)
    fact_path.with_name(f"{fact_id[7:]}.json").write_text(
        json.dumps({**facts, "content_sha256": fact_id}),
        encoding="utf-8",
    )
    fact_path.unlink()

    evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
    evidence.pop("content_sha256")
    evidence["execution_fact_snapshot_sha256"] = fact_id
    evidence["bars_by_symbol"][position_symbol] = []
    evidence["bar_grid_audits"].append(
        {
            "symbol": position_symbol,
            "status": "EXECUTION_FACT_MISSING_FAIL_CLOSED",
            "native_row_count": 0,
            "normalized_row_count": 0,
            "complete_sessions": [],
            "session_issues": [
                {
                    "session": REVIEWED_AT.date().isoformat(),
                    "code": "QMT_EXECUTION_FACT_REQUIRED_BEFORE_BAR_GRID",
                    "observed_rows": 0,
                    "detail": "same-session instrument facts are unavailable",
                }
            ],
            "source_base_stream_revision": None,
        }
    )
    evidence["all_required_bar_grids_complete"] = False
    evidence_id = sha256_json(evidence)
    evidence_path.with_name(f"{evidence_id[7:]}.json").write_text(
        json.dumps({**evidence, "content_sha256": evidence_id}),
        encoding="utf-8",
    )
    evidence_path.unlink()

    audit = audit_human_paper_pending_continuity(
        events,
        forward_root=forward_root,
        current_session=(REVIEWED_AT + timedelta(days=1)).date(),
        trading_sessions=(REVIEWED_AT.date(),),
    )

    assert audit["status"] == "COMPLETE"
    assert audit["covered_intent_session_count"] == 1
    assert audit["gap_intent_count"] == 0


def test_later_position_cannot_backfill_an_earlier_continuity_capture(
    tmp_path: Path,
) -> None:
    alert = _alert()
    buy = build_human_paper_intent(feedback=_feedback(alert), alert=alert)
    assert buy is not None
    forward_root = tmp_path / "future-position"
    _write_continuity_evidence(
        forward_root=forward_root,
        session=REVIEWED_AT,
        intent_id=buy.intent_id,
        symbol=alert.symbol,
    )
    later_session = REVIEWED_AT + timedelta(days=1)
    _later_fact_path, later_evidence_path = _write_continuity_evidence(
        forward_root=forward_root,
        session=later_session,
        intent_id="sha256:" + "c" * 64,
        symbol="SH.600001",
    )
    later_evidence = json.loads(
        later_evidence_path.read_text(encoding="utf-8")
    )
    events = (
        {"kind": "INTENT", "payload": buy.document()},
        {
            "kind": "FILL",
            "payload": {
                "intent_id": "sha256:" + "d" * 64,
                "symbol": "SH.600001",
                "side": "BUY",
                "quantity": 100,
                "filled_at": later_session.replace(hour=10).isoformat(),
                "execution_snapshot_sha256": later_evidence["content_sha256"],
            },
        },
    )

    audit = audit_human_paper_pending_continuity(
        events,
        forward_root=forward_root,
        current_session=(REVIEWED_AT + timedelta(days=2)).date(),
        trading_sessions=(REVIEWED_AT.date(),),
    )

    assert audit["status"] == "CAUSAL_GAPS"
    assert audit["covered_intent_session_count"] == 0
    assert audit["gaps"][0]["reason"] == (
        "TERMINAL_OUTCOME_MISSING_FROM_LEDGER"
    )


@pytest.mark.parametrize(
    ("acquired_days_before", "expected_status"),
    ((0, "COMPLETE"), (1, "CAUSAL_GAPS")),
)
def test_pending_sell_continuity_recomputes_t_plus_one_and_fill_obligation(
    tmp_path: Path,
    acquired_days_before: int,
    expected_status: str,
) -> None:
    alert = _alert(alert_type="POSSIBLE_30M_EXIT")
    sell = build_human_paper_intent(
        feedback=_sell_feedback(alert),
        alert=alert,
        virtual_position_quantity=100,
    )
    assert sell is not None and sell.side == "SELL"
    acquired_on = REVIEWED_AT.date() - timedelta(
        days=acquired_days_before
    )
    acquired_at = datetime.combine(acquired_on, time(10), tzinfo=TZ)
    events = (
        {
            "kind": "FILL",
            "payload": {
                "intent_id": "sha256:" + "a" * 64,
                "symbol": alert.symbol,
                "side": "BUY",
                "quantity": 100,
                "filled_at": acquired_at.isoformat(),
            },
        },
        {"kind": "INTENT", "payload": sell.document()},
    )
    forward_root = tmp_path / f"sell-{acquired_days_before}"
    _write_continuity_evidence(
        forward_root=forward_root,
        session=REVIEWED_AT,
        intent_id=sell.intent_id,
        symbol=alert.symbol,
        virtual_position_quantity=100,
        oldest_virtual_acquired_session=acquired_on,
    )

    audit = audit_human_paper_pending_continuity(
        events,
        forward_root=forward_root,
        current_session=(REVIEWED_AT + timedelta(days=1)).date(),
        trading_sessions=(REVIEWED_AT.date(),),
    )

    assert audit["status"] == expected_status
    if acquired_days_before == 0:
        assert audit["covered_intent_session_count"] == 1
        assert audit["gap_intent_count"] == 0
    else:
        assert audit["covered_intent_session_count"] == 0
        assert audit["gaps"][0]["reason"] == (
            "TERMINAL_OUTCOME_MISSING_FROM_LEDGER"
        )


@pytest.mark.parametrize(
    "forgery",
    ("AGGREGATE_COUNT", "RAW_SECURITY_STATUS", "LIMIT_LOCK"),
)
def test_pending_continuity_rejects_fully_rehashed_document_forgery(
    tmp_path: Path,
    forgery: str,
) -> None:
    path = tmp_path / "paper.json"
    alert = _alert()
    intent = build_human_paper_intent(feedback=_feedback(alert), alert=alert)
    assert intent is not None
    document, _event = append_human_paper_intent(path, intent)
    forward_root = tmp_path / forgery.lower()
    fact_path, evidence_path = _write_continuity_evidence(
        forward_root=forward_root,
        session=REVIEWED_AT,
        intent_id=intent.intent_id,
        symbol=alert.symbol,
        eligible=forgery != "RAW_SECURITY_STATUS",
    )
    facts = json.loads(fact_path.read_text(encoding="utf-8"))
    evidence = json.loads(evidence_path.read_text(encoding="utf-8"))

    if forgery == "AGGREGATE_COUNT":
        facts.pop("content_sha256")
        facts["requested_symbol_count"] = 2
    elif forgery == "RAW_SECURITY_STATUS":
        facts.pop("content_sha256")
        facts["symbols"][0]["instrument_status"] = 0
    else:
        evidence.pop("content_sha256")
        first_bar = evidence["bars_by_symbol"][alert.symbol][0]
        first_bar.update(
            {
                "open": "11",
                "high": "11",
                "low": "11",
                "close": "11",
                "limit_up_locked": False,
            }
        )

    if "content_sha256" not in facts:
        new_fact_id = sha256_json(facts)
        new_fact_path = fact_path.with_name(f"{new_fact_id[7:]}.json")
        new_fact_path.write_text(
            json.dumps({**facts, "content_sha256": new_fact_id}),
            encoding="utf-8",
        )
        fact_path.unlink()
        evidence.pop("content_sha256")
        evidence["execution_fact_snapshot_sha256"] = new_fact_id
    new_evidence_id = sha256_json(evidence)
    new_evidence_path = evidence_path.with_name(
        f"{new_evidence_id[7:]}.json"
    )
    new_evidence_path.write_text(
        json.dumps({**evidence, "content_sha256": new_evidence_id}),
        encoding="utf-8",
    )
    evidence_path.unlink()

    audit = audit_human_paper_pending_continuity(
        document["events"],
        forward_root=forward_root,
        current_session=(REVIEWED_AT + timedelta(days=1)).date(),
        trading_sessions=(REVIEWED_AT.date(),),
    )
    assert audit["status"] == "CAUSAL_GAPS"
    assert audit["covered_intent_session_count"] == 0


def test_pending_intent_cannot_jump_over_a_missing_forward_session(
    tmp_path: Path,
) -> None:
    path = tmp_path / "paper.json"
    alert = _alert()
    intent = build_human_paper_intent(feedback=_feedback(alert), alert=alert)
    assert intent is not None
    document, _event = append_human_paper_intent(path, intent)
    forward_root = tmp_path / "forward"
    first_session = REVIEWED_AT
    second_session = REVIEWED_AT + timedelta(days=1)
    current_session = (REVIEWED_AT + timedelta(days=2)).date()

    _write_continuity_evidence(
        forward_root=forward_root,
        session=first_session,
        intent_id=intent.intent_id,
        symbol=alert.symbol,
        executable=False,
    )
    missing = audit_human_paper_pending_continuity(
        document["events"],
        forward_root=forward_root,
        current_session=current_session,
        trading_sessions=(first_session.date(), second_session.date()),
    )
    assert missing["status"] == "CAUSAL_GAPS"
    assert missing["required_intent_session_count"] == 2
    assert missing["covered_intent_session_count"] == 1
    assert missing["gap_intent_ids"] == [intent.intent_id]
    assert missing["gaps"] == [
        {
            "intent_id": intent.intent_id,
            "symbol": alert.symbol,
            "session": second_session.date().isoformat(),
            "reason": "FULL_SESSION_EXECUTION_EVIDENCE_MISSING",
        }
    ]

    _write_continuity_evidence(
        forward_root=forward_root,
        session=second_session,
        intent_id=intent.intent_id,
        symbol=alert.symbol,
        executable=False,
    )
    complete = audit_human_paper_pending_continuity(
        document["events"],
        forward_root=forward_root,
        current_session=current_session,
        trading_sessions=(first_session.date(), second_session.date()),
    )
    assert complete["status"] == "COMPLETE"
    assert complete["covered_intent_session_count"] == 2
    assert complete["gap_intent_count"] == 0
    assert latest_human_paper_pending_continuity(document["events"], ())["status"] == (
        "UNPROVEN"
    )
    exposed = latest_human_paper_pending_continuity(
        document["events"],
        (
            {
                "evidence": {
                    "human_paper_settlement": {
                        "pending_continuity": complete,
                    }
                }
            },
        ),
    )
    assert exposed == complete


def test_virtual_sell_without_position_is_observation_only() -> None:
    alert = _alert(alert_type="POSSIBLE_30M_EXIT")
    intent = build_human_paper_intent(
        feedback=_sell_feedback(alert),
        alert=alert,
        virtual_position_quantity=0,
    )
    assert intent is not None
    assert intent.status == "OBSERVATION_ONLY"
    assert intent.reason_codes == ("SELL_REVIEW_HAS_NO_VIRTUAL_POSITION",)


def test_risk_gate_never_blocks_an_existing_virtual_position_exit() -> None:
    alert = _alert(
        market_gate="RED",
        symbol_gate="AMBER",
        alert_type="POSSIBLE_30M_EXIT",
    )
    intent = build_human_paper_intent(
        feedback=_sell_feedback(alert),
        alert=alert,
        virtual_position_quantity=100,
    )
    assert intent is not None
    assert intent.status == "PENDING"


def test_incomplete_human_structure_is_observation_only() -> None:
    alert = _alert()
    feedback = replace(_feedback(alert), center_judgement="UNCERTAIN")
    intent = build_human_paper_intent(feedback=feedback, alert=alert)
    assert intent is not None
    assert intent.status == "OBSERVATION_ONLY"


def test_uncertain_human_trend_type_cannot_create_virtual_intent() -> None:
    alert = _alert()
    feedback = replace(_feedback(alert), trend_judgement="UNCERTAIN")
    intent = build_human_paper_intent(feedback=feedback, alert=alert)
    assert intent is not None and intent.status == "OBSERVATION_ONLY"
    assert intent.reason_codes == (
        "HUMAN_TREND_TYPE_CONFIRMATION_INCOMPLETE",
        "HUMAN_CONFIRM_TREND_TYPE_BEFORE_VIRTUAL_INTENT",
    )


def test_warmup_divergence_blocks_virtual_buy_but_never_strands_exit() -> None:
    buy_alert = replace(_alert(), warning_codes=("WARMUP_NOT_CONVERGED",))
    buy = build_human_paper_intent(
        feedback=_feedback(buy_alert),
        alert=buy_alert,
    )
    assert buy is not None and buy.status == "OBSERVATION_ONLY"
    assert buy.reason_codes == (
        "WARMUP_CONVERGENCE_REQUIRED_FOR_VIRTUAL_ENTRY",
        "WARMUP_DIVERGENCE_IS_NOT_HUMAN_OVERRIDABLE",
    )

    sell_alert = replace(
        _alert(alert_type="POSSIBLE_SELL_REVIEW"),
        warning_codes=("WARMUP_NOT_CONVERGED",),
        entry_confirmation_bar_closed_at=None,
        entry_price_cap=None,
        entry_valid_until=None,
        entry_boundary_evidence_id=None,
    )
    sell = build_human_paper_intent(
        feedback=_sell_feedback(sell_alert),
        alert=sell_alert,
        virtual_position_quantity=100,
    )
    assert sell is not None and sell.status == "PENDING"
    assert sell.reason_codes == ("HUMAN_CONFIRMED_VIRTUAL_EXIT",)


def test_fixed_one_lot_tactical_alert_is_review_only_at_5m_level() -> None:
    alert = _alert(alert_type="POSSIBLE_5M_TACTICAL_SELL")
    tactical_feedback = replace(
        _sell_feedback(alert),
        level_judgement="5M",
    )
    intent = build_human_paper_intent(
        feedback=tactical_feedback,
        alert=alert,
        virtual_position_quantity=100,
    )
    assert intent is not None and intent.status == "OBSERVATION_ONLY"
    assert intent.reason_codes == (
        "FIXED_ONE_LOT_TACTICAL_TARGET_BELOW_TRADING_UNIT",
        "TACTICAL_REVIEW_OBSERVATION_ONLY",
    )

    wrong_level = build_human_paper_intent(
        feedback=replace(tactical_feedback, level_judgement="30M"),
        alert=alert,
        virtual_position_quantity=100,
    )
    assert wrong_level is not None and wrong_level.status == "OBSERVATION_ONLY"
    assert "EXPECTED_REVIEW_LEVEL_5M" in wrong_level.reason_codes

    buyback_alert = _alert(alert_type="POSSIBLE_5M_TACTICAL_BUYBACK")
    buyback = build_human_paper_intent(
        feedback=replace(
            _feedback(buyback_alert),
            level_judgement="5M",
        ),
        alert=buyback_alert,
        virtual_position_quantity=100,
    )
    assert buyback is not None and buyback.status == "OBSERVATION_ONLY"
    assert buyback.reason_codes == intent.reason_codes


def test_unclassified_sell_defers_30m_or_5m_role_to_human() -> None:
    alert = _alert(alert_type="POSSIBLE_SELL_REVIEW")

    strategic = build_human_paper_intent(
        feedback=_sell_feedback(alert),
        alert=alert,
        virtual_position_quantity=100,
    )
    assert strategic is not None and strategic.status == "PENDING"
    assert strategic.reason_codes == ("HUMAN_CONFIRMED_VIRTUAL_EXIT",)

    tactical = build_human_paper_intent(
        feedback=replace(_sell_feedback(alert), level_judgement="5M"),
        alert=alert,
        virtual_position_quantity=100,
    )
    assert tactical is not None and tactical.status == "OBSERVATION_ONLY"
    assert tactical.reason_codes == (
        "FIXED_ONE_LOT_TACTICAL_TARGET_BELOW_TRADING_UNIT",
        "TACTICAL_REVIEW_OBSERVATION_ONLY",
    )

    unresolved = build_human_paper_intent(
        feedback=replace(_sell_feedback(alert), level_judgement="1M"),
        alert=alert,
        virtual_position_quantity=100,
    )
    assert unresolved is not None and unresolved.status == "OBSERVATION_ONLY"
    assert unresolved.reason_codes == (
        "HUMAN_STRUCTURE_CONFIRMATION_INCOMPLETE",
        "EXPECTED_REVIEW_LEVEL_30M_OR_5M",
    )


@pytest.mark.parametrize(
    ("alert_type", "feedback_factory", "virtual_position_quantity"),
    (
        ("POSSIBLE_30M_BUY", _sell_feedback, 100),
        ("POSSIBLE_30M_EXIT", _feedback, 0),
        ("POSSIBLE_SELL_REVIEW", _feedback, 0),
    ),
)
def test_opposite_human_judgement_is_recorded_but_never_becomes_an_intent(
    alert_type: str,
    feedback_factory,
    virtual_position_quantity: int,
) -> None:
    alert = _alert(alert_type=alert_type)
    intent = build_human_paper_intent(
        feedback=feedback_factory(alert),
        alert=alert,
        virtual_position_quantity=virtual_position_quantity,
    )
    assert intent is not None and intent.status == "OBSERVATION_ONLY"
    assert intent.reason_codes == (
        "HUMAN_POINT_SIDE_CONTRADICTS_PROGRAM_CLUE",
        "CONTRADICTORY_REVIEW_CANNOT_CREATE_VIRTUAL_INTENT",
    )


def test_open_strategic_cycle_cannot_create_another_buy_intent() -> None:
    alert = _alert()
    intent = build_human_paper_intent(
        feedback=_feedback(alert),
        alert=alert,
        virtual_position_quantity=100,
        reserved_virtual_sell_quantity=100,
    )
    assert intent is not None and intent.status == "OBSERVATION_ONLY"
    assert intent.reason_codes == (
        "VIRTUAL_STRATEGIC_CYCLE_ALREADY_OPEN",
        "ONE_SECURITY_ONE_STRATEGIC_SLOT",
    )


def test_consumed_buy_point_cannot_reopen_after_strategic_cycle_closes(
    tmp_path: Path,
) -> None:
    """卖空旧周期后仍必须等待新 source point，不能复用旧买点。"""

    path = tmp_path / "paper.json"
    buy_alert = _alert()
    reconcile_human_paper_feedback(
        path,
        feedback=_feedback(buy_alert),
        alert=buy_alert,
    )
    settle_human_paper_intents(
        path,
        bars_by_symbol={
            buy_alert.symbol: (_bar(REVIEWED_AT + timedelta(minutes=1)),)
        },
    )

    sell_alert = _alert(alert_type="POSSIBLE_30M_EXIT")
    reconcile_human_paper_feedback(
        path,
        feedback=_sell_feedback(sell_alert),
        alert=sell_alert,
    )
    closed = settle_human_paper_intents(
        path,
        bars_by_symbol={
            buy_alert.symbol: (_bar(REVIEWED_AT + timedelta(days=1)),)
        },
    )
    assert human_paper_position_quantities(closed["events"]) == {}

    later_review = REVIEWED_AT + timedelta(days=2)
    repeated_alert = replace(
        buy_alert,
        signal_at=later_review - timedelta(minutes=30),
        review_available_at=later_review,
        structure_snapshot_id="sha256:" + "8" * 64,
        source_fact_ids=("sha256:" + "9" * 64,),
    )
    assert repeated_alert.candidate_id != buy_alert.candidate_id
    assert repeated_alert.signal_lifecycle_id == buy_alert.signal_lifecycle_id
    repeated_feedback = replace(
        _feedback(repeated_alert),
        reviewed_at=later_review,
        source_screen_content_sha256="sha256:" + "a" * 64,
    )

    document, event, cancellations, changed = reconcile_human_paper_feedback(
        path,
        feedback=repeated_feedback,
        alert=repeated_alert,
    )

    assert changed is True
    assert cancellations == ()
    assert event is not None
    assert event["payload"]["status"] == "OBSERVATION_ONLY"
    assert event["payload"]["reason_codes"] == [
        "SIGNAL_LIFECYCLE_ALREADY_CONSUMED",
        "NEW_STRUCTURE_REQUIRED_FOR_NEW_VIRTUAL_CYCLE",
    ]
    assert human_paper_position_quantities(document["events"]) == {}
    assert [value["kind"] for value in document["events"]].count("FILL") == 2


def test_direct_append_cannot_bypass_consumed_lifecycle_latch(
    tmp_path: Path,
) -> None:
    path = tmp_path / "paper.json"
    alert = _alert()
    first = build_human_paper_intent(feedback=_feedback(alert), alert=alert)
    assert first is not None
    append_human_paper_intent(path, first)
    settle_human_paper_intents(
        path,
        bars_by_symbol={alert.symbol: (_bar(REVIEWED_AT + timedelta(minutes=1)),)},
    )

    later = REVIEWED_AT + timedelta(days=1)
    repeated_alert = replace(
        alert,
        signal_at=later - timedelta(minutes=30),
        review_available_at=later,
        structure_snapshot_id="sha256:" + "8" * 64,
    )
    repeated = build_human_paper_intent(
        feedback=replace(_feedback(repeated_alert), reviewed_at=later),
        alert=repeated_alert,
    )
    assert repeated is not None and repeated.status == "PENDING"

    with pytest.raises(ValueError, match="reused a consumed signal lifecycle"):
        append_human_paper_intent(path, repeated)


def test_fully_rehashed_consumed_lifecycle_reuse_is_rejected(
    tmp_path: Path,
) -> None:
    """局部、事件链和文件哈希全重算也不能复用旧买点。"""

    path = tmp_path / "paper.json"
    alert = _alert()
    first = build_human_paper_intent(feedback=_feedback(alert), alert=alert)
    assert first is not None
    append_human_paper_intent(path, first)
    settle_human_paper_intents(
        path,
        bars_by_symbol={alert.symbol: (_bar(REVIEWED_AT + timedelta(minutes=1)),)},
    )

    later = REVIEWED_AT + timedelta(days=1)
    repeated_alert = replace(
        alert,
        signal_at=later - timedelta(minutes=30),
        review_available_at=later,
        structure_snapshot_id="sha256:" + "8" * 64,
    )
    repeated = build_human_paper_intent(
        feedback=replace(_feedback(repeated_alert), reviewed_at=later),
        alert=repeated_alert,
    )
    assert repeated is not None and repeated.status == "PENDING"

    payload = json.loads(path.read_text(encoding="utf-8"))
    raw_intent = json.loads(json.dumps(asdict(repeated), default=str))
    raw_intent["intent_id"] = repeated.intent_id
    stable_event = {
        "kind": "INTENT",
        "payload": raw_intent,
        "previous_event_id": payload["events"][-1]["event_id"],
    }
    payload["events"].append(
        {**stable_event, "event_id": sha256_json(stable_event)}
    )
    stable_document = dict(payload)
    stable_document.pop("content_sha256")
    payload["content_sha256"] = sha256_json(stable_document)
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ValueError, match="reused a consumed signal lifecycle"):
        load_human_paper_ledger(path)


def test_human_paper_ledger_rejects_outer_rehashed_payload_tampering(
    tmp_path: Path,
) -> None:
    path = tmp_path / "paper.json"
    alert = _alert()
    intent = build_human_paper_intent(feedback=_feedback(alert), alert=alert)
    assert intent is not None
    append_human_paper_intent(path, intent)

    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["events"][0]["payload"]["quantity"] = 200
    stable = dict(payload)
    stable.pop("content_sha256")
    payload["content_sha256"] = sha256_json(stable)
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ValueError, match="event hash mismatch"):
        load_human_paper_ledger(path)


def test_human_paper_ledger_rejects_fully_rehashed_safety_tampering(
    tmp_path: Path,
) -> None:
    """重算意图、事件和文件全部哈希也不能绕过 LIVE_DISABLED 语义门。"""

    path = tmp_path / "paper.json"
    alert = _alert()
    intent = build_human_paper_intent(feedback=_feedback(alert), alert=alert)
    assert intent is not None
    append_human_paper_intent(path, intent)

    payload = json.loads(path.read_text(encoding="utf-8"))
    event = payload["events"][0]
    intent_payload = event["payload"]
    intent_payload["automated_order_authorized"] = True

    identity_document = dict(intent_payload)
    identity_document.pop("intent_id")
    intent_payload["intent_id"] = sha256_json(identity_document)

    stable_event = dict(event)
    stable_event.pop("event_id")
    event["event_id"] = sha256_json(stable_event)

    stable_document = dict(payload)
    stable_document.pop("content_sha256")
    payload["content_sha256"] = sha256_json(stable_document)
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ValueError, match="intent payload is invalid"):
        load_human_paper_ledger(path)


def test_human_paper_ledger_rejects_fully_rehashed_cancellation_tampering(
    tmp_path: Path,
) -> None:
    path = tmp_path / "paper.json"
    alert = _alert()
    first_feedback = _feedback(alert)
    reconcile_human_paper_feedback(
        path,
        feedback=first_feedback,
        alert=alert,
    )
    reconcile_human_paper_feedback(
        path,
        feedback=replace(
            first_feedback,
            reviewed_at=REVIEWED_AT + timedelta(minutes=5),
            disposition="WATCH",
        ),
        alert=alert,
    )

    payload = json.loads(path.read_text(encoding="utf-8"))
    cancellation_event = payload["events"][-1]
    cancellation = cancellation_event["payload"]
    cancellation["broker_transport_available"] = True
    cancellation_identity = dict(cancellation)
    cancellation_identity.pop("cancellation_id")
    cancellation["cancellation_id"] = sha256_json(cancellation_identity)
    stable_event = dict(cancellation_event)
    stable_event.pop("event_id")
    cancellation_event["event_id"] = sha256_json(stable_event)
    stable_document = dict(payload)
    stable_document.pop("content_sha256")
    payload["content_sha256"] = sha256_json(stable_document)
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ValueError, match="cancellation payload is invalid"):
        load_human_paper_ledger(path)


def test_human_paper_ledger_rejects_fully_rehashed_capital_rejection_tampering(
    tmp_path: Path,
) -> None:
    path = tmp_path / "paper.json"
    alert = _alert()
    intent = build_human_paper_intent(feedback=_feedback(alert), alert=alert)
    assert intent is not None
    append_human_paper_intent(path, intent)
    expensive = replace(
        _bar(REVIEWED_AT + timedelta(minutes=1)),
        open=Decimal("10000.00"),
        high=Decimal("10001.00"),
        low=Decimal("9999.00"),
        close=Decimal("10000.00"),
    )
    settle_human_paper_intents_with_capital_controls(
        path,
        bars_by_symbol={alert.symbol: (expensive,)},
        accounting_parameters=ACCOUNTING_PARAMETERS,
    )

    payload = json.loads(path.read_text(encoding="utf-8"))
    rejection_event = payload["events"][-1]
    rejection = rejection_event["payload"]
    rejection["available_cash"] = rejection["required_cash"]
    rejection_identity = dict(rejection)
    rejection_identity.pop("rejection_id")
    rejection["rejection_id"] = sha256_json(rejection_identity)
    stable_event = dict(rejection_event)
    stable_event.pop("event_id")
    rejection_event["event_id"] = sha256_json(stable_event)
    stable_document = dict(payload)
    stable_document.pop("content_sha256")
    payload["content_sha256"] = sha256_json(stable_document)
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ValueError, match="capital rejection payload is invalid"):
        load_human_paper_ledger(path)


def test_capital_decision_audit_rejects_plausible_fully_rehashed_cash_tampering(
    tmp_path: Path,
) -> None:
    """A locally self-consistent rejection must still match its ledger prefix."""

    path = tmp_path / "paper.json"
    alert = _alert()
    intent = build_human_paper_intent(feedback=_feedback(alert), alert=alert)
    assert intent is not None
    append_human_paper_intent(path, intent)
    expensive = replace(
        _bar(REVIEWED_AT + timedelta(minutes=1)),
        open=Decimal("10000.00"),
        high=Decimal("10001.00"),
        low=Decimal("9999.00"),
        close=Decimal("10000.00"),
    )
    settle_human_paper_intents_with_capital_controls(
        path,
        bars_by_symbol={alert.symbol: (expensive,)},
        accounting_parameters=ACCOUNTING_PARAMETERS,
    )

    payload = json.loads(path.read_text(encoding="utf-8"))
    rejection_event = payload["events"][-1]
    rejection = rejection_event["payload"]
    # Keep the insufficient-cash predicate true so the dataclass remains
    # locally valid, but lie about the cash reconstructed from prior fills.
    rejection["available_cash"] = "999999.99"
    decision_stable = {
        "schema": "chanlun-human-paper-cash-slot-decision/v1",
        "accounting_contract_id": rejection["accounting_contract_id"],
        "symbol": rejection["symbol"],
        "quantity": rejection["quantity"],
        "price": rejection["candidate_price"],
        "session": datetime.fromisoformat(
            rejection["candidate_bar_opened_at"]
        ).date().isoformat(),
        "available_cash": rejection["available_cash"],
        "notional": rejection["notional"],
        "terminal_buy_fee": rejection["terminal_buy_fee"],
        "required_cash": rejection["required_cash"],
        "occupied_slots": rejection["occupied_slots"],
        "slot_count": rejection["slot_count"],
        "allowed": False,
        "reason_codes": tuple(rejection["reason_codes"]),
        "slot_fraction_notional_gate_evaluable": False,
        "account_exposure_notional_gate_evaluable": False,
        "fixed_one_lot_diagnostic": True,
        "tick_data_used": False,
        "broker_transport_available": False,
        "live_status": "LIVE_DISABLED",
    }
    rejection["cash_slot_decision_sha256"] = sha256_json(decision_stable)
    model_values = dict(rejection)
    model_values.pop("rejection_id")
    for name in (
        "candidate_bar_opened_at",
        "candidate_bar_closed_at",
        "rejected_at",
    ):
        model_values[name] = datetime.fromisoformat(model_values[name])
    for name in (
        "candidate_price",
        "available_cash",
        "notional",
        "terminal_buy_fee",
        "required_cash",
    ):
        model_values[name] = Decimal(model_values[name])
    model_values["reason_codes"] = tuple(model_values["reason_codes"])
    rejection["rejection_id"] = HumanPaperCapitalRejection(
        **model_values
    ).rejection_id
    stable_event = dict(rejection_event)
    stable_event.pop("event_id")
    rejection_event["event_id"] = sha256_json(stable_event)
    stable_document = dict(payload)
    stable_document.pop("content_sha256")
    payload["content_sha256"] = sha256_json(stable_document)
    path.write_text(json.dumps(payload), encoding="utf-8")

    loaded = load_human_paper_ledger(path)
    audit = audit_human_paper_capital_decisions(
        loaded["events"],
        parameters=ACCOUNTING_PARAMETERS,
    )
    assert audit["status"] == "INVALID"
    assert audit["verified_rejection_count"] == 0
    assert "decision identity disagrees" in audit["invalid_decisions"][0]["reason"]


def test_human_paper_ledger_rejects_fully_rehashed_t_plus_one_violation(
    tmp_path: Path,
) -> None:
    """同日卖出即使重算全部身份，也不能成为可加载的虚拟事实。"""

    path = tmp_path / "paper.json"
    alert = _alert()
    buy = build_human_paper_intent(feedback=_feedback(alert), alert=alert)
    assert buy is not None
    append_human_paper_intent(path, buy)
    bought = settle_human_paper_intents(
        path,
        bars_by_symbol={alert.symbol: (_bar(REVIEWED_AT + timedelta(minutes=4)),)},
    )
    positions = human_paper_position_quantities(bought["events"])
    sell_alert = _alert(alert_type="POSSIBLE_30M_EXIT")
    sell = build_human_paper_intent(
        feedback=_sell_feedback(sell_alert),
        alert=sell_alert,
        virtual_position_quantity=positions[alert.symbol],
    )
    assert sell is not None
    append_human_paper_intent(path, sell)
    settle_human_paper_intents(
        path,
        bars_by_symbol={
            alert.symbol: (_bar(REVIEWED_AT + timedelta(days=1, minutes=1)),)
        },
    )

    payload = json.loads(path.read_text(encoding="utf-8"))
    event = payload["events"][-1]
    fill_payload = event["payload"]
    forged_fill_at = REVIEWED_AT + timedelta(hours=3)
    forged_fill_close = forged_fill_at + timedelta(minutes=1)
    fill_payload["filled_at"] = forged_fill_close.isoformat()
    fill_payload["source_bar_closed_at"] = forged_fill_close.isoformat()
    fill_identity = dict(fill_payload)
    fill_identity.pop("fill_id")
    fill_identity["filled_at"] = datetime.fromisoformat(
        str(fill_identity["filled_at"])
    )
    fill_identity["source_bar_closed_at"] = datetime.fromisoformat(
        str(fill_identity["source_bar_closed_at"])
    )
    fill_identity["price"] = Decimal(str(fill_identity["price"]))
    fill_payload["fill_id"] = sha256_json(fill_identity)

    stable_event = dict(event)
    stable_event.pop("event_id")
    event["event_id"] = sha256_json(stable_event)
    stable_document = dict(payload)
    stable_document.pop("content_sha256")
    payload["content_sha256"] = sha256_json(stable_document)
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ValueError, match=r"oversell or T\+1 violation"):
        load_human_paper_ledger(path)


def _operations_cancel(
    intent,
    *,
    cancelled_at: datetime | None = None,
) -> HumanPaperOperationsCancellation:
    return HumanPaperOperationsCancellation(
        intent_id=intent.intent_id,
        symbol=intent.symbol,
        candidate_id=intent.candidate_id,
        signal_lifecycle_id=intent.signal_lifecycle_id,
        cancelled_at=cancelled_at or REVIEWED_AT + timedelta(hours=5),
        execution_fact_snapshot_sha256="sha256:" + "c" * 64,
        execution_evidence_snapshot_sha256="sha256:" + "d" * 64,
        grid_status="INVALID_FAIL_CLOSED",
    )


def test_operations_cancel_is_terminal_and_consumes_signal_lifecycle(
    tmp_path: Path,
) -> None:
    path = tmp_path / "paper.json"
    alert = _alert()
    intent = build_human_paper_intent(feedback=_feedback(alert), alert=alert)
    assert intent is not None and intent.signal_lifecycle_id is not None
    append_human_paper_intent(path, intent)

    cancelled, evaluations = settle_human_paper_intents_with_portfolio_controls(
        path,
        bars_by_symbol={},
        accounting_parameters=ACCOUNTING_PARAMETERS,
        operations_cancellations=(_operations_cancel(intent),),
    )

    assert [event["kind"] for event in cancelled["events"]] == [
        "INTENT",
        "OPERATIONS_CANCEL",
    ]
    assert evaluations[0]["result"] == (
        "OPTIONAL_BUY_CANCELLED_BY_EXECUTION_DATA_HALT"
    )
    assert intent.signal_lifecycle_id in human_paper_consumed_signal_lifecycle_ids(
        cancelled["events"]
    )
    replacement = replace(
        intent,
        feedback_id="sha256:" + "e" * 64,
        created_at=intent.created_at + timedelta(minutes=1),
        earliest_fill_at=intent.earliest_fill_at + timedelta(minutes=1),
    )
    with pytest.raises(ValueError, match="reused a consumed signal lifecycle"):
        append_human_paper_intent(path, replacement)


def test_security_gate_operations_cancel_has_distinct_reason_and_state() -> None:
    intent = build_human_paper_intent(
        feedback=_feedback(_alert()),
        alert=_alert(),
    )
    assert intent is not None
    cancellation = HumanPaperOperationsCancellation(
        intent_id=intent.intent_id,
        symbol=intent.symbol,
        candidate_id=intent.candidate_id,
        signal_lifecycle_id=intent.signal_lifecycle_id,
        cancelled_at=REVIEWED_AT + timedelta(hours=5),
        execution_fact_snapshot_sha256="sha256:" + "c" * 64,
        execution_evidence_snapshot_sha256="sha256:" + "d" * 64,
        grid_status="NOT_REQUIRED_INSTRUMENT_INELIGIBLE",
        reason_code="OPTIONAL_BUY_CANCELLED_BY_SECURITY_GATE",
        operations_state="SECURITY_GATE_CLOSED",
    )
    assert cancellation.reason_code == (
        "OPTIONAL_BUY_CANCELLED_BY_SECURITY_GATE"
    )

    with pytest.raises(
        ValueError,
        match="operations cancellation safety boundary changed",
    ):
        replace(cancellation, operations_state="OPERATIONS_HALT")
    with pytest.raises(
        ValueError,
        match="operations cancellation safety boundary changed",
    ):
        replace(
            cancellation,
            reason_code="OPTIONAL_BUY_CANCELLED_BY_EXECUTION_DATA_HALT",
        )
    with pytest.raises(
        ValueError,
        match="operations cancellation safety boundary changed",
    ):
        replace(
            cancellation,
            grid_status="EXECUTION_FACT_MISSING_FAIL_CLOSED",
        )


def test_operations_cancel_and_other_fill_are_atomic_and_causally_ordered(
    tmp_path: Path,
) -> None:
    path = tmp_path / "paper.json"
    first_alert = _alert()
    failed = build_human_paper_intent(
        feedback=_feedback(first_alert),
        alert=first_alert,
    )
    assert failed is not None
    eligible = replace(
        failed,
        feedback_id="sha256:" + "e" * 64,
        candidate_id="sha256:" + "f" * 64,
        signal_lifecycle_id="sha256:" + "a" * 64,
        symbol="SH.600001",
    )
    append_human_paper_intent(path, failed)
    append_human_paper_intent(path, eligible)
    eligible_bar = replace(
        _bar(REVIEWED_AT + timedelta(minutes=1)),
        symbol=eligible.symbol,
    )

    settled, evaluations = settle_human_paper_intents_with_portfolio_controls(
        path,
        bars_by_symbol={eligible.symbol: (eligible_bar,)},
        accounting_parameters=ACCOUNTING_PARAMETERS,
        operations_cancellations=(_operations_cancel(failed),),
    )

    assert [event["kind"] for event in settled["events"]] == [
        "INTENT",
        "INTENT",
        "FILL",
        "OPERATIONS_CANCEL",
    ]
    assert settled["events"][-2]["payload"]["symbol"] == eligible.symbol
    assert settled["events"][-1]["payload"]["intent_id"] == failed.intent_id
    assert [value["result"] for value in evaluations] == [
        "FILL_ALLOWED",
        "OPTIONAL_BUY_CANCELLED_BY_EXECUTION_DATA_HALT",
    ]


def test_human_paper_ledger_rejects_rehashed_operations_cancel_tampering(
    tmp_path: Path,
) -> None:
    path = tmp_path / "paper.json"
    alert = _alert()
    intent = build_human_paper_intent(feedback=_feedback(alert), alert=alert)
    assert intent is not None
    append_human_paper_intent(path, intent)
    settle_human_paper_intents_with_portfolio_controls(
        path,
        bars_by_symbol={},
        accounting_parameters=ACCOUNTING_PARAMETERS,
        operations_cancellations=(_operations_cancel(intent),),
    )

    payload = json.loads(path.read_text(encoding="utf-8"))
    cancellation_event = payload["events"][-1]
    cancellation = cancellation_event["payload"]
    cancellation["operations_state"] = "RUNNING"
    cancellation_identity = dict(cancellation)
    cancellation_identity.pop("cancellation_id")
    cancellation["cancellation_id"] = sha256_json(cancellation_identity)
    stable_event = dict(cancellation_event)
    stable_event.pop("event_id")
    cancellation_event["event_id"] = sha256_json(stable_event)
    stable_document = dict(payload)
    stable_document.pop("content_sha256")
    payload["content_sha256"] = sha256_json(stable_document)
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(
        ValueError,
        match="operations cancellation payload is invalid",
    ):
        load_human_paper_ledger(path)
