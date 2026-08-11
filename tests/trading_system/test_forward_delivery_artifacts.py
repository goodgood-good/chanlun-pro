from __future__ import annotations

from datetime import date, datetime
import hashlib
import json
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

import chanlun.decision_support.trading_system.human_paper_valuation as valuation_module
import chanlun.decision_support.trading_system.human_review_screening as review_module
import chanlun.decision_support.trading_system.live_human_review as live_module
from chanlun.decision_support.fingerprints import sha256_json
from chanlun.decision_support.trading_system.forward_paper import (
    FORWARD_IMPLEMENTATION_PROVENANCE_SCHEMA,
    append_forward_paper_event,
    audit_forward_paper_session_delivery as _audit_forward_paper_session_delivery,
    load_frozen_forward_contract,
)
from chanlun.decision_support.trading_system.human_paper_ledger import (
    LEDGER_SCHEMA,
    PAPER_CONTRACT_ID,
)
from chanlun.decision_support.trading_system.trading_session import (
    build_trading_session_evidence,
)


CN = ZoneInfo("Asia/Shanghai")
SESSION = date(2026, 7, 30)
PARAMETERS = (
    Path(__file__).resolve().parents[2]
    / "audit"
    / "chanlun_trading_system_backtest"
    / "recent_year_current_sector_no3p"
    / "parameter_snapshot_human_review.json"
)


def audit_forward_paper_session_delivery(*args, **kwargs):
    session = kwargs["session"]
    observed_at = kwargs["observed_at"]
    kwargs.setdefault(
        "trading_session_evidence",
        build_trading_session_evidence(
            session=session,
            observed_at=observed_at,
            returned_sessions=(session,),
            published_through=session,
            query_attempted=True,
            query_succeeded=True,
        ),
    )
    return _audit_forward_paper_session_delivery(*args, **kwargs)


def _document(stable: dict[str, object]) -> dict[str, object]:
    return {**stable, "content_sha256": sha256_json(stable)}


def _implementation_provenance() -> dict[str, object]:
    return _document(
        {
            "schema": FORWARD_IMPLEMENTATION_PROVENANCE_SCHEMA,
            "application_source_revision": "a" * 40 + ".tree." + "b" * 24,
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
    )


def _write_file_object(
    session_root: Path,
    *,
    kind: str,
    payload: dict[str, object],
) -> tuple[Path, str]:
    raw = (
        json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    ).encode("utf-8")
    file_sha256 = "sha256:" + hashlib.sha256(raw).hexdigest()
    path = session_root / "objects" / kind / f"{file_sha256[7:]}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(raw)
    return path, file_sha256


def _write_semantic_object(
    session_root: Path,
    *,
    kind: str,
    payload: dict[str, object],
) -> tuple[Path, str]:
    content_sha256 = str(payload["content_sha256"])
    raw = (
        json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    ).encode("utf-8")
    file_sha256 = "sha256:" + hashlib.sha256(raw).hexdigest()
    path = session_root / "objects" / kind / f"{content_sha256[7:]}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(raw)
    return path, file_sha256


def _capture_evidence() -> tuple[dict[str, object], dict[str, object]]:
    entry_sha256 = "sha256:" + "a" * 64
    captured_at = "2026-07-30T09:12:00+08:00"
    evidence = {
        "receipt": {
            "schema": "chanlun-qmt-sector-daily-capture-receipt",
            "capture_session": SESSION.isoformat(),
            "captured_at": captured_at,
            "complete": True,
            "entry_sha256": entry_sha256,
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
        "reason_code": "READY",
        "session": SESSION.isoformat(),
        "catalog_entry_sha256": entry_sha256,
        "catalog_captured_at": captured_at,
        "receipt_proven": True,
        "real_account_accessed": False,
        "real_order_transport_enabled": False,
        "live_status": "LIVE_DISABLED",
    }
    return evidence, readiness


def _data_ready_evidence() -> dict[str, object]:
    return {
        "session": SESSION.isoformat(),
        "sector_catalog_entry_sha256": "sha256:" + "a" * 64,
        "sector_capture_at": "2026-07-30T09:12:00+08:00",
        "reason_codes": (),
        "market_data_gate": {
            "complete": True,
            "session": SESSION.isoformat(),
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


def _events(
    path: Path,
    *,
    evaluation_evidence: dict[str, object],
) -> tuple[dict[str, object], ...]:
    contract = load_frozen_forward_contract(PARAMETERS)
    capture, _readiness = _capture_evidence()
    ledger = None
    for phase, status, recorded_at, evidence in (
        (
            "CAPTURE",
            "CAPTURED",
            datetime(2026, 7, 30, 9, 12, tzinfo=CN),
            capture,
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
            evaluation_evidence,
        ),
    ):
        event_evidence = dict(evidence)
        event_evidence["implementation_provenance"] = _implementation_provenance()
        ledger, _event, _reused = append_forward_paper_event(
            path,
            contract=contract,
            session=SESSION,
            phase=phase,
            status=status,
            evidence=event_evidence,
            recorded_at=recorded_at,
        )
    assert ledger is not None
    return tuple(ledger["events"])


def _artifact_evidence(forward_root: Path) -> dict[str, object]:
    contract = load_frozen_forward_contract(PARAMETERS)
    session_root = forward_root / "sessions" / SESSION.isoformat()
    source_content_sha256 = "sha256:" + "1" * 64
    screening_policy_id = "sha256:" + "2" * 64
    decision_core_id = "sha256:" + "3" * 64
    paper_ledger_stable: dict[str, object] = {
        "schema": LEDGER_SCHEMA,
        "paper_contract_id": PAPER_CONTRACT_ID,
        "events": [],
        "broker_transport_available": False,
        "automated_order_authorized": False,
        "live_status": "LIVE_DISABLED",
    }
    paper_ledger_content_sha256 = sha256_json(paper_ledger_stable)
    paper_ledger = {
        **paper_ledger_stable,
        "content_sha256": paper_ledger_content_sha256,
    }
    paper_ledger_path, paper_ledger_file_sha256 = _write_semantic_object(
        session_root,
        kind="human_paper_ledger_prefix",
        payload=paper_ledger,
    )
    accounting_content_sha256 = "sha256:" + "5" * 64
    live = _document(
        {
            "schema": "chanlun-forward-live-screening-snapshot",
            "session": SESSION.isoformat(),
            "contract_id": contract.contract_id,
            "strategy_parameter_set_id": contract.strategy_parameter_set_id,
            "source_content_sha256": source_content_sha256,
            "screening_policy_id": screening_policy_id,
            "decision_core_id": decision_core_id,
            "highest_status": "REVIEW_REQUIRED",
            "human_confirmation_required": True,
            "automated_order_authorized": False,
            "orders_created": 0,
            "fills_created": 0,
            "positions_created": 0,
            "candidate_count": 0,
            "live_status": "LIVE_DISABLED",
            "snapshot": {"test_fixture": True},
        }
    )
    live_path, live_file_sha256 = _write_file_object(
        session_root,
        kind="forward_live_screening_snapshot",
        payload=live,
    )
    review = _document(
        {
            "forward_paper_session": SESSION.isoformat(),
            "input_hashes": {
                "live_screening_snapshot": source_content_sha256,
            },
            "review_queue": [],
        }
    )
    review_path, review_file_sha256 = _write_file_object(
        session_root,
        kind="forward_human_review_screen",
        payload=review,
    )
    valuation = _document(
        {
            "session": SESSION.isoformat(),
            "all_complete": True,
            "equity_curve_point_available": True,
            "paper_ledger_content_sha256": paper_ledger_content_sha256,
            "accounting_content_sha256": accounting_content_sha256,
        }
    )
    valuation_path = (
        session_root
        / "objects"
        / "paper_valuation"
        / f"{str(valuation['content_sha256'])[7:]}.json"
    )
    valuation_path.parent.mkdir(parents=True, exist_ok=True)
    valuation_path.write_text(
        json.dumps(valuation, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    live_identity = {
        "path": str(live_path.relative_to(session_root)),
        "file_sha256": live_file_sha256,
        "content_sha256": live["content_sha256"],
    }
    review_identity = {
        "path": str(review_path.relative_to(session_root)),
        "file_sha256": review_file_sha256,
        "content_sha256": review["content_sha256"],
    }
    attempt_identity = {
        "schema": "chanlun-forward-evaluation-attempt",
        "session": SESSION.isoformat(),
        "contract_id": contract.contract_id,
        "strategy_parameter_set_id": contract.strategy_parameter_set_id,
        "screening_policy_id": screening_policy_id,
        "decision_core_id": decision_core_id,
        "source_content_sha256": source_content_sha256,
        "live_object": live_identity,
        "human_review_object": review_identity,
        "candidate_count": 0,
        "scanner_error_count": 0,
        "highest_status": "REVIEW_REQUIRED",
        "live_status": "LIVE_DISABLED",
    }
    attempt_id = sha256_json(attempt_identity)
    attempt = {
        **attempt_identity,
        "attempt_id": attempt_id,
    }
    receipt = _document({**attempt, "promoted_sample": True})
    receipt_path, receipt_file_sha256 = _write_file_object(
        session_root,
        kind="forward_evaluation_attempt",
        payload=receipt,
    )
    manifest = _document(
        {
            "schema": "chanlun-forward-session-manifest",
            "session": SESSION.isoformat(),
            "contract_id": contract.contract_id,
            "strategy_parameter_set_id": contract.strategy_parameter_set_id,
            "attempts": [attempt],
            "attempt_count": 1,
            "promoted_attempt_id": attempt_id,
            "promoted_screening_policy_id": screening_policy_id,
            "promoted_decision_core_id": decision_core_id,
            "promoted_sample_count": 1,
            "promotion_policy": "FIRST_VALID_EVALUATION_ONLY",
            "highest_status": "REVIEW_REQUIRED",
            "live_status": "LIVE_DISABLED",
        }
    )
    manifest_path = session_root / "forward_session_manifest.json"
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    return {
        "session_manifest": str(manifest_path),
        "session_manifest_revision": manifest["content_sha256"],
        "session_attempt_count": 1,
        "promoted_sample_count": 1,
        "promoted_sample": True,
        "attempt_id": attempt_id,
        "attempt_receipt": str(receipt_path),
        "attempt_receipt_sha256": receipt_file_sha256,
        "screening_policy_id": screening_policy_id,
        "decision_core_id": decision_core_id,
        "result": str(live_path),
        "result_sha256": live_file_sha256,
        "content_sha256": live["content_sha256"],
        "human_review_result": str(review_path),
        "human_review_result_sha256": review_file_sha256,
        "human_review_content_sha256": review["content_sha256"],
        "source_content_sha256": source_content_sha256,
        "candidate_count": 0,
        "human_review_candidate_count": 0,
        "orders_created": 0,
        "fills_created": 0,
        "human_confirmation_required": True,
        "human_paper_settlement": {
            "status": "NO_PENDING_VIRTUAL_INTENTS",
            "content_sha256": paper_ledger_content_sha256,
            "entry_selection_settlement_gate": {
                "schema": "chanlun-human-paper-entry-selection-settlement-gate",
                "status": "NO_PENDING_BUYS",
                "sector_catalog_ledger": "C:/test/qmt-sector-ledger.json",
                "sector_catalog_ledger_status": "VALID",
                "sector_catalog_ledger_content_sha256": "sha256:" + "b" * 64,
                "sector_catalog_ledger_error": None,
                "pending_buy_intent_count": 0,
                "pending_buy_intent_ids": [],
                "verified_pending_buy_intent_count": 0,
                "verified_pending_buy_intent_ids": [],
                "blocked_pending_buy_intent_count": 0,
                "blocked_pending_buy_intent_ids": [],
                "attestation_audit": {
                    "schema": "chanlun-human-paper-entry-selection-attestation-audit",
                    "status": "NO_SELECTION_ATTESTATIONS",
                    "attested_buy_intent_count": 0,
                    "verified_catalog_binding_count": 0,
                    "verified_buy_intent_ids": [],
                    "catalog_unavailable_intent_ids": [],
                    "invalid_attestations": [],
                    "selection_evidence_ids": [],
                    "catalog_entry_sha256s": [],
                    "exact_qmt_revision_name_and_membership_verified": True,
                    "tick_data_used": False,
                    "broker_transport_available": False,
                    "live_status": "LIVE_DISABLED",
                },
                "source_binding_audit": {
                    "schema": "chanlun-human-paper-entry-selection-source-audit",
                    "status": "NO_REQUIRED_SELECTION_INTENTS",
                    "required_live_ranked_buy_intent_count": 0,
                    "verified_source_binding_count": 0,
                    "verified_required_buy_intent_ids": [],
                    "source_unavailable_intent_ids": [],
                    "invalid_source_bindings": [],
                    "immutable_source_ranking_resolved": True,
                    "broker_transport_available": False,
                    "live_status": "LIVE_DISABLED",
                },
                "source_report_archive": {
                    "schema": "chanlun-human-paper-entry-source-report-archive",
                    "status": "NO_REQUIRED_SOURCE_REPORTS",
                    "archive_performed": True,
                    "required_source_report_count": 0,
                    "required_source_content_sha256s": [],
                    "archived_source_report_count": 0,
                    "objects": [],
                    "all_required_source_reports_archived": True,
                    "broker_transport_available": False,
                    "live_status": "LIVE_DISABLED",
                },
                "paper_ledger_prefix_archive": {
                    "schema": "chanlun-human-paper-ledger-prefix-archive",
                    "status": "COMPLETE",
                    "archive_performed": True,
                    "paper_ledger_content_sha256": paper_ledger_content_sha256,
                    "path": str(paper_ledger_path.relative_to(session_root)),
                    "file_sha256": paper_ledger_file_sha256,
                    "event_count": 0,
                    "last_event_id": None,
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
            },
            "broker_transport_available": False,
            "live_status": "LIVE_DISABLED",
        },
        "qmt_instrument_status_snapshot": {
            "status": "CAPTURE_INCOMPLETE",
            "session": SESSION.isoformat(),
            "reason_code": "QMT_INSTRUMENT_STATUS_SNAPSHOT_UNAVAILABLE",
            "error": "test fixture: QMT status source unavailable",
            "coverage_scope": "SCREENING_SIGNAL_SYMBOLS_ONLY",
            "can_explain_same_session_decision": False,
            "can_explain_prior_historical_session": False,
            "future_consumer_connected": False,
            "historical_backfill_allowed": False,
            "real_account_accessed": False,
            "real_order_transport_enabled": False,
            "live_status": "LIVE_DISABLED",
        },
        "human_paper_valuation": {
            "status": "VALUATION_COMPLETE",
            "session": SESSION.isoformat(),
            "valuation_object": str(valuation_path),
            "valuation_content_sha256": valuation["content_sha256"],
            "paper_ledger_content_sha256": paper_ledger_content_sha256,
            "accounting_content_sha256": accounting_content_sha256,
            "equity_curve_point_available": True,
            "performance_evaluable": False,
            "minimum_market_data_frequency": "1m",
            "tick_data_used": False,
            "broker_transport_available": False,
            "live_status": "LIVE_DISABLED",
        },
        "live_status": "LIVE_DISABLED",
    }


def test_forward_delivery_proves_immutable_current_contract_artifacts(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    forward_root = tmp_path / "forward"
    evidence = _artifact_evidence(forward_root)
    monkeypatch.setattr(
        live_module,
        "validate_live_review_snapshot",
        lambda _snapshot, *, session: (
            datetime(2026, 7, 30, 15, 1, tzinfo=CN),
            (),
        ),
    )
    monkeypatch.setattr(
        review_module,
        "validate_human_review_screen_document",
        lambda _document: (),
    )
    monkeypatch.setattr(
        valuation_module,
        "validate_human_paper_valuation_document",
        lambda payload: payload,
    )
    events = _events(tmp_path / "events.json", evaluation_evidence=evidence)
    _capture, readiness = _capture_evidence()

    result = audit_forward_paper_session_delivery(
        events,
        session=SESSION,
        observed_at=datetime(2026, 7, 30, 15, 25, tzinfo=CN),
        sector_capture_readiness=readiness,
        forward_root=forward_root,
    )

    assert result["ready"] is True
    assert result["reason_code"] == "READY"
    assert result["capture_evidence_proven"] is True
    assert result["data_ready_evidence_proven"] is True
    assert result["evaluation_artifacts_proven"] is True


def test_rehashed_event_cannot_replace_the_virtual_ledger_binding(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    forward_root = tmp_path / "forward"
    evidence = _artifact_evidence(forward_root)
    evidence["human_paper_settlement"]["content_sha256"] = "sha256:" + "f" * 64
    monkeypatch.setattr(
        live_module,
        "validate_live_review_snapshot",
        lambda _snapshot, *, session: (
            datetime(2026, 7, 30, 15, 1, tzinfo=CN),
            (),
        ),
    )
    monkeypatch.setattr(
        review_module,
        "validate_human_review_screen_document",
        lambda _document: (),
    )
    monkeypatch.setattr(
        valuation_module,
        "validate_human_paper_valuation_document",
        lambda payload: payload,
    )
    events = _events(tmp_path / "events.json", evaluation_evidence=evidence)
    _capture, readiness = _capture_evidence()

    result = audit_forward_paper_session_delivery(
        events,
        session=SESSION,
        observed_at=datetime(2026, 7, 30, 15, 25, tzinfo=CN),
        sector_capture_readiness=readiness,
        forward_root=forward_root,
    )

    assert result["ready"] is False
    assert result["reason_code"] == "EVALUATION_ARTIFACT_EVIDENCE_INVALID"
