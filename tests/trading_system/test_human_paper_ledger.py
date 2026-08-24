from __future__ import annotations

from dataclasses import replace
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
    audit_human_paper_portfolio_decisions,
    audit_human_paper_portfolio_fill_decisions,
    load_human_paper_accounting_parameters,
)
from chanlun.decision_support.trading_system.human_paper_ledger import (
    HumanPaperEntrySelectionEvidence,
    HumanPaperMinuteBar,
    HumanPaperOperationsCancellation,
    audit_human_paper_entry_boundary_attestations,
    audit_human_paper_entry_selection_attestations,
    audit_human_paper_entry_selection_source_bindings,
    audit_human_paper_pending_continuity,
    append_human_paper_intent,
    build_human_paper_intent,
    human_paper_consumed_signal_lifecycle_ids,
    human_paper_position_quantities,
    human_paper_portfolio_rejected_intent_ids,
    human_paper_terminal_intent_ids,
    latest_human_paper_pending_continuity,
    load_human_paper_ledger,
    reconcile_human_paper_feedback,
    settle_human_paper_intents_with_portfolio_controls,
)
from chanlun.decision_support.trading_system.models import EntryExecutionBoundary
from chanlun.decision_support.trading_system.human_review_screening import (
    HumanReviewAlert,
    HumanReviewFeedback,
    MONITOR_ONLY_WARNING_CODE,
    SectorRankingReviewEvidence,
    human_review_screening_parameters,
)
from chanlun.decision_support.trading_system.qmt_sector_ledger import (
    catalog_capture_entry,
)


TZ = ZoneInfo("Asia/Shanghai")
REVIEWED_AT = datetime(2026, 7, 28, 10, 0, tzinfo=TZ)
PARAMETER_SNAPSHOT = (
    Path(__file__).resolve().parents[1]
    / "fixtures"
    / "forward_paper"
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
        signal_alignment_parameter_set_id=(
            human_review_screening_parameters().signal_alignment_parameter_set_id
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
        {"schema": "chanlun-qmt-gics3-catalog", "sectors": sectors}
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


def test_monitor_only_buy_cannot_create_a_pending_virtual_entry() -> None:
    """Sector-first selection cannot be bypassed through human paper feedback."""

    alert = replace(
        _alert(),
        warning_codes=(MONITOR_ONLY_WARNING_CODE,),
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
        warning_codes=(MONITOR_ONLY_WARNING_CODE,),
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
        setup_occurrence_id="setup-occurrence:paper-ledger-test",
        point_id="sha256:" + "9" * 64,
        source_frequency="1m",
        confirmation_bar_closed_at=REVIEWED_AT,
        raw_open=Decimal("10.00"),
        raw_high=Decimal("10.05"),
        raw_low=Decimal("9.98"),
        raw_close=Decimal("10.03"),
        raw_volume=Decimal("10000"),
        entry_valid_until=REVIEWED_AT + timedelta(minutes=1),
        raw_price_basis_revision="qmt-none-test",
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
            "schema": "chanlun-qmt-gics3-catalog",
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


def test_current_intent_identity_covers_the_complete_document() -> None:
    intent = build_human_paper_intent(feedback=_feedback(_alert()), alert=_alert())
    assert intent is not None
    document = intent.document()
    assert document["entry_selection_evidence"] is None
    stable = dict(document)
    stable.pop("intent_id")
    assert intent.intent_id == sha256_json(stable)


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
    # Simulate a pre-existing pending intent. New feedback creation is
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


def test_non_green_sector_gate_blocks_virtual_buy_intent() -> None:
    alert = _alert(sector_gate="UNRESOLVED")
    intent = build_human_paper_intent(feedback=_feedback(alert), alert=alert)

    assert intent is not None
    assert intent.status == "BLOCKED_BY_RISK_GATE"
    assert "SECTOR_GATE_UNRESOLVED" in intent.reason_codes


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
        "schema": "chanlun-human-paper-execution-facts",
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
        "schema": "chanlun-human-paper-execution-evidence",
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
