from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta
import json
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from chanlun.decision_support.fingerprints import sha256_json
from chanlun.decision_support.trading_system.forward_paper import (
    FORWARD_IMPLEMENTATION_PROVENANCE_SCHEMA,
    FORWARD_PAPER_CONTRACT_SCHEMA,
    CURRENT_HUMAN_REVIEW_SCREENING_PARAMETER_SET_ID,
    CURRENT_SIGNAL_ALIGNMENT_PARAMETER_SET_ID,
    CURRENT_STRATEGY_PARAMETER_SET_ID,
    _human_paper_entry_selection_gate_proven,
    append_forward_paper_event,
    audit_forward_implementation_continuity,
    audit_forward_paper_session_delivery as _audit_forward_paper_session_delivery,
    load_forward_paper_ledger,
    load_forward_contract,
)
from chanlun.decision_support.trading_system.trading_session import (
    build_trading_session_evidence,
)


CN = ZoneInfo("Asia/Shanghai")
PARAMETER_FIXTURE_ROOT = (
    Path(__file__).resolve().parents[1] / "fixtures" / "forward_paper"
)
PARAMETER_SOURCE = PARAMETER_FIXTURE_ROOT / "parameter_snapshot_human_review.json"


def test_forward_contract_fixtures_are_test_owned() -> None:
    """Historical-output cleanup must never delete forward contract fixtures."""

    for path in (PARAMETER_SOURCE,):
        assert path.is_file()
        assert "audit" not in path.parts
        assert path.is_relative_to(Path(__file__).resolve().parents[1])


def _selection_settlement(
    *,
    blocked: bool = False,
) -> dict[str, object]:
    intent_id = "sha256:" + "a" * 64
    blocked_ids = [intent_id] if blocked else []
    verified_ids = [] if blocked else [intent_id]
    source_hash = "sha256:" + "f" * 64
    ledger_hash = "sha256:" + "9" * 64
    source_objects = (
        []
        if blocked
        else [
            {
                "source_content_sha256": source_hash,
                "path": (
                    "objects/paper_entry_selection_source_report/"
                    + source_hash[7:]
                    + ".json"
                ),
                "file_sha256": "sha256:" + "1" * 64,
                "candidate_ids": ["sha256:" + "2" * 64],
                "verified_pending_buy_intent_ids": [intent_id],
                "live_status": "LIVE_DISABLED",
            }
        ]
    )
    gate = {
        "schema": "chanlun-human-paper-entry-selection-settlement-gate",
        "status": "BLOCKED" if blocked else "READY",
        "sector_catalog_ledger": "C:/test/qmt-sector-ledger.json",
        "sector_catalog_ledger_status": "VALID",
        "sector_catalog_ledger_content_sha256": "sha256:" + "b" * 64,
        "sector_catalog_ledger_error": None,
        "pending_buy_intent_count": 1,
        "pending_buy_intent_ids": [intent_id],
        "verified_pending_buy_intent_count": len(verified_ids),
        "verified_pending_buy_intent_ids": verified_ids,
        "blocked_pending_buy_intent_count": len(blocked_ids),
        "blocked_pending_buy_intent_ids": blocked_ids,
        "attestation_audit": {
            "schema": ("chanlun-human-paper-entry-selection-attestation-audit"),
            "status": "COMPLETE" if not blocked else "INVALID",
            "attested_buy_intent_count": 1,
            "verified_catalog_binding_count": len(verified_ids),
            "verified_buy_intent_ids": verified_ids,
            "catalog_unavailable_intent_ids": [],
            "invalid_attestations": (
                []
                if not blocked
                else [{"intent_id": intent_id, "reason": "TEST_INVALID"}]
            ),
            "selection_evidence_ids": (["sha256:" + "d" * 64] if not blocked else []),
            "catalog_entry_sha256s": (["sha256:" + "e" * 64] if not blocked else []),
            "exact_qmt_revision_name_and_membership_verified": not blocked,
            "tick_data_used": False,
            "broker_transport_available": False,
            "live_status": "LIVE_DISABLED",
        },
        "source_binding_audit": {
            "schema": "chanlun-human-paper-entry-selection-source-audit",
            "status": "COMPLETE" if not blocked else "INVALID",
            "required_live_ranked_buy_intent_count": 1 if not blocked else 0,
            "verified_source_binding_count": len(verified_ids),
            "verified_required_buy_intent_ids": verified_ids,
            "source_unavailable_intent_ids": [],
            "invalid_source_bindings": (
                []
                if not blocked
                else [{"intent_id": intent_id, "reason": "TEST_INVALID"}]
            ),
            "immutable_source_ranking_resolved": not blocked,
            "broker_transport_available": False,
            "live_status": "LIVE_DISABLED",
        },
        "source_report_archive": {
            "schema": "chanlun-human-paper-entry-source-report-archive",
            "status": ("NO_REQUIRED_SOURCE_REPORTS" if blocked else "COMPLETE"),
            "archive_performed": True,
            "required_source_report_count": 0 if blocked else 1,
            "required_source_content_sha256s": ([] if blocked else [source_hash]),
            "archived_source_report_count": len(source_objects),
            "objects": source_objects,
            "all_required_source_reports_archived": True,
            "broker_transport_available": False,
            "live_status": "LIVE_DISABLED",
        },
        "paper_ledger_prefix_archive": {
            "schema": "chanlun-human-paper-ledger-prefix-archive",
            "status": "COMPLETE",
            "archive_performed": True,
            "paper_ledger_content_sha256": ledger_hash,
            "path": ("objects/human_paper_ledger_prefix/" + ledger_hash[7:] + ".json"),
            "file_sha256": "sha256:" + "8" * 64,
            "event_count": 1,
            "last_event_id": intent_id,
            "broker_transport_available": False,
            "automated_order_authorized": False,
            "live_status": "LIVE_DISABLED",
        },
        "exact_qmt_sector_admission_required_before_virtual_buy_fill": True,
        "immutable_source_ranking_required_before_virtual_buy_fill": True,
        "paper_ledger_prefix_required_for_independent_replay": True,
        "blocked_buy_remains_pending": True,
        "persistent_sell_processing_continues": True,
        "tick_data_used": False,
        "broker_transport_available": False,
        "live_status": "LIVE_DISABLED",
    }
    return {
        "status": (
            "VIRTUAL_SETTLEMENT_BLOCKED_BY_ENTRY_SELECTION_EVIDENCE"
            if blocked
            else "VIRTUAL_SETTLEMENT_READY"
        ),
        "content_sha256": ledger_hash,
        "entry_selection_settlement_gate": gate,
    }


def test_entry_selection_settlement_gate_artifact_semantics() -> None:
    assert _human_paper_entry_selection_gate_proven(_selection_settlement())
    assert _human_paper_entry_selection_gate_proven(_selection_settlement(blocked=True))
    assert not _human_paper_entry_selection_gate_proven(
        {"status": "NO_PENDING_VIRTUAL_INTENTS"}
    )

    forged = json.loads(json.dumps(_selection_settlement()))
    forged["entry_selection_settlement_gate"]["blocked_pending_buy_intent_ids"] = [
        "sha256:" + "c" * 64
    ]
    assert not _human_paper_entry_selection_gate_proven(forged)

    for audit_name, field, bad in (
        ("attestation_audit", "attested_buy_intent_count", 2),
        ("attestation_audit", "verified_catalog_binding_count", 0),
        (
            "attestation_audit",
            "exact_qmt_revision_name_and_membership_verified",
            False,
        ),
        ("source_binding_audit", "required_live_ranked_buy_intent_count", 2),
        ("source_binding_audit", "verified_source_binding_count", 0),
        ("source_binding_audit", "immutable_source_ranking_resolved", False),
    ):
        forged = json.loads(json.dumps(_selection_settlement()))
        forged["entry_selection_settlement_gate"][audit_name][field] = bad
        assert not _human_paper_entry_selection_gate_proven(forged), (
            audit_name,
            field,
        )

    forged = json.loads(json.dumps(_selection_settlement()))
    source = forged["entry_selection_settlement_gate"]["source_binding_audit"]
    source["source_unavailable_intent_ids"] = ["sha256:" + "c" * 64]
    assert not _human_paper_entry_selection_gate_proven(forged)

    forged = json.loads(json.dumps(_selection_settlement()))
    forged["entry_selection_settlement_gate"]["source_report_archive"][
        "archived_source_report_count"
    ] = 0
    assert not _human_paper_entry_selection_gate_proven(forged)

    forged = json.loads(json.dumps(_selection_settlement()))
    forged["entry_selection_settlement_gate"]["paper_ledger_prefix_archive"][
        "event_count"
    ] = -1
    assert not _human_paper_entry_selection_gate_proven(forged)

    forged = json.loads(json.dumps(_selection_settlement()))
    del forged["entry_selection_settlement_gate"]["paper_ledger_prefix_archive"]
    assert not _human_paper_entry_selection_gate_proven(forged)


def _implementation_provenance(
    *,
    source_digit: str = "a",
) -> dict[str, object]:
    stable: dict[str, object] = {
        "schema": FORWARD_IMPLEMENTATION_PROVENANCE_SCHEMA,
        "application_source_revision": (
            source_digit * 40 + ".tree." + source_digit * 24
        ),
        "forward_scheduler_module_sha256": "sha256:" + "1" * 64,
        "forward_python_tool_sha256": "sha256:" + "2" * 64,
        "sector_capture_tool_sha256": "sha256:" + "3" * 64,
        "python_implementation": "CPython",
        "python_version": "3.11.0",
        "pandas_version": "2.0.0",
        "real_account_accessed": False,
        "real_order_transport_enabled": False,
        "live_status": "LIVE_DISABLED",
    }
    return {**stable, "content_sha256": sha256_json(stable)}


def audit_forward_paper_session_delivery(*args, **kwargs):
    """Give unproven delivery cases explicit calendar evidence."""

    session = kwargs["session"]
    observed_at = kwargs["observed_at"]
    kwargs.setdefault(
        "trading_session_evidence",
        build_trading_session_evidence(
            session=session,
            observed_at=observed_at,
            returned_sessions=() if session.weekday() >= 5 else (session,),
            published_through=None if session.weekday() >= 5 else session,
            query_attempted=session.weekday() < 5,
            query_succeeded=session.weekday() < 5,
        ),
    )
    return _audit_forward_paper_session_delivery(*args, **kwargs)


def test_current_forward_contract_has_no_order_authority() -> None:
    contract = load_forward_contract(PARAMETER_SOURCE)

    assert contract.strategy_parameter_set_id == CURRENT_STRATEGY_PARAMETER_SET_ID
    assert contract.strategic_frequency == "30m"
    assert contract.tactical_frequency == "5m"
    assert contract.segment_difference_frequency == "1m"
    assert contract.technical_mode == "HUMAN_REVIEW_SCREENING"
    assert (
        contract.signal_alignment_parameter_set_id
        == CURRENT_SIGNAL_ALIGNMENT_PARAMETER_SET_ID
    )
    assert (
        contract.human_review_screening_parameter_set_id
        == CURRENT_HUMAN_REVIEW_SCREENING_PARAMETER_SET_ID
    )
    assert contract.operational_status == "REVIEW_REQUIRED"
    assert contract.highest_status == "REVIEW_REQUIRED"
    assert contract.real_account_access is False
    assert contract.real_order_transport is False
    assert contract.live_status == "LIVE_DISABLED"
    assert contract.document()["schema"] == FORWARD_PAPER_CONTRACT_SCHEMA


def test_human_review_forward_ledger_preserves_review_required_status(
    tmp_path: Path,
) -> None:
    contract = load_forward_contract(PARAMETER_SOURCE)
    path = tmp_path / "human-review.json"

    ledger, event, reused = append_forward_paper_event(
        path,
        contract=contract,
        session=date(2026, 7, 28),
        phase="CONTROL",
        status="PAPER_STARTED",
        evidence={"automated_order_authorized": False},
        recorded_at=datetime(2026, 7, 28, 8, 0, tzinfo=CN),
    )

    assert reused is False
    assert event["paper_status"] == "REVIEW_REQUIRED"
    assert ledger["paper_status"] == "REVIEW_REQUIRED"
    assert load_forward_paper_ledger(path, contract=contract) == ledger


def test_forward_ledger_is_hash_chained_and_duplicate_safe(tmp_path: Path) -> None:
    contract = load_forward_contract(PARAMETER_SOURCE)
    ledger_path = tmp_path / "paper.json"
    recorded = datetime(2026, 7, 28, 9, 15, tzinfo=CN)

    first, first_event, reused = append_forward_paper_event(
        ledger_path,
        contract=contract,
        session=date(2026, 7, 28),
        phase="CAPTURE",
        status="CAPTURED",
        evidence={"receipt_sha256": "sha256:" + "1" * 64},
        recorded_at=recorded,
    )
    assert reused is False
    duplicate, same_event, reused = append_forward_paper_event(
        ledger_path,
        contract=contract,
        session=date(2026, 7, 28),
        phase="CAPTURE",
        status="CAPTURED",
        evidence={"receipt_sha256": "sha256:" + "1" * 64},
        recorded_at=recorded + timedelta(minutes=1),
    )
    assert reused is True
    assert duplicate == first
    assert same_event == first_event

    second, second_event, reused = append_forward_paper_event(
        ledger_path,
        contract=contract,
        session=date(2026, 7, 28),
        phase="DATA_GATE",
        status="DATA_BLOCKED",
        evidence={"reason": "ONE_MINUTE_SESSION_INCOMPLETE"},
        recorded_at=recorded + timedelta(hours=6),
    )
    assert reused is False
    assert second_event["previous_event_sha256"] == first_event["event_sha256"]
    assert load_forward_paper_ledger(ledger_path, contract=contract) == second


def test_forward_ledger_appends_recovery_after_a_later_phase_block(
    tmp_path: Path,
) -> None:
    contract = load_forward_contract(PARAMETER_SOURCE)
    path = tmp_path / "recovery.json"
    recorded = datetime(2026, 7, 28, 15, 20, tzinfo=CN)
    ready_evidence = {"gate": "READY"}

    append_forward_paper_event(
        path,
        contract=contract,
        session=date(2026, 7, 28),
        phase="DATA_GATE",
        status="DATA_READY",
        evidence=ready_evidence,
        recorded_at=recorded,
    )
    append_forward_paper_event(
        path,
        contract=contract,
        session=date(2026, 7, 28),
        phase="DATA_GATE",
        status="DATA_BLOCKED",
        evidence={"gate": "BLOCKED"},
        recorded_at=recorded + timedelta(minutes=1),
    )
    ledger, recovered, reused = append_forward_paper_event(
        path,
        contract=contract,
        session=date(2026, 7, 28),
        phase="DATA_GATE",
        status="DATA_READY",
        evidence=ready_evidence,
        recorded_at=recorded + timedelta(minutes=2),
    )

    assert reused is False
    assert len(ledger["events"]) == 3
    assert recovered == ledger["events"][-1]
    assert recovered["status"] == "DATA_READY"
    assert recovered["previous_event_sha256"] == ledger["events"][-2]["event_sha256"]


def test_forward_append_rejects_invalid_session_and_time_without_corruption(
    tmp_path: Path,
) -> None:
    contract = load_forward_contract(PARAMETER_SOURCE)
    path = tmp_path / "chronology.json"
    session = date(2026, 7, 28)
    recorded = datetime(2026, 7, 28, 10, 0, tzinfo=CN)
    original, _event, _reused = append_forward_paper_event(
        path,
        contract=contract,
        session=session,
        phase="CONTROL",
        status="PAPER_STARTED",
        evidence={"step": 1},
        recorded_at=recorded,
    )

    with pytest.raises(TypeError, match="session must be a date"):
        append_forward_paper_event(
            path,
            contract=contract,
            session=recorded,
            phase="CONTROL",
            status="PAPER_STARTED",
            evidence={"step": 2},
            recorded_at=recorded,
        )
    with pytest.raises(ValueError, match="predates its session"):
        append_forward_paper_event(
            path,
            contract=contract,
            session=session,
            phase="CONTROL",
            status="PAPER_STARTED",
            evidence={"step": 2},
            recorded_at=datetime(2026, 7, 27, 15, 0, tzinfo=CN),
        )
    with pytest.raises(ValueError, match="not chronological"):
        append_forward_paper_event(
            path,
            contract=contract,
            session=session,
            phase="CONTROL",
            status="PAPER_STARTED",
            evidence={"step": 2},
            recorded_at=recorded - timedelta(seconds=1),
        )

    assert load_forward_paper_ledger(path, contract=contract) == original


def test_forward_ledger_tampering_is_rejected(tmp_path: Path) -> None:
    contract = load_forward_contract(PARAMETER_SOURCE)
    path = tmp_path / "paper.json"
    append_forward_paper_event(
        path,
        contract=contract,
        session=date(2026, 7, 28),
        phase="CONTROL",
        status="PAPER_STARTED",
        evidence={"reason": "AUTHORIZED"},
        recorded_at=datetime(2026, 7, 28, 8, 0, tzinfo=CN),
    )
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["events"][0]["real_order_transport_enabled"] = True
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ValueError, match="event hash changed"):
        load_forward_paper_ledger(path, contract=contract)


def _rehash_forward_ledger(payload: dict[str, object]) -> None:
    previous: str | None = None
    for event in payload["events"]:
        event["previous_event_sha256"] = previous
        stable_event = {
            key: value for key, value in event.items() if key != "event_sha256"
        }
        event["event_sha256"] = sha256_json(stable_event)
        previous = event["event_sha256"]
    stable_ledger = {
        key: value for key, value in payload.items() if key != "content_sha256"
    }
    payload["content_sha256"] = sha256_json(stable_ledger)


def test_rehashed_ledger_still_rejects_evidence_and_safety_drift(
    tmp_path: Path,
) -> None:
    contract = load_forward_contract(PARAMETER_SOURCE)
    original = tmp_path / "original.json"
    append_forward_paper_event(
        original,
        contract=contract,
        session=date(2026, 7, 28),
        phase="CONTROL",
        status="PAPER_STARTED",
        evidence={"reason": "ORIGINAL"},
        recorded_at=datetime(2026, 7, 28, 8, 0, tzinfo=CN),
    )
    source = json.loads(original.read_text(encoding="utf-8"))

    changed_evidence = json.loads(json.dumps(source))
    changed_evidence["events"][0]["evidence"]["reason"] = "FORGED"
    _rehash_forward_ledger(changed_evidence)
    evidence_path = tmp_path / "evidence.json"
    evidence_path.write_text(json.dumps(changed_evidence), encoding="utf-8")
    with pytest.raises(ValueError, match="event evidence hash changed"):
        load_forward_paper_ledger(evidence_path, contract=contract)

    changed_safety = json.loads(json.dumps(source))
    changed_safety["events"][0]["live_status"] = "LIVE_ENABLED"
    _rehash_forward_ledger(changed_safety)
    safety_path = tmp_path / "safety.json"
    safety_path.write_text(json.dumps(changed_safety), encoding="utf-8")
    with pytest.raises(ValueError, match="event safety status changed"):
        load_forward_paper_ledger(safety_path, contract=contract)


def test_forward_ledger_rejects_malformed_implementation_provenance(
    tmp_path: Path,
) -> None:
    contract = load_forward_contract(PARAMETER_SOURCE)
    path = tmp_path / "malformed-provenance.json"
    with pytest.raises(
        ValueError,
        match="forward implementation provenance fields changed",
    ):
        append_forward_paper_event(
            path,
            contract=contract,
            session=date(2026, 7, 28),
            phase="CONTROL",
            status="PAPER_STARTED",
            evidence={"implementation_provenance": {"schema": "forged"}},
            recorded_at=datetime(2026, 7, 28, 8, 0, tzinfo=CN),
        )
    assert path.exists() is False


def test_forward_ledger_serializes_concurrent_writers(tmp_path: Path) -> None:
    contract = load_forward_contract(PARAMETER_SOURCE)
    path = tmp_path / "paper.json"
    recorded = datetime(2026, 7, 28, 9, 15, tzinfo=CN)

    def append(index: int) -> None:
        append_forward_paper_event(
            path,
            contract=contract,
            session=date(2026, 7, 28),
            phase="CAPTURE",
            status="CAPTURED",
            evidence={"receipt_sha256": "sha256:" + f"{index:064x}"},
            recorded_at=recorded,
        )

    with ThreadPoolExecutor(max_workers=8) as pool:
        tuple(pool.map(append, range(16)))

    ledger = load_forward_paper_ledger(path, contract=contract)
    assert len(ledger["events"]) == 16
    assert len({row["event_sha256"] for row in ledger["events"]}) == 16


def _forward_session_events(
    tmp_path: Path,
    *statuses: tuple,
    implementation_provenance: bool = True,
) -> tuple[dict[str, object], ...]:
    contract = load_forward_contract(PARAMETER_SOURCE)
    path = tmp_path / "delivery.json"
    ledger: dict[str, object] | None = None
    for item in statuses:
        phase, status, recorded_at = item[:3]
        evidence = dict(item[3]) if len(item) == 4 else {"status": status}
        if implementation_provenance:
            evidence.setdefault(
                "implementation_provenance",
                _implementation_provenance(),
            )
        ledger, _event, _reused = append_forward_paper_event(
            path,
            contract=contract,
            session=date(2026, 7, 30),
            phase=phase,
            status=status,
            evidence=evidence,
            recorded_at=recorded_at,
        )
    return () if ledger is None else tuple(ledger["events"])


def _capture_delivery_evidence() -> tuple[dict[str, object], dict[str, object]]:
    entry_sha256 = "sha256:" + "a" * 64
    captured_at = "2026-07-30T09:12:00+08:00"
    event_evidence = {
        "receipt": {
            "schema": "chanlun-qmt-sector-daily-capture-receipt",
            "capture_session": "2026-07-30",
            "captured_at": captured_at,
            "complete": True,
            "entry_sha256": entry_sha256,
            "highest_status": "PAPER_OBSERVATION",
            "historical_backfill_allowed": False,
            "real_account_accessed": False,
            "real_order_transport_enabled": False,
            "live_status": "LIVE_DISABLED",
        },
        "receipt_sha256": "sha256:" + "b" * 64,
        "sector_ledger_sha256": "sha256:" + "c" * 64,
    }
    readiness = {
        "schema": "chanlun-forward-sector-capture-readiness",
        "ready": True,
        "status": "ready",
        "reason_code": "READY",
        "session": "2026-07-30",
        "catalog_entry_sha256": entry_sha256,
        "catalog_captured_at": captured_at,
        "receipt_proven": True,
        "real_account_accessed": False,
        "real_order_transport_enabled": False,
        "live_status": "LIVE_DISABLED",
    }
    return event_evidence, readiness


def test_forward_implementation_continuity_preflight_is_fail_closed_and_recoverable(
    tmp_path: Path,
) -> None:
    session = date(2026, 7, 30)
    current = _implementation_provenance()

    missing = audit_forward_implementation_continuity(
        (),
        session=session,
        current_implementation_provenance=current,
    )
    assert missing["ready"] is False
    assert missing["reason_code"] == "CAPTURE_EVENT_MISSING"
    assert missing["market_data_read_authorized"] is False

    unattested = _forward_session_events(
        tmp_path / "unattested-preflight",
        ("CAPTURE", "CAPTURED", datetime(2026, 7, 30, 9, 12, tzinfo=CN)),
        implementation_provenance=False,
    )
    unattested_result = audit_forward_implementation_continuity(
        unattested,
        session=session,
        current_implementation_provenance=current,
    )
    assert unattested_result["reason_code"] == (
        "CAPTURE_IMPLEMENTATION_PROVENANCE_UNATTESTED"
    )

    captured = _forward_session_events(
        tmp_path / "captured-preflight",
        ("CAPTURE", "CAPTURED", datetime(2026, 7, 30, 9, 12, tzinfo=CN)),
    )
    matching = audit_forward_implementation_continuity(
        captured,
        session=session,
        current_implementation_provenance=current,
    )
    assert matching["ready"] is True
    assert matching["reason_code"] == "READY"
    assert matching["same_implementation_as_capture"] is True
    assert matching["market_data_read_authorized"] is True

    changed = audit_forward_implementation_continuity(
        captured,
        session=session,
        current_implementation_provenance=_implementation_provenance(source_digit="b"),
    )
    assert changed["ready"] is False
    assert changed["reason_code"] == "IMPLEMENTATION_CHANGED_SINCE_CAPTURE"
    assert changed["same_implementation_as_capture"] is False
    assert changed["capture_event_sha256"] == captured[-1]["event_sha256"]
    assert changed["content_sha256"] == sha256_json(
        {key: value for key, value in changed.items() if key != "content_sha256"}
    )

    restored = audit_forward_implementation_continuity(
        captured,
        session=session,
        current_implementation_provenance=current,
    )
    assert restored == matching


def test_forward_implementation_continuity_rejects_rehashed_malformed_provenance(
    tmp_path: Path,
) -> None:
    events = _forward_session_events(
        tmp_path,
        ("CAPTURE", "CAPTURED", datetime(2026, 7, 30, 9, 12, tzinfo=CN)),
    )
    forged = _implementation_provenance()
    forged["real_order_transport_enabled"] = True
    forged["content_sha256"] = sha256_json(
        {key: value for key, value in forged.items() if key != "content_sha256"}
    )

    with pytest.raises(ValueError, match="safety changed"):
        audit_forward_implementation_continuity(
            events,
            session=date(2026, 7, 30),
            current_implementation_provenance=forged,
        )


def _data_ready_evidence() -> dict[str, object]:
    return {
        "session": "2026-07-30",
        "sector_catalog_entry_sha256": "sha256:" + "a" * 64,
        "sector_capture_at": "2026-07-30T09:12:00+08:00",
        "reason_codes": (),
        "market_data_gate": {
            "complete": True,
            "session": "2026-07-30",
            "minimum_market_data_frequency": "1m",
            "market_data_was_synthesized": False,
            "tick_data_used": False,
            "real_account_accessed": False,
            "real_order_transport_enabled": False,
            "reason_codes": (),
            "frequencies": {
                "1m": {
                    "row_count": 240,
                    "minimum_rows": 240,
                    "first_at": "2026-07-30T09:31:00+08:00",
                    "last_at": "2026-07-30T15:00:00+08:00",
                    "expected_last_at": "2026-07-30T15:00:00+08:00",
                },
                "5m": {
                    "row_count": 48,
                    "minimum_rows": 48,
                    "first_at": "2026-07-30T09:35:00+08:00",
                    "last_at": "2026-07-30T15:00:00+08:00",
                    "expected_last_at": "2026-07-30T15:00:00+08:00",
                },
            },
        },
    }


def test_forward_delivery_distinguishes_not_due_missing_and_pending(
    tmp_path: Path,
) -> None:
    session = date(2026, 7, 30)

    not_due = audit_forward_paper_session_delivery(
        (),
        session=session,
        observed_at=datetime(2026, 7, 30, 9, 9, tzinfo=CN),
    )
    assert not_due["ready"] is False
    assert not_due["status"] == "not_due"
    assert not_due["reason_code"] == "CAPTURE_NOT_DUE"

    missing = audit_forward_paper_session_delivery(
        (),
        session=session,
        observed_at=datetime(2026, 7, 30, 9, 11, tzinfo=CN),
    )
    assert missing["status"] == "not_ready"
    assert missing["reason_code"] == "CAPTURE_MISSING_AFTER_DUE"

    events = _forward_session_events(
        tmp_path,
        ("CAPTURE", "CAPTURED", datetime(2026, 7, 30, 9, 12, tzinfo=CN)),
    )
    waiting = audit_forward_paper_session_delivery(
        events,
        session=session,
        observed_at=datetime(2026, 7, 30, 15, 19, tzinfo=CN),
    )
    assert waiting["status"] == "pending"
    assert waiting["reason_code"] == "CAPTURED_WAITING_FOR_EVALUATION"
    assert waiting["capture_event_present"] is True
    assert waiting["capture_ready"] is False
    assert waiting["evaluation_ready"] is False

    in_grace = audit_forward_paper_session_delivery(
        events,
        session=session,
        observed_at=datetime(2026, 7, 30, 22, 59, tzinfo=CN),
    )
    assert in_grace["status"] == "pending"
    assert in_grace["reason_code"] == "EVALUATION_PENDING"

    overdue = audit_forward_paper_session_delivery(
        events,
        session=session,
        observed_at=datetime(2026, 7, 30, 23, 1, tzinfo=CN),
    )
    assert overdue["status"] == "not_ready"
    assert overdue["reason_code"] == "EVALUATION_MISSING_AFTER_DEADLINE"


def test_forward_delivery_requires_both_capture_and_evaluated_events(
    tmp_path: Path,
) -> None:
    session = date(2026, 7, 30)
    events = _forward_session_events(
        tmp_path,
        ("CAPTURE", "CAPTURED", datetime(2026, 7, 30, 9, 12, tzinfo=CN)),
        ("DATA_GATE", "DATA_READY", datetime(2026, 7, 30, 15, 23, tzinfo=CN)),
        ("DECISION", "EVALUATION_BLOCKED", datetime(2026, 7, 30, 15, 24, tzinfo=CN)),
    )
    blocked = audit_forward_paper_session_delivery(
        events,
        session=session,
        observed_at=datetime(2026, 7, 30, 15, 25, tzinfo=CN),
    )
    assert blocked["ready"] is False
    assert blocked["reason_code"] == "EVALUATION_BLOCKED"
    assert blocked["latest_terminal_event_status"] == "EVALUATION_BLOCKED"

    retry_events = _forward_session_events(
        tmp_path / "retry",
        ("CAPTURE", "CAPTURED", datetime(2026, 7, 30, 9, 12, tzinfo=CN)),
        ("DATA_GATE", "DATA_BLOCKED", datetime(2026, 7, 30, 15, 21, tzinfo=CN)),
        ("DATA_GATE", "DATA_READY", datetime(2026, 7, 30, 15, 22, tzinfo=CN)),
    )
    retry_pending = audit_forward_paper_session_delivery(
        retry_events,
        session=session,
        observed_at=datetime(2026, 7, 30, 15, 23, tzinfo=CN),
    )
    assert retry_pending["status"] == "pending"
    assert retry_pending["reason_code"] == "EVALUATION_PENDING"

    capture_evidence, capture_readiness = _capture_delivery_evidence()
    complete_events = _forward_session_events(
        tmp_path / "complete",
        (
            "CAPTURE",
            "CAPTURED",
            datetime(2026, 7, 30, 9, 12, tzinfo=CN),
            capture_evidence,
        ),
        (
            "DATA_GATE",
            "DATA_READY",
            datetime(2026, 7, 30, 15, 23, tzinfo=CN),
            _data_ready_evidence(),
        ),
        ("DECISION", "EVALUATED", datetime(2026, 7, 30, 15, 24, tzinfo=CN)),
    )
    complete = audit_forward_paper_session_delivery(
        complete_events,
        session=session,
        observed_at=datetime(2026, 7, 30, 15, 25, tzinfo=CN),
        sector_capture_readiness=capture_readiness,
    )
    assert complete["ready"] is False
    assert complete["status"] == "not_ready"
    assert complete["reason_code"] == "EVALUATION_ARTIFACTS_UNAVAILABLE"
    assert complete["capture_ready"] is True
    assert complete["evaluation_event_present"] is True
    assert complete["evaluation_ready"] is False
    assert complete["capture_evidence_proven"] is True
    assert complete["data_ready_evidence_proven"] is True
    assert complete["evaluation_artifacts_proven"] is False
    assert complete["implementation_provenance_present"] is True
    assert complete["implementation_provenance_proven"] is True
    assert (
        complete["capture_implementation_provenance_id"]
        == (_implementation_provenance()["content_sha256"])
    )
    assert complete["real_account_accessed"] is False
    assert complete["real_order_transport_enabled"] is False
    assert complete["live_status"] == "LIVE_DISABLED"


def test_forward_delivery_rejects_unsupported_and_mixed_implementation_provenance(
    tmp_path: Path,
) -> None:
    capture_evidence, capture_readiness = _capture_delivery_evidence()
    statuses = (
        (
            "CAPTURE",
            "CAPTURED",
            datetime(2026, 7, 30, 9, 12, tzinfo=CN),
            capture_evidence,
        ),
        (
            "DATA_GATE",
            "DATA_READY",
            datetime(2026, 7, 30, 15, 23, tzinfo=CN),
            _data_ready_evidence(),
        ),
        (
            "DECISION",
            "EVALUATED",
            datetime(2026, 7, 30, 15, 24, tzinfo=CN),
        ),
    )
    unattested = _forward_session_events(
        tmp_path / "unattested",
        *statuses,
        implementation_provenance=False,
    )
    unattested_result = audit_forward_paper_session_delivery(
        unattested,
        session=date(2026, 7, 30),
        observed_at=datetime(2026, 7, 30, 15, 25, tzinfo=CN),
        sector_capture_readiness=capture_readiness,
    )
    assert unattested_result["ready"] is False
    assert unattested_result["reason_code"] == ("IMPLEMENTATION_PROVENANCE_UNATTESTED")
    assert unattested_result["implementation_provenance_present"] is False
    assert unattested_result["implementation_provenance_proven"] is False

    mixed_statuses = list(statuses)
    mixed_statuses[-1] = (
        "DECISION",
        "EVALUATED",
        datetime(2026, 7, 30, 15, 24, tzinfo=CN),
        {"implementation_provenance": _implementation_provenance(source_digit="b")},
    )
    mixed = _forward_session_events(tmp_path / "mixed", *mixed_statuses)
    mixed_result = audit_forward_paper_session_delivery(
        mixed,
        session=date(2026, 7, 30),
        observed_at=datetime(2026, 7, 30, 15, 25, tzinfo=CN),
        sector_capture_readiness=capture_readiness,
    )
    assert mixed_result["ready"] is False
    assert mixed_result["reason_code"] == "MIXED_IMPLEMENTATION_PROVENANCE"
    assert mixed_result["implementation_provenance_present"] is True
    assert mixed_result["implementation_provenance_proven"] is False


def test_forward_delivery_rejects_an_evaluation_without_capture(
    tmp_path: Path,
) -> None:
    events = _forward_session_events(
        tmp_path,
        ("DECISION", "EVALUATED", datetime(2026, 7, 30, 15, 24, tzinfo=CN)),
    )

    result = audit_forward_paper_session_delivery(
        events,
        session=date(2026, 7, 30),
        observed_at=datetime(2026, 7, 30, 15, 25, tzinfo=CN),
    )

    assert result["ready"] is False
    assert result["reason_code"] == "EVALUATED_WITHOUT_CAPTURE_EVENT"


def test_forward_delivery_rejects_missing_data_ready_and_rehashed_self_reports(
    tmp_path: Path,
) -> None:
    capture_evidence, capture_readiness = _capture_delivery_evidence()
    no_data_ready = _forward_session_events(
        tmp_path / "no-data",
        (
            "CAPTURE",
            "CAPTURED",
            datetime(2026, 7, 30, 9, 12, tzinfo=CN),
            capture_evidence,
        ),
        ("DECISION", "EVALUATED", datetime(2026, 7, 30, 15, 24, tzinfo=CN)),
    )
    missing = audit_forward_paper_session_delivery(
        no_data_ready,
        session=date(2026, 7, 30),
        observed_at=datetime(2026, 7, 30, 15, 25, tzinfo=CN),
        sector_capture_readiness=capture_readiness,
    )
    assert missing["ready"] is False
    assert missing["reason_code"] == "DATA_READY_EVENT_MISSING"

    mismatched = _data_ready_evidence()
    mismatched["session"] = "2026-07-29"
    fake_chain = _forward_session_events(
        tmp_path / "mismatch",
        (
            "CAPTURE",
            "CAPTURED",
            datetime(2026, 7, 30, 9, 12, tzinfo=CN),
            capture_evidence,
        ),
        (
            "DATA_GATE",
            "DATA_READY",
            datetime(2026, 7, 30, 15, 23, tzinfo=CN),
            mismatched,
        ),
        ("DECISION", "EVALUATED", datetime(2026, 7, 30, 15, 24, tzinfo=CN)),
    )
    invalid = audit_forward_paper_session_delivery(
        fake_chain,
        session=date(2026, 7, 30),
        observed_at=datetime(2026, 7, 30, 15, 25, tzinfo=CN),
        sector_capture_readiness=capture_readiness,
    )
    assert invalid["ready"] is False
    assert invalid["reason_code"] == "DATA_READY_EVENT_EVIDENCE_INVALID"

    oversized = _data_ready_evidence()
    oversized["market_data_gate"]["frequencies"]["1m"]["row_count"] = 241
    oversized_events = _forward_session_events(
        tmp_path / "oversized-grid",
        (
            "CAPTURE",
            "CAPTURED",
            datetime(2026, 7, 30, 9, 12, tzinfo=CN),
            capture_evidence,
        ),
        (
            "DATA_GATE",
            "DATA_READY",
            datetime(2026, 7, 30, 15, 23, tzinfo=CN),
            oversized,
        ),
        ("DECISION", "EVALUATED", datetime(2026, 7, 30, 15, 24, tzinfo=CN)),
    )
    invalid_grid = audit_forward_paper_session_delivery(
        oversized_events,
        session=date(2026, 7, 30),
        observed_at=datetime(2026, 7, 30, 15, 25, tzinfo=CN),
        sector_capture_readiness=capture_readiness,
    )
    assert invalid_grid["reason_code"] == "DATA_READY_EVENT_EVIDENCE_INVALID"

    blocked_after_ready = _forward_session_events(
        tmp_path / "blocked-after-ready",
        (
            "CAPTURE",
            "CAPTURED",
            datetime(2026, 7, 30, 9, 12, tzinfo=CN),
            capture_evidence,
        ),
        (
            "DATA_GATE",
            "DATA_READY",
            datetime(2026, 7, 30, 15, 22, tzinfo=CN),
            _data_ready_evidence(),
        ),
        (
            "DATA_GATE",
            "DATA_BLOCKED",
            datetime(2026, 7, 30, 15, 23, tzinfo=CN),
        ),
        ("DECISION", "EVALUATED", datetime(2026, 7, 30, 15, 24, tzinfo=CN)),
    )
    invalid_retry = audit_forward_paper_session_delivery(
        blocked_after_ready,
        session=date(2026, 7, 30),
        observed_at=datetime(2026, 7, 30, 15, 25, tzinfo=CN),
        sector_capture_readiness=capture_readiness,
    )
    assert invalid_retry["reason_code"] == "FORWARD_EVENT_SEQUENCE_INVALID"


def test_forward_delivery_weekend_is_never_required_or_promoted(
    tmp_path: Path,
) -> None:
    weekend = date(2026, 8, 1)
    events = _forward_session_events(
        tmp_path,
        ("DECISION", "EVALUATED", datetime(2026, 7, 30, 15, 24, tzinfo=CN)),
    )
    forged = [dict(events[0], session=weekend.isoformat())]

    result = audit_forward_paper_session_delivery(
        forged,
        session=weekend,
        observed_at=datetime(2026, 8, 1, 20, 0, tzinfo=CN),
    )

    assert result["required"] is False
    assert result["ready"] is False
    assert result["reason_code"] == "NON_TRADING_SESSION_NOT_DUE"
    assert result["requirement_resolved"] is True
    assert result["trading_session_status"] == "NON_TRADING_SESSION"
