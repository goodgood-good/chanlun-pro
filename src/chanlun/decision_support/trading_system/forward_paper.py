"""Append-only control ledger for the frozen strict strategy forward paper observation.

This module is deliberately operational rather than strategic.  It binds the
already-frozen recent-year parameter snapshot, records each daily gate, and
can never turn on a broker/account transport.  The decision pipeline remains
the same shared strict strategy decision/replay core; this ledger only proves what was (or
was not) available before that pipeline ran.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import date, datetime, time, timedelta
import hashlib
import json
import os
from pathlib import Path
import re
from typing import Mapping, Sequence
from zoneinfo import ZoneInfo

from chanlun.decision_support.fingerprints import normalize_datetime, sha256_json
from chanlun.decision_support.trading_system.decision_source_provenance import (
    FORWARD_IMPLEMENTATION_PROVENANCE_SCHEMA,
)
from chanlun.decision_support.trading_system.file_lock import interprocess_file_lock
from chanlun.decision_support.trading_system.trading_session import (
    resolve_trading_session_requirement,
)
from chanlun.decision_support.trading_system.qmt_sector_ledger import (
    QMT_SECTOR_RECEIPT_SCHEMA,
)


FORWARD_PAPER_CONTRACT_SCHEMA = "chanlun-forward-paper-human-review-contract"
FORWARD_PAPER_LEDGER_SCHEMA = "chanlun-forward-paper-ledger"
FORWARD_PAPER_EVENT_SCHEMA = "chanlun-forward-paper-event"
FORWARD_PAPER_SESSION_DELIVERY_SCHEMA = "chanlun-forward-paper-session-delivery"
FORWARD_IMPLEMENTATION_CONTINUITY_SCHEMA = "chanlun-forward-implementation-continuity"
FROZEN_RESEARCH_PARAMETER_SET_ID = (
    "sha256:7c7f7f0fe638110ad891b5f98f87f6f4b784bfd15980239261c964f80d06cf0b"
)
FROZEN_TECHNICAL_APPROXIMATION_PARAMETER_SET_ID = (
    "sha256:84f3e7f2146d4aefbd26f33028cb16b0c0eb86e10d523c55d7d1a83a743f81e0"
)
FROZEN_TECHNICAL_ALIGNMENT_PARAMETER_SET_ID = (
    "sha256:303673b8517de296ec57b7c98691c80f866bc45932b0dbcc1bf25607ec12e7df"
)
FROZEN_HUMAN_REVIEW_SCREENING_PARAMETER_SET_ID = (
    "sha256:00f1c5b19ce6cc71893e41be0fc83661f4fb8faaa02c86bbdcb24821efbc2e86"
)
_SHA256 = re.compile(r"^sha256:[0-9a-f]{64}$")
_PHASES = {"CAPTURE", "DATA_GATE", "DECISION", "CONTROL"}
_STATUSES = {
    "PAPER_STARTED",
    "CAPTURED",
    "CAPTURE_FAILED",
    "DATA_READY",
    "DATA_BLOCKED",
    "EVALUATED",
    "EVALUATION_BLOCKED",
}
_CN = ZoneInfo("Asia/Shanghai")
_FORWARD_SESSION_MANIFEST_SCHEMA = "chanlun-forward-session-manifest"
_FORWARD_ATTEMPT_RECEIPT_SCHEMA = "chanlun-forward-evaluation-attempt"
_FORWARD_LIVE_OBJECT_SCHEMA = "chanlun-forward-live-screening-snapshot"
_SOURCE_REVISION = re.compile(r"^[0-9a-f]{40,64}\.tree\.[0-9a-f]{24}$")
_IMPLEMENTATION_PROVENANCE_FIELDS = frozenset(
    {
        "schema",
        "application_source_revision",
        "forward_scheduler_module_sha256",
        "forward_python_tool_sha256",
        "sector_capture_tool_sha256",
        "python_implementation",
        "python_version",
        "pandas_version",
        "real_account_accessed",
        "real_order_transport_enabled",
        "live_status",
        "content_sha256",
    }
)


def validate_forward_implementation_provenance(
    payload: Mapping[str, object],
) -> dict[str, object]:
    """Validate the deterministic implementation identity carried by an event."""

    if not isinstance(payload, Mapping):
        raise ValueError("forward implementation provenance is invalid")
    if set(payload) != _IMPLEMENTATION_PROVENANCE_FIELDS:
        raise ValueError("forward implementation provenance fields changed")
    stable = {key: payload[key] for key in payload if key != "content_sha256"}
    if payload.get("schema") != FORWARD_IMPLEMENTATION_PROVENANCE_SCHEMA:
        raise ValueError("unsupported forward implementation provenance schema")
    if (
        _SOURCE_REVISION.fullmatch(
            str(payload.get("application_source_revision") or "")
        )
        is None
    ):
        raise ValueError("forward application source revision is invalid")
    for key in (
        "forward_scheduler_module_sha256",
        "forward_python_tool_sha256",
        "sector_capture_tool_sha256",
    ):
        if _SHA256.fullmatch(str(payload.get(key) or "")) is None:
            raise ValueError(f"forward implementation {key} is invalid")
    for key in ("python_implementation", "python_version", "pandas_version"):
        if not isinstance(payload.get(key), str) or not str(payload[key]).strip():
            raise ValueError(f"forward implementation {key} is invalid")
    if (
        payload.get("real_account_accessed") is not False
        or payload.get("real_order_transport_enabled") is not False
        or payload.get("live_status") != "LIVE_DISABLED"
    ):
        raise ValueError("forward implementation provenance safety changed")
    if payload.get("content_sha256") != sha256_json(stable):
        raise ValueError("forward implementation provenance content hash changed")
    return dict(payload)


def _event_implementation_provenance_identity(
    event: Mapping[str, object] | None,
) -> str | None:
    if not isinstance(event, Mapping):
        return None
    evidence = event.get("evidence")
    if not isinstance(evidence, Mapping):
        return None
    provenance = evidence.get("implementation_provenance")
    if not isinstance(provenance, Mapping):
        return None
    try:
        validated = validate_forward_implementation_provenance(provenance)
    except ValueError:
        return None
    return str(validated["content_sha256"])


def audit_forward_implementation_continuity(
    events: Sequence[Mapping[str, object]],
    *,
    session: date,
    current_implementation_provenance: Mapping[str, object],
) -> dict[str, object]:
    """Prove that Evaluate will use the implementation recorded by Capture.

    Delivery audits intentionally judge a completed historical chain by the
    three event identities it actually recorded.  This separate preflight is
    prospective: it prevents a later scheduled Evaluate process from consuming
    market data or writing ``DATA_READY`` after the working tree changed since
    the same-session Capture.  A blocked attempt remains recoverable if the
    original source state is restored and Evaluate is retried.
    """

    if isinstance(session, datetime) or not isinstance(session, date):
        raise TypeError("session must be a date")
    current = validate_forward_implementation_provenance(
        current_implementation_provenance
    )
    session_events: list[Mapping[str, object]] = []
    for event in events:
        if not isinstance(event, Mapping):
            raise ValueError("forward implementation continuity event is invalid")
        if str(event.get("session") or "") != session.isoformat():
            continue
        phase = str(event.get("phase") or "")
        status = str(event.get("status") or "")
        if phase not in _PHASES or status not in _STATUSES:
            raise ValueError(
                "forward implementation continuity event phase/status is invalid"
            )
        event_hash = str(event.get("event_sha256") or "")
        if _SHA256.fullmatch(event_hash) is None:
            raise ValueError(
                "forward implementation continuity event identity is invalid"
            )
        session_events.append(event)

    captured = next(
        (
            event
            for event in reversed(session_events)
            if event.get("status") == "CAPTURED"
        ),
        None,
    )
    evaluated = next(
        (
            event
            for event in reversed(session_events)
            if event.get("status") == "EVALUATED"
        ),
        None,
    )
    capture_identity = _event_implementation_provenance_identity(captured)
    current_identity = str(current["content_sha256"])
    if captured is None:
        ready = False
        reason_code = "CAPTURE_EVENT_MISSING"
    elif capture_identity is None:
        ready = False
        reason_code = "CAPTURE_IMPLEMENTATION_PROVENANCE_UNATTESTED"
    elif capture_identity != current_identity:
        ready = False
        reason_code = "IMPLEMENTATION_CHANGED_SINCE_CAPTURE"
    else:
        ready = True
        reason_code = "READY"

    stable: dict[str, object] = {
        "schema": FORWARD_IMPLEMENTATION_CONTINUITY_SCHEMA,
        "session": session.isoformat(),
        "ready": ready,
        "status": "ready" if ready else "not_ready",
        "reason_code": reason_code,
        "session_event_count": len(session_events),
        "capture_event_present": captured is not None,
        "evaluation_event_present": evaluated is not None,
        "capture_event_sha256": (
            None if captured is None else str(captured["event_sha256"])
        ),
        "evaluation_event_sha256": (
            None if evaluated is None else str(evaluated["event_sha256"])
        ),
        "capture_implementation_provenance_id": capture_identity,
        "current_implementation_provenance_id": current_identity,
        "same_implementation_as_capture": bool(
            capture_identity is not None and capture_identity == current_identity
        ),
        "market_data_read_authorized": ready,
        "real_account_accessed": False,
        "real_order_transport_enabled": False,
        "paper_status": "REVIEW_REQUIRED",
        "live_status": "LIVE_DISABLED",
    }
    return {**stable, "content_sha256": sha256_json(stable)}


def _aware_local_datetime(value: object) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(_CN)


def _capture_delivery_evidence_proven(
    event: Mapping[str, object],
    *,
    session: date,
    readiness: Mapping[str, object] | None,
) -> bool:
    if not isinstance(readiness, Mapping):
        return False
    evidence = event.get("evidence")
    if not isinstance(evidence, Mapping):
        return False
    receipt = evidence.get("receipt")
    if not isinstance(receipt, Mapping):
        return False
    captured_at = _aware_local_datetime(receipt.get("captured_at"))
    catalog_captured_at = _aware_local_datetime(readiness.get("catalog_captured_at"))
    recorded_at = _aware_local_datetime(event.get("recorded_at"))
    entry_sha256 = str(receipt.get("entry_sha256") or "")
    return bool(
        readiness.get("schema") == "chanlun-forward-sector-capture-readiness"
        and readiness.get("ready") is True
        and readiness.get("reason_code") == "READY"
        and readiness.get("session") == session.isoformat()
        and readiness.get("receipt_proven") is True
        and readiness.get("catalog_entry_sha256") == entry_sha256
        and readiness.get("real_account_accessed") is False
        and readiness.get("real_order_transport_enabled") is False
        and readiness.get("live_status") == "LIVE_DISABLED"
        and receipt.get("schema") == QMT_SECTOR_RECEIPT_SCHEMA
        and receipt.get("capture_session") == session.isoformat()
        and receipt.get("complete") is True
        and _SHA256.fullmatch(entry_sha256) is not None
        and _SHA256.fullmatch(str(evidence.get("receipt_sha256") or "")) is not None
        and _SHA256.fullmatch(str(evidence.get("sector_ledger_sha256") or ""))
        is not None
        and receipt.get("historical_backfill_allowed") is False
        and receipt.get("real_account_accessed") is False
        and receipt.get("real_order_transport_enabled") is False
        and receipt.get("live_status") == "LIVE_DISABLED"
        and captured_at is not None
        and catalog_captured_at == captured_at
        and recorded_at is not None
        and captured_at.date() == session
        and captured_at <= recorded_at
    )


def _data_ready_delivery_evidence_proven(
    event: Mapping[str, object],
    *,
    session: date,
    catalog_entry_sha256: str,
    catalog_captured_at: str,
) -> bool:
    evidence = event.get("evidence")
    if not isinstance(evidence, Mapping):
        return False
    gate = evidence.get("market_data_gate")
    if not isinstance(gate, Mapping):
        return False
    frequencies = gate.get("frequencies")
    if not isinstance(frequencies, Mapping):
        return False
    expected = {
        "1m": (240, time(9, 31)),
        "5m": (48, time(9, 35)),
    }
    latest_close: datetime | None = None
    for frequency, (minimum_rows, first_time) in expected.items():
        row = frequencies.get(frequency)
        if not isinstance(row, Mapping):
            return False
        first_at = _aware_local_datetime(row.get("first_at"))
        last_at = _aware_local_datetime(row.get("last_at"))
        expected_last = _aware_local_datetime(row.get("expected_last_at"))
        row_count = row.get("row_count")
        declared_minimum = row.get("minimum_rows")
        if (
            isinstance(row_count, bool)
            or not isinstance(row_count, int)
            or row_count != minimum_rows
            or declared_minimum != minimum_rows
            or first_at is None
            or last_at is None
            or expected_last is None
            or first_at.date() != session
            or first_at.timetz().replace(tzinfo=None) != first_time
            or last_at.date() != session
            or last_at.timetz().replace(tzinfo=None) != time(15)
            or expected_last != last_at
        ):
            return False
        latest_close = last_at
    recorded_at = _aware_local_datetime(event.get("recorded_at"))
    event_reasons = evidence.get("reason_codes")
    gate_reasons = gate.get("reason_codes")
    return bool(
        evidence.get("session") == session.isoformat()
        and evidence.get("sector_catalog_entry_sha256") == catalog_entry_sha256
        and evidence.get("sector_capture_at") == catalog_captured_at
        and isinstance(event_reasons, (list, tuple))
        and not event_reasons
        and gate.get("complete") is True
        and gate.get("session") == session.isoformat()
        and gate.get("minimum_market_data_frequency") == "1m"
        and gate.get("market_data_was_synthesized") is False
        and gate.get("tick_data_used") is False
        and gate.get("real_account_accessed") is False
        and gate.get("real_order_transport_enabled") is False
        and isinstance(gate_reasons, (list, tuple))
        and not gate_reasons
        and recorded_at is not None
        and latest_close is not None
        and recorded_at >= latest_close
    )


def _forward_artifact_path(
    session_root: Path,
    raw: object,
) -> Path:
    value = Path(str(raw))
    candidate = (
        value.resolve() if value.is_absolute() else (session_root / value).resolve()
    )
    try:
        candidate.relative_to(session_root.resolve())
    except ValueError as exc:
        raise ValueError("forward delivery artifact escaped its session") from exc
    return candidate


def _verified_json_document(path: Path) -> dict[str, object]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError("forward delivery artifact cannot be read") from exc
    if not isinstance(payload, dict):
        raise ValueError("forward delivery artifact is invalid")
    return payload


def _semantic_content_sha256(payload: Mapping[str, object]) -> str:
    claimed = str(payload.get("content_sha256") or "")
    stable = dict(payload)
    stable.pop("content_sha256", None)
    if _SHA256.fullmatch(claimed) is None or claimed != sha256_json(stable):
        raise ValueError("forward delivery artifact semantic hash changed")
    return claimed


def _attempt_identity_document(
    attempt: Mapping[str, object],
) -> dict[str, object]:
    fields = (
        "schema",
        "session",
        "contract_id",
        "strategy_parameter_set_id",
        "screening_policy_id",
        "decision_core_id",
        "source_content_sha256",
        "live_object",
        "human_review_object",
        "candidate_count",
        "scanner_error_count",
        "highest_status",
        "live_status",
    )
    if any(field not in attempt for field in fields):
        raise ValueError("forward evaluation attempt identity is incomplete")
    return {field: attempt[field] for field in fields}


def _selection_audit_identities(
    audit: Mapping[str, object],
    field: str,
    *,
    sorted_required: bool,
) -> tuple[str, ...] | None:
    raw = audit.get(field)
    if not isinstance(raw, list):
        return None
    values = tuple(str(value) for value in raw)
    if (
        len(values) != len(set(values))
        or any(_SHA256.fullmatch(value) is None for value in values)
        or (sorted_required and values != tuple(sorted(values)))
    ):
        return None
    return values


def _selection_audit_invalid_rows(
    audit: Mapping[str, object],
    field: str,
) -> tuple[str, ...] | None:
    raw = audit.get(field)
    if not isinstance(raw, list):
        return None
    identities: list[str] = []
    for value in raw:
        if (
            not isinstance(value, Mapping)
            or set(value) != {"intent_id", "reason"}
            or _SHA256.fullmatch(str(value.get("intent_id") or "")) is None
            or not str(value.get("reason") or "").strip()
        ):
            return None
        identities.append(str(value["intent_id"]))
    if len(identities) != len(set(identities)):
        return None
    return tuple(identities)


def _selection_attestation_audit_proven(
    audit: object,
) -> tuple[bool, tuple[str, ...]]:
    expected_fields = {
        "schema",
        "status",
        "attested_buy_intent_count",
        "verified_catalog_binding_count",
        "verified_buy_intent_ids",
        "catalog_unavailable_intent_ids",
        "invalid_attestations",
        "selection_evidence_ids",
        "catalog_entry_sha256s",
        "exact_qmt_revision_name_and_membership_verified",
        "tick_data_used",
        "broker_transport_available",
        "live_status",
    }
    if (
        not isinstance(audit, Mapping)
        or set(audit) != expected_fields
        or audit.get("schema")
        != "chanlun-human-paper-entry-selection-attestation-audit"
        or audit.get("status")
        not in {
            "COMPLETE",
            "INCOMPLETE_CATALOG_ARCHIVE",
            "INVALID",
            "NO_SELECTION_ATTESTATIONS",
        }
        or audit.get("tick_data_used") is not False
        or audit.get("broker_transport_available") is not False
        or audit.get("live_status") != "LIVE_DISABLED"
    ):
        return False, ()
    attested = audit.get("attested_buy_intent_count")
    verified_count = audit.get("verified_catalog_binding_count")
    if (
        type(attested) is not int
        or attested < 0
        or type(verified_count) is not int
        or verified_count < 0
    ):
        return False, ()
    verified = _selection_audit_identities(
        audit,
        "verified_buy_intent_ids",
        sorted_required=True,
    )
    unavailable = _selection_audit_identities(
        audit,
        "catalog_unavailable_intent_ids",
        sorted_required=False,
    )
    invalid = _selection_audit_invalid_rows(audit, "invalid_attestations")
    evidence_ids = _selection_audit_identities(
        audit,
        "selection_evidence_ids",
        sorted_required=True,
    )
    catalog_ids = _selection_audit_identities(
        audit,
        "catalog_entry_sha256s",
        sorted_required=True,
    )
    if any(
        value is None
        for value in (
            verified,
            unavailable,
            invalid,
            evidence_ids,
            catalog_ids,
        )
    ):
        return False, ()
    assert verified is not None
    assert unavailable is not None
    assert invalid is not None
    assert evidence_ids is not None
    assert catalog_ids is not None
    outcomes = (set(verified), set(unavailable), set(invalid))
    if (
        any(
            left & right
            for index, left in enumerate(outcomes)
            for right in outcomes[index + 1 :]
        )
        or attested != len(verified) + len(unavailable) + len(invalid)
        or verified_count != len(verified)
        or len(evidence_ids) != len(verified)
        or len(catalog_ids) > len(verified)
        or (bool(verified) != bool(catalog_ids))
    ):
        return False, ()
    expected_status = (
        "INVALID"
        if invalid
        else "INCOMPLETE_CATALOG_ARCHIVE"
        if unavailable
        else "COMPLETE"
        if attested
        else "NO_SELECTION_ATTESTATIONS"
    )
    if audit.get("status") != expected_status or audit.get(
        "exact_qmt_revision_name_and_membership_verified"
    ) is not (expected_status in {"COMPLETE", "NO_SELECTION_ATTESTATIONS"}):
        return False, ()
    return True, verified


def _selection_source_audit_proven(
    audit: object,
) -> tuple[bool, tuple[str, ...]]:
    expected_fields = {
        "schema",
        "status",
        "required_live_ranked_buy_intent_count",
        "verified_source_binding_count",
        "verified_required_buy_intent_ids",
        "source_unavailable_intent_ids",
        "invalid_source_bindings",
        "immutable_source_ranking_resolved",
        "broker_transport_available",
        "live_status",
    }
    if (
        not isinstance(audit, Mapping)
        or set(audit) != expected_fields
        or audit.get("schema") != "chanlun-human-paper-entry-selection-source-audit"
        or audit.get("status")
        not in {
            "COMPLETE",
            "INCOMPLETE_SOURCE_ARCHIVE",
            "INVALID",
            "NO_REQUIRED_SELECTION_INTENTS",
        }
        or audit.get("broker_transport_available") is not False
        or audit.get("live_status") != "LIVE_DISABLED"
    ):
        return False, ()
    required = audit.get("required_live_ranked_buy_intent_count")
    verified_count = audit.get("verified_source_binding_count")
    if (
        type(required) is not int
        or required < 0
        or type(verified_count) is not int
        or verified_count < 0
    ):
        return False, ()
    verified = _selection_audit_identities(
        audit,
        "verified_required_buy_intent_ids",
        sorted_required=True,
    )
    unavailable = _selection_audit_identities(
        audit,
        "source_unavailable_intent_ids",
        sorted_required=False,
    )
    invalid = _selection_audit_invalid_rows(audit, "invalid_source_bindings")
    if any(value is None for value in (verified, unavailable, invalid)):
        return False, ()
    assert verified is not None
    assert unavailable is not None
    assert invalid is not None
    outcomes = (set(verified), set(unavailable), set(invalid))
    minimum_required = len(verified)
    if (
        any(
            left & right
            for index, left in enumerate(outcomes)
            for right in outcomes[index + 1 :]
        )
        or verified_count != len(verified)
        or required < minimum_required
        or required > minimum_required + len(invalid)
    ):
        return False, ()
    expected_status = (
        "INVALID"
        if invalid
        else "INCOMPLETE_SOURCE_ARCHIVE"
        if unavailable
        else "COMPLETE"
        if required
        else "NO_REQUIRED_SELECTION_INTENTS"
    )
    if audit.get("status") != expected_status or audit.get(
        "immutable_source_ranking_resolved"
    ) is not (expected_status in {"COMPLETE", "NO_REQUIRED_SELECTION_INTENTS"}):
        return False, ()
    return True, verified


def _selection_source_report_archive_proven(
    archive: object,
    *,
    verified_pending_intent_ids: Sequence[str],
) -> bool:
    expected_fields = {
        "schema",
        "status",
        "archive_performed",
        "required_source_report_count",
        "required_source_content_sha256s",
        "archived_source_report_count",
        "objects",
        "all_required_source_reports_archived",
        "broker_transport_available",
        "live_status",
    }
    if (
        not isinstance(archive, Mapping)
        or set(archive) != expected_fields
        or archive.get("schema") != "chanlun-human-paper-entry-source-report-archive"
        or archive.get("status") not in {"COMPLETE", "NO_REQUIRED_SOURCE_REPORTS"}
        or archive.get("archive_performed") is not True
        or archive.get("all_required_source_reports_archived") is not True
        or archive.get("broker_transport_available") is not False
        or archive.get("live_status") != "LIVE_DISABLED"
    ):
        return False
    required = _selection_audit_identities(
        archive,
        "required_source_content_sha256s",
        sorted_required=True,
    )
    required_count = archive.get("required_source_report_count")
    archived_count = archive.get("archived_source_report_count")
    objects = archive.get("objects")
    if (
        required is None
        or type(required_count) is not int
        or required_count < 0
        or type(archived_count) is not int
        or archived_count < 0
        or not isinstance(objects, list)
    ):
        return False
    object_sources: list[str] = []
    object_intent_ids: list[str] = []
    for value in objects:
        if not isinstance(value, Mapping) or set(value) != {
            "source_content_sha256",
            "path",
            "file_sha256",
            "candidate_ids",
            "verified_pending_buy_intent_ids",
            "live_status",
        }:
            return False
        source_hash = str(value.get("source_content_sha256") or "")
        file_hash = str(value.get("file_sha256") or "")
        raw_path = str(value.get("path") or "")
        path = Path(raw_path)
        candidate_ids = _selection_audit_identities(
            value,
            "candidate_ids",
            sorted_required=True,
        )
        receipt_intent_ids = _selection_audit_identities(
            value,
            "verified_pending_buy_intent_ids",
            sorted_required=True,
        )
        if (
            _SHA256.fullmatch(source_hash) is None
            or _SHA256.fullmatch(file_hash) is None
            or raw_path in {"", "."}
            or path.is_absolute()
            or ".." in path.parts
            or candidate_ids is None
            or not candidate_ids
            or receipt_intent_ids is None
            or not receipt_intent_ids
            or value.get("live_status") != "LIVE_DISABLED"
        ):
            return False
        object_sources.append(source_hash)
        object_intent_ids.extend(receipt_intent_ids)
    if (
        tuple(object_sources) != tuple(sorted(object_sources))
        or len(object_sources) != len(set(object_sources))
        or set(object_sources) != set(required)
        or len(object_intent_ids) != len(set(object_intent_ids))
        or set(object_intent_ids) != set(verified_pending_intent_ids)
        or required_count != len(required)
        or archived_count != len(objects)
        or archive.get("status")
        != ("COMPLETE" if required else "NO_REQUIRED_SOURCE_REPORTS")
    ):
        return False
    return True


def _selection_source_report_archive_files_proven(
    archive: object,
    *,
    session_root: Path,
    paper_ledger_events: Sequence[Mapping[str, object]] | None = None,
) -> bool:
    """Reopen every copied source report instead of trusting its receipt."""

    from chanlun.decision_support.trading_system.human_paper_ledger import (
        audit_human_paper_entry_selection_source_bindings,
    )
    from chanlun.decision_support.trading_system.human_review_screening import (
        validate_human_review_screen_document,
    )

    if not isinstance(archive, Mapping):
        return False
    alerts_by_source: dict[str, object] = {}
    receipt_intent_ids: set[str] = set()
    try:
        for identity in archive.get("objects", ()):
            if not isinstance(identity, Mapping):
                raise ValueError("forward entry source report identity is invalid")
            source_hash = str(identity.get("source_content_sha256") or "")
            source_path = _forward_artifact_path(
                session_root,
                identity.get("path"),
            )
            if (
                source_path.parent
                != session_root / "objects" / "paper_entry_selection_source_report"
                or source_path.name != f"{source_hash[7:]}.json"
                or not source_path.is_file()
                or sha256_file(source_path) != identity.get("file_sha256")
            ):
                raise ValueError("forward entry source report object changed")
            source_report = _verified_json_document(source_path)
            source_content_sha256 = _semantic_content_sha256(source_report)
            source_alerts = validate_human_review_screen_document(source_report)
            if source_content_sha256 != source_hash or sorted(
                value.candidate_id for value in source_alerts
            ) != identity.get("candidate_ids"):
                raise ValueError("forward entry source report semantics changed")
            alerts_by_source[source_hash] = source_alerts
            raw_intent_ids = identity.get("verified_pending_buy_intent_ids")
            if not isinstance(raw_intent_ids, list):
                raise ValueError("forward entry source intent receipt changed")
            receipt_intent_ids.update(str(value) for value in raw_intent_ids)
        if paper_ledger_events is not None and receipt_intent_ids:
            relevant_events = tuple(
                event
                for event in paper_ledger_events
                if isinstance(event, Mapping)
                and event.get("kind") == "INTENT"
                and isinstance(event.get("payload"), Mapping)
                and str(event["payload"].get("intent_id") or "") in receipt_intent_ids
            )
            if len(relevant_events) != len(receipt_intent_ids):
                raise ValueError(
                    "forward entry source intents do not resolve in ledger"
                )
            source_audit = audit_human_paper_entry_selection_source_bindings(
                relevant_events,
                alerts_by_source_content_sha256=alerts_by_source,
            )
            if (
                source_audit.get("status") != "COMPLETE"
                or set(source_audit.get("verified_required_buy_intent_ids") or ())
                != receipt_intent_ids
            ):
                raise ValueError(
                    "forward ledger intents differ from archived source reports"
                )
    except (OSError, TypeError, ValueError):
        return False
    return True


def _selection_paper_ledger_prefix_archive_proven(
    archive: object,
    *,
    expected_content_sha256: object,
) -> bool:
    expected_fields = {
        "schema",
        "status",
        "archive_performed",
        "paper_ledger_content_sha256",
        "path",
        "file_sha256",
        "event_count",
        "last_event_id",
        "broker_transport_available",
        "automated_order_authorized",
        "live_status",
    }
    if not isinstance(archive, Mapping) or set(archive) != expected_fields:
        return False
    content_hash = str(archive.get("paper_ledger_content_sha256") or "")
    file_hash = str(archive.get("file_sha256") or "")
    raw_path = str(archive.get("path") or "")
    path = Path(raw_path)
    event_count = archive.get("event_count")
    last_event_id = archive.get("last_event_id")
    if (
        archive.get("schema") != "chanlun-human-paper-ledger-prefix-archive"
        or archive.get("status") != "COMPLETE"
        or archive.get("archive_performed") is not True
        or content_hash != expected_content_sha256
        or _SHA256.fullmatch(content_hash) is None
        or _SHA256.fullmatch(file_hash) is None
        or raw_path in {"", "."}
        or path.is_absolute()
        or ".." in path.parts
        or type(event_count) is not int
        or event_count < 0
        or (event_count == 0 and last_event_id is not None)
        or (event_count > 0 and _SHA256.fullmatch(str(last_event_id or "")) is None)
        or archive.get("broker_transport_available") is not False
        or archive.get("automated_order_authorized") is not False
        or archive.get("live_status") != "LIVE_DISABLED"
    ):
        return False
    return True


def _selection_paper_ledger_prefix_archive_file(
    archive: object,
    *,
    session_root: Path,
) -> dict[str, object] | None:
    """Reopen and fully validate the exact ledger prefix used by settlement."""

    from chanlun.decision_support.trading_system.human_paper_ledger import (
        load_human_paper_ledger,
    )

    if not isinstance(archive, Mapping):
        return None
    try:
        content_hash = str(archive.get("paper_ledger_content_sha256") or "")
        ledger_path = _forward_artifact_path(session_root, archive.get("path"))
        if (
            ledger_path.parent != session_root / "objects" / "human_paper_ledger_prefix"
            or ledger_path.name != f"{content_hash[7:]}.json"
            or not ledger_path.is_file()
            or sha256_file(ledger_path) != archive.get("file_sha256")
        ):
            raise ValueError("forward human paper ledger prefix object changed")
        ledger = load_human_paper_ledger(ledger_path)
        events = ledger.get("events")
        if not isinstance(events, list):
            raise ValueError("forward human paper ledger prefix events changed")
        last_event_id = None if not events else events[-1].get("event_id")
        if (
            ledger.get("content_sha256") != content_hash
            or len(events) != archive.get("event_count")
            or last_event_id != archive.get("last_event_id")
        ):
            raise ValueError("forward human paper ledger prefix receipt changed")
    except (OSError, TypeError, ValueError):
        return None
    return ledger


def _human_paper_entry_selection_gate_proven(
    settlement: Mapping[str, object],
) -> bool:
    """Validate the mandatory execution-time QMT sector-admission gate."""

    gate = settlement.get("entry_selection_settlement_gate")
    if gate is None:
        return False
    if (
        not isinstance(gate, Mapping)
        or gate.get("schema") != "chanlun-human-paper-entry-selection-settlement-gate"
        or gate.get("status") not in {"READY", "BLOCKED", "NO_PENDING_BUYS"}
        or gate.get("sector_catalog_ledger_status")
        not in {"VALID", "MISSING", "INVALID"}
        or gate.get("exact_qmt_sector_admission_required_before_virtual_buy_fill")
        is not True
        or gate.get("immutable_source_ranking_required_before_virtual_buy_fill")
        is not True
        or gate.get("paper_ledger_prefix_required_for_independent_replay") is not True
        or gate.get("blocked_buy_remains_pending") is not True
        or gate.get("persistent_sell_processing_continues") is not True
        or gate.get("tick_data_used") is not False
        or gate.get("broker_transport_available") is not False
        or gate.get("live_status") != "LIVE_DISABLED"
    ):
        return False

    def identities(field: str) -> tuple[str, ...] | None:
        raw = gate.get(field)
        if not isinstance(raw, list):
            return None
        values = tuple(str(value) for value in raw)
        if (
            len(values) != len(set(values))
            or values != tuple(sorted(values))
            or any(_SHA256.fullmatch(value) is None for value in values)
        ):
            return None
        return values

    pending = identities("pending_buy_intent_ids")
    verified = identities("verified_pending_buy_intent_ids")
    blocked = identities("blocked_pending_buy_intent_ids")
    if pending is None or verified is None or blocked is None:
        return False
    if (
        set(verified) & set(blocked)
        or set(verified) | set(blocked) != set(pending)
        or gate.get("pending_buy_intent_count") != len(pending)
        or gate.get("verified_pending_buy_intent_count") != len(verified)
        or gate.get("blocked_pending_buy_intent_count") != len(blocked)
    ):
        return False
    expected_gate_status = (
        "BLOCKED" if blocked else "READY" if pending else "NO_PENDING_BUYS"
    )
    if gate.get("status") != expected_gate_status:
        return False
    attestation_valid, audited_verified = _selection_attestation_audit_proven(
        gate.get("attestation_audit")
    )
    if not attestation_valid:
        return False
    catalog_status = str(gate.get("sector_catalog_ledger_status"))
    catalog_hash = gate.get("sector_catalog_ledger_content_sha256")
    if (
        catalog_status == "VALID" and _SHA256.fullmatch(str(catalog_hash or "")) is None
    ) or (catalog_status != "VALID" and catalog_hash is not None):
        return False
    if pending and not blocked and catalog_status != "VALID":
        return False
    source_valid, source_verified = _selection_source_audit_proven(
        gate.get("source_binding_audit")
    )
    if not source_valid:
        return False
    expected_verified = set(pending) & set(audited_verified) & set(source_verified)
    if set(verified) != expected_verified:
        return False
    if not _selection_source_report_archive_proven(
        gate.get("source_report_archive"),
        verified_pending_intent_ids=verified,
    ):
        return False
    if not _selection_paper_ledger_prefix_archive_proven(
        gate.get("paper_ledger_prefix_archive"),
        expected_content_sha256=settlement.get("content_sha256"),
    ):
        return False
    settlement_status = str(settlement.get("status") or "")
    if "ENTRY_SELECTION_EVIDENCE" in settlement_status and (
        not blocked or gate.get("status") != "BLOCKED"
    ):
        return False
    if settlement_status == "VIRTUAL_SETTLEMENT_READY" and blocked:
        return False
    return True


def _qmt_instrument_status_snapshot_evidence_proven(
    value: object,
    *,
    session_root: Path,
    session: date,
    expected_source_screen_content_sha256: str,
    expected_symbols: Sequence[str],
) -> bool:
    """Verify the status-snapshot result bound to one screening run."""

    from chanlun.decision_support.trading_system.qmt_instrument_status_snapshot import (
        QMT_INSTRUMENT_STATUS_SNAPSHOT_SCHEMA,
        QmtInstrumentStatusSnapshot,
    )

    if value is None:
        return False
    if not isinstance(value, Mapping):
        return False
    if value.get("status") == "CAPTURE_INCOMPLETE":
        return bool(
            value.get("session") == session.isoformat()
            and value.get("reason_code") == "QMT_INSTRUMENT_STATUS_SNAPSHOT_UNAVAILABLE"
            and isinstance(value.get("error"), str)
            and bool(str(value.get("error") or "").strip())
            and value.get("coverage_scope") == "SCREENING_SIGNAL_SYMBOLS_ONLY"
            and value.get("can_explain_same_session_decision") is False
            and value.get("can_explain_prior_historical_session") is False
            and value.get("future_consumer_connected") is False
            and value.get("historical_backfill_allowed") is False
            and value.get("real_account_accessed") is False
            and value.get("real_order_transport_enabled") is False
            and value.get("live_status") == "LIVE_DISABLED"
        )
    try:
        status_object_path = _forward_artifact_path(
            session_root,
            value.get("object_path"),
        )
        expected_content_sha256 = str(value.get("content_sha256") or "")
        expected_file_sha256 = str(value.get("object_file_sha256") or "")
        status_counts = value.get("status_counts")
        expected_status_directory = (
            session_root / "objects" / "qmt_instrument_status_snapshot"
        )
        if (
            value.get("schema") != QMT_INSTRUMENT_STATUS_SNAPSHOT_SCHEMA
            or value.get("session") != session.isoformat()
            or _SHA256.fullmatch(expected_content_sha256) is None
            or _SHA256.fullmatch(expected_file_sha256) is None
            or status_object_path.parent != expected_status_directory
            or status_object_path.name != f"{expected_content_sha256[7:]}.json"
            or not status_object_path.is_file()
            or sha256_file(status_object_path) != expected_file_sha256
            or value.get("coverage_scope") != "SCREENING_SIGNAL_SYMBOLS_ONLY"
            or value.get("can_explain_same_session_decision") is not False
            or value.get("can_explain_prior_historical_session") is not False
            or value.get("future_consumer_connected") is not False
            or value.get("same_session_decision_adjudication_allowed") is not False
            or value.get("historical_backfill_allowed") is not False
            or value.get("real_account_accessed") is not False
            or value.get("real_order_transport_enabled") is not False
            or value.get("live_status") != "LIVE_DISABLED"
            or type(value.get("requested_symbol_count")) is not int
            or type(value.get("complete_symbol_count")) is not int
            or type(value.get("error_count")) is not int
            or type(value.get("all_complete")) is not bool
            or not isinstance(status_counts, Mapping)
            or set(status_counts) != {"NORMAL", "SUSPENDED"}
            or any(
                type(status_counts[key]) is not int or status_counts[key] < 0
                for key in ("NORMAL", "SUSPENDED")
            )
        ):
            return False
        status_document = _verified_json_document(status_object_path)
        validated_status = QmtInstrumentStatusSnapshot.from_document(status_document)
        expected = tuple(sorted(set(expected_symbols)))
        return bool(
            validated_status.session == session
            and value.get("captured_at") == validated_status.captured_at.isoformat()
            and value.get("sector_catalog_entry_sha256")
            == validated_status.sector_catalog_entry_sha256
            and value.get("source_screen_content_sha256")
            == validated_status.source_screen_content_sha256
            and validated_status.source_screen_content_sha256
            == expected_source_screen_content_sha256
            and validated_status.requested_symbols == expected
            and validated_status.content_sha256 == expected_content_sha256
            and value.get("requested_symbol_count") == len(expected)
            and value.get("complete_symbol_count") == len(validated_status.facts)
            and value.get("error_count") == len(validated_status.errors)
            and value.get("all_complete") is validated_status.all_complete
            and status_counts == validated_status.document()["status_counts"]
            and value.get("point_in_time_scope") == "SUBSEQUENT_SESSION_DECISIONS_ONLY"
        )
    except (OSError, TypeError, ValueError):
        return False


def _evaluation_artifacts_proof(
    event: Mapping[str, object],
    *,
    session: date,
    forward_root: Path,
) -> tuple[bool, str]:
    # Lazy imports keep the forward contract module below accounting and
    # valuation in the dependency graph; both of those modules bind the frozen
    # forward parameter identity and therefore import this module themselves.
    from chanlun.decision_support.trading_system.human_paper_valuation import (
        validate_human_paper_valuation_document,
    )
    from chanlun.decision_support.trading_system.human_review_screening import (
        validate_human_review_screen_document,
    )
    from chanlun.decision_support.trading_system.live_human_review import (
        validate_live_review_snapshot,
    )

    evidence = event.get("evidence")
    if not isinstance(evidence, Mapping):
        return False, "EVALUATION_ARTIFACT_EVIDENCE_INVALID"
    valuation = evidence.get("human_paper_valuation")
    if (
        not isinstance(valuation, Mapping)
        or valuation.get("status") != "VALUATION_COMPLETE"
        or valuation.get("session") != session.isoformat()
        or valuation.get("equity_curve_point_available") is not True
        or valuation.get("performance_evaluable") is not False
        or valuation.get("minimum_market_data_frequency") != "1m"
        or valuation.get("tick_data_used") is not False
        or valuation.get("broker_transport_available") is not False
        or valuation.get("live_status") != "LIVE_DISABLED"
    ):
        return False, "EVALUATION_VALUATION_EVIDENCE_MISSING"
    try:
        session_root = (
            forward_root.resolve() / "sessions" / session.isoformat()
        ).resolve()
        manifest_path = _forward_artifact_path(
            session_root,
            evidence.get("session_manifest"),
        )
        if manifest_path != session_root / "forward_session_manifest.json":
            raise ValueError("forward session manifest path changed")
        receipt_path = _forward_artifact_path(
            session_root,
            evidence.get("attempt_receipt"),
        )
        if (
            receipt_path.parent
            != session_root / "objects" / "forward_evaluation_attempt"
        ):
            raise ValueError("forward evaluation receipt path changed")
        receipt_file_sha256 = str(evidence.get("attempt_receipt_sha256") or "")
        if (
            not receipt_path.is_file()
            or _SHA256.fullmatch(receipt_file_sha256) is None
            or sha256_file(receipt_path) != receipt_file_sha256
            or receipt_path.name != f"{receipt_file_sha256[7:]}.json"
        ):
            raise ValueError("forward evaluation receipt file identity changed")
        receipt = _verified_json_document(receipt_path)
        _semantic_content_sha256(receipt)
        identity = _attempt_identity_document(receipt)
        attempt_id = str(receipt.get("attempt_id") or "")
        screening_policy_id = str(receipt.get("screening_policy_id") or "")
        decision_core_id = str(receipt.get("decision_core_id") or "")
        if (
            receipt.get("schema") != _FORWARD_ATTEMPT_RECEIPT_SCHEMA
            or receipt.get("session") != session.isoformat()
            or receipt.get("contract_id") != event.get("contract_id")
            or receipt.get("strategy_parameter_set_id")
            != event.get("strategy_parameter_set_id")
            or receipt.get("highest_status") != "REVIEW_REQUIRED"
            or receipt.get("live_status") != "LIVE_DISABLED"
            or receipt.get("promoted_sample") is not True
            or attempt_id != sha256_json(identity)
            or evidence.get("attempt_id") != attempt_id
            or _SHA256.fullmatch(screening_policy_id) is None
            or _SHA256.fullmatch(decision_core_id) is None
            or evidence.get("screening_policy_id") != screening_policy_id
            or evidence.get("decision_core_id") != decision_core_id
        ):
            raise ValueError("forward evaluation attempt identity changed")

        manifest = _verified_json_document(manifest_path)
        manifest_sha256 = _semantic_content_sha256(manifest)
        attempts = manifest.get("attempts")
        if not isinstance(attempts, list):
            raise ValueError("forward session attempts are invalid")
        promoted = next(
            (
                item
                for item in attempts
                if isinstance(item, Mapping) and item.get("attempt_id") == attempt_id
            ),
            None,
        )
        if (
            manifest.get("schema") != _FORWARD_SESSION_MANIFEST_SCHEMA
            or manifest.get("session") != session.isoformat()
            or manifest.get("contract_id") != event.get("contract_id")
            or manifest.get("strategy_parameter_set_id")
            != event.get("strategy_parameter_set_id")
            or manifest.get("highest_status") != "REVIEW_REQUIRED"
            or manifest.get("live_status") != "LIVE_DISABLED"
            or manifest.get("promoted_attempt_id") != attempt_id
            or manifest.get("promoted_screening_policy_id") != screening_policy_id
            or manifest.get("promoted_decision_core_id") != decision_core_id
            or manifest.get("promotion_policy") != "FIRST_VALID_EVALUATION_ONLY"
            or manifest.get("promoted_sample_count") != 1
            or manifest.get("attempt_count") != len(attempts)
            or promoted is None
            or _attempt_identity_document(promoted) != identity
            or promoted.get("screening_policy_id") != screening_policy_id
            or promoted.get("decision_core_id") != decision_core_id
            or evidence.get("session_manifest_revision") != manifest_sha256
            or evidence.get("session_attempt_count") != len(attempts)
            or evidence.get("promoted_sample_count") != 1
            or evidence.get("promoted_sample") is not True
        ):
            raise ValueError("forward session manifest identity changed")

        live_identity = receipt.get("live_object")
        review_identity = receipt.get("human_review_object")
        if not isinstance(live_identity, Mapping) or not isinstance(
            review_identity, Mapping
        ):
            raise ValueError("forward attempt object identities are unavailable")
        live_path = _forward_artifact_path(
            session_root,
            live_identity.get("path"),
        )
        review_path = _forward_artifact_path(
            session_root,
            review_identity.get("path"),
        )
        if (
            not live_path.is_file()
            or sha256_file(live_path) != live_identity.get("file_sha256")
            or live_path.parent
            != session_root / "objects" / "forward_live_screening_snapshot"
            or live_path.name != f"{str(live_identity.get('file_sha256'))[7:]}.json"
            or not review_path.is_file()
            or sha256_file(review_path) != review_identity.get("file_sha256")
            or review_path.parent
            != session_root / "objects" / "forward_human_review_screen"
            or review_path.name != f"{str(review_identity.get('file_sha256'))[7:]}.json"
            or _forward_artifact_path(session_root, evidence.get("result")) != live_path
            or _forward_artifact_path(
                session_root,
                evidence.get("human_review_result"),
            )
            != review_path
            or evidence.get("result_sha256") != live_identity.get("file_sha256")
            or evidence.get("human_review_result_sha256")
            != review_identity.get("file_sha256")
        ):
            raise ValueError("forward promoted object file identity changed")

        live = _verified_json_document(live_path)
        live_content_sha256 = _semantic_content_sha256(live)
        snapshot = live.get("snapshot")
        if not isinstance(snapshot, Mapping):
            raise ValueError("forward live screening snapshot is unavailable")
        _review_at, signals = validate_live_review_snapshot(
            snapshot,
            session=session,
        )
        if (
            live.get("schema") != _FORWARD_LIVE_OBJECT_SCHEMA
            or live.get("session") != session.isoformat()
            or live.get("contract_id") != event.get("contract_id")
            or live.get("strategy_parameter_set_id")
            != event.get("strategy_parameter_set_id")
            or live.get("source_content_sha256") != receipt.get("source_content_sha256")
            or live.get("screening_policy_id") != screening_policy_id
            or live.get("decision_core_id") != decision_core_id
            or live.get("highest_status") != "REVIEW_REQUIRED"
            or live.get("human_confirmation_required") is not True
            or live.get("automated_order_authorized") is not False
            or live.get("orders_created") != 0
            or live.get("fills_created") != 0
            or live.get("positions_created") != 0
            or live.get("live_status") != "LIVE_DISABLED"
            or live_content_sha256 != live_identity.get("content_sha256")
            or live.get("candidate_count") != len(signals)
        ):
            raise ValueError("forward live object boundary changed")

        expected_status_symbols = tuple(
            str(value.get("code") or "") for value in signals
        )
        if not _qmt_instrument_status_snapshot_evidence_proven(
            evidence.get("qmt_instrument_status_snapshot"),
            session_root=session_root,
            session=session,
            expected_source_screen_content_sha256=str(
                live.get("source_content_sha256") or ""
            ),
            expected_symbols=expected_status_symbols,
        ):
            raise ValueError("forward instrument-status evidence changed")

        review = _verified_json_document(review_path)
        review_content_sha256 = _semantic_content_sha256(review)
        alerts = validate_human_review_screen_document(review)
        input_hashes = review.get("input_hashes")
        if (
            review.get("forward_paper_session") != session.isoformat()
            or not isinstance(input_hashes, Mapping)
            or input_hashes.get("live_screening_snapshot")
            != receipt.get("source_content_sha256")
            or review_content_sha256 != review_identity.get("content_sha256")
            or len(alerts) != receipt.get("candidate_count")
            or evidence.get("candidate_count") != len(signals)
            or evidence.get("human_review_candidate_count") != len(alerts)
            or evidence.get("content_sha256") != live_content_sha256
            or evidence.get("human_review_content_sha256") != review_content_sha256
            or evidence.get("source_content_sha256")
            != receipt.get("source_content_sha256")
            or evidence.get("orders_created") != 0
            or evidence.get("fills_created") != 0
            or evidence.get("human_confirmation_required") is not True
            or evidence.get("live_status") != "LIVE_DISABLED"
        ):
            raise ValueError("forward evaluation evidence binding changed")

        valuation_path = _forward_artifact_path(
            session_root,
            valuation.get("valuation_object"),
        )
        valuation_content_sha256 = str(valuation.get("valuation_content_sha256") or "")
        if (
            _SHA256.fullmatch(valuation_content_sha256) is None
            or valuation_path.parent != session_root / "objects" / "paper_valuation"
            or valuation_path.name != f"{valuation_content_sha256[7:]}.json"
        ):
            raise ValueError("forward valuation object path changed")
        valuation_document = _verified_json_document(valuation_path)
        validated_valuation = validate_human_paper_valuation_document(
            valuation_document
        )
        settlement = evidence.get("human_paper_settlement")
        if (
            not isinstance(settlement, Mapping)
            or str(settlement.get("status") or "")
            not in {
                "NO_PENDING_VIRTUAL_INTENTS",
                "VIRTUAL_SETTLEMENT_READY",
                "VIRTUAL_SETTLEMENT_PARTIALLY_BLOCKED",
                "VIRTUAL_SETTLEMENT_PARTIALLY_BLOCKED_BY_CAUSAL_GAP",
                "VIRTUAL_SETTLEMENT_PARTIALLY_BLOCKED_BY_ENTRY_SELECTION_EVIDENCE",
                "VIRTUAL_SETTLEMENT_PARTIALLY_BLOCKED_BY_POSITION_MARKS",
                "VIRTUAL_SETTLEMENT_PARTIALLY_BLOCKED_BY_EXECUTION_FACTS",
                "VIRTUAL_SETTLEMENT_PARTIALLY_BLOCKED_BY_SECURITY_GATE",
                "VIRTUAL_SETTLEMENT_PARTIALLY_BLOCKED_BY_1M_GRID",
                "VIRTUAL_SETTLEMENT_BLOCKED_BY_CAUSAL_GAP",
                "VIRTUAL_SETTLEMENT_BLOCKED_BY_ENTRY_SELECTION_EVIDENCE",
            }
            or not _human_paper_entry_selection_gate_proven(settlement)
            or _SHA256.fullmatch(str(settlement.get("content_sha256") or "")) is None
            or settlement.get("broker_transport_available") is not False
            or settlement.get("live_status") != "LIVE_DISABLED"
            or validated_valuation.get("session") != session.isoformat()
            or validated_valuation.get("all_complete") is not True
            or validated_valuation.get("equity_curve_point_available") is not True
            or validated_valuation.get("content_sha256") != valuation_content_sha256
            or validated_valuation.get("paper_ledger_content_sha256")
            != settlement.get("content_sha256")
            or valuation.get("paper_ledger_content_sha256")
            != validated_valuation.get("paper_ledger_content_sha256")
            or valuation.get("accounting_content_sha256")
            != validated_valuation.get("accounting_content_sha256")
        ):
            raise ValueError("forward valuation object identity changed")
        selection_gate = settlement.get("entry_selection_settlement_gate")
        if selection_gate is not None:
            if not isinstance(selection_gate, Mapping):
                raise ValueError("forward entry selection gate is invalid")
            ledger_archive = selection_gate.get("paper_ledger_prefix_archive")
            archived_ledger = _selection_paper_ledger_prefix_archive_file(
                ledger_archive,
                session_root=session_root,
            )
            if archived_ledger is None:
                raise ValueError("forward human paper ledger archive changed")
            source_archive = selection_gate.get("source_report_archive")
            if not _selection_source_report_archive_files_proven(
                source_archive,
                session_root=session_root,
                paper_ledger_events=archived_ledger["events"],
            ):
                raise ValueError("forward entry source report archive changed")
    except (OSError, TypeError, ValueError):
        return False, "EVALUATION_ARTIFACT_EVIDENCE_INVALID"
    return True, "READY"


def audit_forward_paper_session_delivery(
    events: Sequence[Mapping[str, object]],
    *,
    session: date,
    observed_at: datetime,
    capture_due: time = time(9, 10),
    evaluate_due: time = time(15, 20),
    evaluation_wait: timedelta = timedelta(minutes=460),
    sector_capture_readiness: Mapping[str, object] | None = None,
    trading_session_evidence: Mapping[str, object] | None = None,
    forward_root: Path | None = None,
) -> dict[str, object]:
    """Prove whether one forward session was actually captured and archived.

    ``forward_archive`` is a *pre-evaluation* data gate: it proves that the
    screen and the same-session QMT sector capture are usable.  It does not
    prove that the scheduled Capture/Evaluate processes ran.  This audit uses
    only the already validated, hash-chained forward ledger and therefore
    keeps those two meanings separate.

    Evaluate starts at 15:20 and may wait until 23:00 for the post-close
    full-market scan.  The former 240-minute deadline ended at 19:20, while an
    observed 4,941-symbol run can legitimately finish around 22:00; it therefore
    reported a healthy, progressing day as overdue.  A missing ``EVALUATED``
    event is now called overdue only after the same 23:00 boundary as the
    post-close preselection window.  Whether the date is required is resolved
    only from validated trading-session evidence.  Missing or unpublished QMT
    calendar facts stay tri-state ``UNRESOLVED`` and can never fall back to a
    Monday-Friday approximation.
    """

    if isinstance(session, datetime) or not isinstance(session, date):
        raise TypeError("session must be a date")
    observed = normalize_datetime(observed_at, "observed_at").astimezone(_CN)
    if not isinstance(capture_due, time) or capture_due.tzinfo is not None:
        raise TypeError("capture_due must be a naive time")
    if not isinstance(evaluate_due, time) or evaluate_due.tzinfo is not None:
        raise TypeError("evaluate_due must be a naive time")
    if evaluation_wait < timedelta(0):
        raise ValueError("evaluation_wait cannot be negative")

    requirement = resolve_trading_session_requirement(
        trading_session_evidence,
        session=session,
        observed_at=observed,
    )

    capture_due_at = datetime.combine(session, capture_due, tzinfo=_CN)
    evaluate_due_at = datetime.combine(session, evaluate_due, tzinfo=_CN)
    evaluation_deadline_at = evaluate_due_at + evaluation_wait
    session_events: list[Mapping[str, object]] = []
    for event in events:
        if not isinstance(event, Mapping):
            raise ValueError("forward delivery event is invalid")
        if str(event.get("session") or "") != session.isoformat():
            continue
        phase = str(event.get("phase") or "")
        status = str(event.get("status") or "")
        if phase not in _PHASES or status not in _STATUSES:
            raise ValueError("forward delivery event phase/status is invalid")
        event_hash = str(event.get("event_sha256") or "")
        if _SHA256.fullmatch(event_hash) is None:
            raise ValueError("forward delivery event identity is invalid")
        session_events.append(event)

    def latest(*statuses: str) -> Mapping[str, object] | None:
        accepted = frozenset(statuses)
        return next(
            (
                event
                for event in reversed(session_events)
                if str(event.get("status")) in accepted
            ),
            None,
        )

    captured = latest("CAPTURED")
    capture_failed = latest("CAPTURE_FAILED")
    evaluated = latest("EVALUATED")
    data_ready = latest("DATA_READY")
    latest_evaluation_progress = latest(
        "EVALUATED",
        "EVALUATION_BLOCKED",
        "DATA_BLOCKED",
        "DATA_READY",
    )
    blocked = (
        latest_evaluation_progress
        if latest_evaluation_progress is not None
        and latest_evaluation_progress.get("status")
        in {"EVALUATION_BLOCKED", "DATA_BLOCKED"}
        else None
    )
    capture_event_present = captured is not None
    evaluation_event_present = evaluated is not None
    data_ready_event_present = data_ready is not None
    capture_evidence_proven = (
        False
        if captured is None
        else _capture_delivery_evidence_proven(
            captured,
            session=session,
            readiness=sector_capture_readiness,
        )
    )
    data_ready_evidence_proven = False
    if data_ready is not None and captured is not None:
        receipt = captured.get("evidence")
        receipt = receipt.get("receipt") if isinstance(receipt, Mapping) else None
        if isinstance(receipt, Mapping):
            data_ready_evidence_proven = _data_ready_delivery_evidence_proven(
                data_ready,
                session=session,
                catalog_entry_sha256=str(receipt.get("entry_sha256") or ""),
                catalog_captured_at=str(receipt.get("captured_at") or ""),
            )
    evaluation_artifacts_proven = False
    evaluation_artifact_reason = "EVALUATION_ARTIFACTS_UNAVAILABLE"
    if evaluated is not None and forward_root is not None:
        (
            evaluation_artifacts_proven,
            evaluation_artifact_reason,
        ) = _evaluation_artifacts_proof(
            evaluated,
            session=session,
            forward_root=forward_root,
        )
    capture_implementation_provenance_id = _event_implementation_provenance_identity(
        captured
    )
    data_ready_implementation_provenance_id = _event_implementation_provenance_identity(
        data_ready
    )
    evaluation_implementation_provenance_id = _event_implementation_provenance_identity(
        evaluated
    )
    implementation_provenance_ids = (
        capture_implementation_provenance_id,
        data_ready_implementation_provenance_id,
        evaluation_implementation_provenance_id,
    )
    implementation_provenance_present = all(
        value is not None for value in implementation_provenance_ids
    )
    implementation_provenance_proven = bool(
        implementation_provenance_present
        and len(set(implementation_provenance_ids)) == 1
    )
    capture_ready = capture_event_present and capture_evidence_proven
    evaluation_ready = evaluation_event_present and evaluation_artifacts_proven
    required = requirement["required"]

    event_indexes = {id(event): index for index, event in enumerate(session_events)}
    sequence_valid = bool(
        captured is not None
        and data_ready is not None
        and evaluated is not None
        and event_indexes[id(captured)]
        < event_indexes[id(data_ready)]
        < event_indexes[id(evaluated)]
    )
    if evaluated is not None:
        latest_gate_before_evaluation = next(
            (
                event
                for event in reversed(session_events[: event_indexes[id(evaluated)]])
                if event.get("phase") == "DATA_GATE"
            ),
            None,
        )
        sequence_valid = bool(
            sequence_valid
            and latest_gate_before_evaluation is data_ready
            and data_ready.get("status") == "DATA_READY"
        )

    if required is None:
        status = "unresolved"
        reason_code = (
            "TRADING_SESSION_EVIDENCE_INVALID"
            if requirement["trading_session_reason_code"]
            == "TRADING_SESSION_EVIDENCE_INVALID"
            else "TRADING_SESSION_EVIDENCE_UNAVAILABLE"
        )
        ready = False
    elif required is False:
        status = "not_due"
        reason_code = "NON_TRADING_SESSION_NOT_DUE"
        ready = False
    elif evaluation_event_present and not capture_event_present:
        status = "not_ready"
        reason_code = "EVALUATED_WITHOUT_CAPTURE_EVENT"
        ready = False
    elif evaluation_event_present and not data_ready_event_present:
        status = "not_ready"
        reason_code = "DATA_READY_EVENT_MISSING"
        ready = False
    elif evaluation_event_present and not sequence_valid:
        status = "not_ready"
        reason_code = "FORWARD_EVENT_SEQUENCE_INVALID"
        ready = False
    elif evaluation_event_present and not implementation_provenance_present:
        status = "not_ready"
        reason_code = "IMPLEMENTATION_PROVENANCE_UNATTESTED"
        ready = False
    elif evaluation_event_present and not implementation_provenance_proven:
        status = "not_ready"
        reason_code = "MIXED_IMPLEMENTATION_PROVENANCE"
        ready = False
    elif evaluation_event_present and not capture_evidence_proven:
        status = "not_ready"
        reason_code = "CAPTURE_EVENT_EVIDENCE_UNPROVEN"
        ready = False
    elif evaluation_event_present and not data_ready_evidence_proven:
        status = "not_ready"
        reason_code = "DATA_READY_EVENT_EVIDENCE_INVALID"
        ready = False
    elif evaluation_event_present and forward_root is None:
        status = "not_ready"
        reason_code = "EVALUATION_ARTIFACTS_UNAVAILABLE"
        ready = False
    elif evaluation_event_present and not evaluation_artifacts_proven:
        status = "not_ready"
        reason_code = evaluation_artifact_reason
        ready = False
    elif capture_ready and evaluation_ready:
        status = "ready"
        reason_code = "READY"
        ready = True
    elif not capture_event_present and observed < capture_due_at:
        status = "not_due"
        reason_code = "CAPTURE_NOT_DUE"
        ready = False
    elif not capture_event_present and capture_failed is not None:
        status = "not_ready"
        reason_code = "CAPTURE_FAILED"
        ready = False
    elif not capture_event_present:
        status = "not_ready"
        reason_code = "CAPTURE_MISSING_AFTER_DUE"
        ready = False
    elif blocked is not None:
        status = "not_ready"
        reason_code = str(blocked["status"])
        ready = False
    elif observed < evaluate_due_at:
        status = "pending"
        reason_code = "CAPTURED_WAITING_FOR_EVALUATION"
        ready = False
    elif observed <= evaluation_deadline_at:
        status = "pending"
        reason_code = "EVALUATION_PENDING"
        ready = False
    else:
        status = "not_ready"
        reason_code = "EVALUATION_MISSING_AFTER_DEADLINE"
        ready = False

    terminal = (
        evaluated or blocked or (capture_failed if not capture_event_present else None)
    )
    return {
        "schema": FORWARD_PAPER_SESSION_DELIVERY_SCHEMA,
        "required": required,
        "requirement_resolved": requirement["requirement_resolved"],
        "trading_session_status": requirement["trading_session_status"],
        "trading_session_reason_code": requirement["trading_session_reason_code"],
        "trading_session_evidence_proven": requirement[
            "trading_session_evidence_proven"
        ],
        "trading_session_evidence": requirement["trading_session_evidence"],
        "ready": ready,
        "status": status,
        "reason_code": reason_code,
        "session": session.isoformat(),
        "observed_at": observed.isoformat(),
        "capture_due_at": capture_due_at.isoformat(),
        "evaluate_due_at": evaluate_due_at.isoformat(),
        "evaluation_deadline_at": evaluation_deadline_at.isoformat(),
        "session_event_count": len(session_events),
        "capture_event_present": capture_event_present,
        "data_ready_event_present": data_ready_event_present,
        "evaluation_event_present": evaluation_event_present,
        "capture_ready": capture_ready,
        "evaluation_ready": evaluation_ready,
        "capture_evidence_proven": capture_evidence_proven,
        "data_ready_evidence_proven": data_ready_evidence_proven,
        "evaluation_artifacts_proven": evaluation_artifacts_proven,
        "implementation_provenance_present": (implementation_provenance_present),
        "implementation_provenance_proven": implementation_provenance_proven,
        "capture_implementation_provenance_id": (capture_implementation_provenance_id),
        "data_ready_implementation_provenance_id": (
            data_ready_implementation_provenance_id
        ),
        "evaluation_implementation_provenance_id": (
            evaluation_implementation_provenance_id
        ),
        "capture_event_sha256": (
            None if captured is None else str(captured["event_sha256"])
        ),
        "evaluation_event_sha256": (
            None if evaluated is None else str(evaluated["event_sha256"])
        ),
        "data_ready_event_sha256": (
            None if data_ready is None else str(data_ready["event_sha256"])
        ),
        "latest_terminal_event_status": (
            None if terminal is None else str(terminal["status"])
        ),
        "latest_terminal_event_sha256": (
            None if terminal is None else str(terminal["event_sha256"])
        ),
        "real_account_accessed": False,
        "real_order_transport_enabled": False,
        "paper_status": "REVIEW_REQUIRED",
        "live_status": "LIVE_DISABLED",
    }


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return "sha256:" + digest.hexdigest()


@dataclass(frozen=True, slots=True)
class ForwardPaperContract:
    strategy_parameter_set_id: str
    strategy_parameter_snapshot_sha256: str
    selection_path: str
    strategic_frequency: str
    tactical_frequency: str
    locator_frequency: str
    recursive_levels: tuple[int, int, int]
    initial_cash: str
    slot_count: int
    slot_fraction: str
    account_exposure_cap: str
    tactical_ratio: str
    technical_mode: str = "HUMAN_REVIEW_SCREENING"
    technical_approximation_parameter_set_id: str = (
        FROZEN_TECHNICAL_APPROXIMATION_PARAMETER_SET_ID
    )
    technical_alignment_parameter_set_id: str = (
        FROZEN_TECHNICAL_ALIGNMENT_PARAMETER_SET_ID
    )
    human_review_screening_parameter_set_id: str = (
        FROZEN_HUMAN_REVIEW_SCREENING_PARAMETER_SET_ID
    )
    tick_data_used: bool = False
    three_program_enabled: bool = False
    completed_one_minute_execution_only: bool = True
    signal_bar_fill_allowed: bool = False
    real_account_access: bool = False
    real_order_transport: bool = False
    highest_status: str = "REVIEW_REQUIRED"
    live_status: str = "LIVE_DISABLED"
    schema: str = FORWARD_PAPER_CONTRACT_SCHEMA

    def __post_init__(self) -> None:
        if self.strategy_parameter_set_id != FROZEN_RESEARCH_PARAMETER_SET_ID:
            raise ValueError("forward paper strategy parameter identity changed")
        if _SHA256.fullmatch(self.strategy_parameter_snapshot_sha256) is None:
            raise ValueError("forward paper parameter snapshot hash is invalid")
        if (
            self.selection_path,
            self.strategic_frequency,
            self.tactical_frequency,
            self.locator_frequency,
            self.recursive_levels,
        ) != (
            "QMT_CURRENT_SECTOR_TECHNICAL_ONLY",
            "30m",
            "5m",
            "1m",
            (2, 1, 0),
        ):
            raise ValueError("forward paper timeframe/selection mapping changed")
        if (
            self.initial_cash,
            self.slot_count,
            self.slot_fraction,
            self.account_exposure_cap,
            self.tactical_ratio,
        ) != ("1000000", 5, "0.18", "0.90", "0.25"):
            raise ValueError("forward paper capital parameters changed")
        if (
            self.tick_data_used
            or self.three_program_enabled
            or not self.completed_one_minute_execution_only
            or self.signal_bar_fill_allowed
            or self.real_account_access
            or self.real_order_transport
            or self.highest_status != "REVIEW_REQUIRED"
            or self.live_status != "LIVE_DISABLED"
        ):
            raise ValueError("forward paper safety contract changed")
        if (
            self.technical_mode != "HUMAN_REVIEW_SCREENING"
            or self.technical_approximation_parameter_set_id
            != FROZEN_TECHNICAL_APPROXIMATION_PARAMETER_SET_ID
            or self.technical_alignment_parameter_set_id
            != FROZEN_TECHNICAL_ALIGNMENT_PARAMETER_SET_ID
            or self.human_review_screening_parameter_set_id
            != FROZEN_HUMAN_REVIEW_SCREENING_PARAMETER_SET_ID
            or self.schema != FORWARD_PAPER_CONTRACT_SCHEMA
        ):
            raise ValueError("human review forward paper contract changed")

    @property
    def contract_id(self) -> str:
        return sha256_json(self._stable_document())

    def _stable_document(self) -> dict[str, object]:
        values: dict[str, object] = asdict(self)
        # JSON has no tuple type.  Store the canonical wire representation so
        # a write/read round trip cannot look like a contract mutation.
        values["recursive_levels"] = list(self.recursive_levels)
        return values

    def document(self) -> dict[str, object]:
        return {**self._stable_document(), "contract_id": self.contract_id}

    @property
    def operational_status(self) -> str:
        return "REVIEW_REQUIRED"


def load_frozen_forward_contract(parameter_snapshot_path: Path) -> ForwardPaperContract:
    try:
        payload = json.loads(parameter_snapshot_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(
            "frozen recent-year parameter snapshot cannot be read"
        ) from exc
    if not isinstance(payload, Mapping):
        raise ValueError("frozen recent-year parameter snapshot is invalid")
    required = {
        "parameter_set_id": FROZEN_RESEARCH_PARAMETER_SET_ID,
        "selection_path": "QMT_CURRENT_SECTOR_TECHNICAL_ONLY",
        "strategic_frequency": "30m",
        "tactical_frequency": "5m",
        "locator_frequency": "1m",
        "strategic_recursive_level": 2,
        "tactical_recursive_level": 1,
        "locator_recursive_level": 0,
        "initial_cash": "1000000",
        "slot_count": 5,
        "slot_fraction": "0.18",
        "account_exposure_cap": "0.90",
        "tactical_ratio": "0.25",
        "tick_data_used": False,
        "three_program_mode": "DISABLED_USER_AUTHORIZED",
        "execution_observation": "COMPLETED_1M_BAR",
        "signal_bar_fill_allowed": False,
        "live_status": "LIVE_DISABLED",
        "technical_mode": "HUMAN_REVIEW_SCREENING",
        "technical_approximation_parameter_set_id": (
            FROZEN_TECHNICAL_APPROXIMATION_PARAMETER_SET_ID
        ),
        "technical_alignment_parameter_set_id": (
            FROZEN_TECHNICAL_ALIGNMENT_PARAMETER_SET_ID
        ),
        "human_review_screening_parameter_set_id": (
            FROZEN_HUMAN_REVIEW_SCREENING_PARAMETER_SET_ID
        ),
        "highest_status": "REVIEW_REQUIRED",
        "automated_order_authorized": False,
        "human_confirmation_required": True,
        "portfolio_backtest_performed": False,
    }
    changed = tuple(
        key for key, expected in required.items() if payload.get(key) != expected
    )
    if changed:
        raise ValueError(
            "frozen recent-year parameter snapshot changed: " + ", ".join(changed)
        )
    return ForwardPaperContract(
        strategy_parameter_set_id=str(payload["parameter_set_id"]),
        strategy_parameter_snapshot_sha256=sha256_file(parameter_snapshot_path),
        selection_path=str(payload["selection_path"]),
        strategic_frequency=str(payload["strategic_frequency"]),
        tactical_frequency=str(payload["tactical_frequency"]),
        locator_frequency=str(payload["locator_frequency"]),
        recursive_levels=(
            int(payload["strategic_recursive_level"]),
            int(payload["tactical_recursive_level"]),
            int(payload["locator_recursive_level"]),
        ),
        initial_cash=str(payload["initial_cash"]),
        slot_count=int(payload["slot_count"]),
        slot_fraction=str(payload["slot_fraction"]),
        account_exposure_cap=str(payload["account_exposure_cap"]),
        tactical_ratio=str(payload["tactical_ratio"]),
        technical_mode=str(payload["technical_mode"]),
        technical_approximation_parameter_set_id=str(
            payload["technical_approximation_parameter_set_id"]
        ),
        technical_alignment_parameter_set_id=str(
            payload["technical_alignment_parameter_set_id"]
        ),
        human_review_screening_parameter_set_id=str(
            payload["human_review_screening_parameter_set_id"]
        ),
        highest_status=str(payload["highest_status"]),
        schema=FORWARD_PAPER_CONTRACT_SCHEMA,
    )


def _ledger_document(
    contract: ForwardPaperContract,
    events: Sequence[Mapping[str, object]],
) -> dict[str, object]:
    stable: dict[str, object] = {
        "schema": FORWARD_PAPER_LEDGER_SCHEMA,
        "contract": contract.document(),
        "events": tuple(dict(value) for value in events),
        "paper_status": contract.operational_status,
        "live_status": "LIVE_DISABLED",
    }
    return {**stable, "content_sha256": sha256_json(stable)}


def validate_forward_paper_ledger(
    payload: Mapping[str, object],
    *,
    contract: ForwardPaperContract,
) -> dict[str, object]:
    if payload.get("schema") != FORWARD_PAPER_LEDGER_SCHEMA:
        raise ValueError("unsupported forward paper ledger schema")
    if payload.get("contract") != contract.document():
        raise ValueError("forward paper ledger contract changed")
    if (
        payload.get("paper_status") != contract.operational_status
        or payload.get("live_status") != "LIVE_DISABLED"
    ):
        raise ValueError("forward paper ledger safety status changed")
    raw_events = payload.get("events")
    if not isinstance(raw_events, (list, tuple)):
        raise ValueError("forward paper events are unavailable")
    previous_hash: str | None = None
    previous_time: datetime | None = None
    events: list[dict[str, object]] = []
    for raw in raw_events:
        if not isinstance(raw, Mapping):
            raise ValueError("forward paper event is invalid")
        stable = {key: raw[key] for key in raw if key != "event_sha256"}
        event_hash = str(raw.get("event_sha256") or "")
        if _SHA256.fullmatch(event_hash) is None or event_hash != sha256_json(stable):
            raise ValueError("forward paper event hash changed")
        if raw.get("schema") != FORWARD_PAPER_EVENT_SCHEMA:
            raise ValueError("unsupported forward paper event schema")
        if raw.get("contract_id") != contract.contract_id:
            raise ValueError("forward paper event contract changed")
        if raw.get("strategy_parameter_set_id") != contract.strategy_parameter_set_id:
            raise ValueError("forward paper event parameter identity changed")
        if raw.get("previous_event_sha256") != previous_hash:
            raise ValueError("forward paper event hash chain is broken")
        if raw.get("phase") not in _PHASES or raw.get("status") not in _STATUSES:
            raise ValueError("forward paper event phase/status is invalid")
        try:
            session = date.fromisoformat(str(raw["session"]))
            recorded = normalize_datetime(
                datetime.fromisoformat(str(raw["recorded_at"])),
                "recorded_at",
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError("forward paper event time is invalid") from exc
        if recorded.date() < session:
            raise ValueError("forward paper event predates its session")
        if previous_time is not None and recorded < previous_time:
            raise ValueError("forward paper events are not chronological")
        if raw.get("real_account_accessed") is not False:
            raise ValueError("forward paper event touched a real account")
        if raw.get("real_order_transport_enabled") is not False:
            raise ValueError("forward paper event enabled an order transport")
        evidence = raw.get("evidence")
        if not isinstance(evidence, Mapping):
            raise ValueError("forward paper event evidence is invalid")
        if raw.get("evidence_sha256") != sha256_json(dict(evidence)):
            raise ValueError("forward paper event evidence hash changed")
        if (
            raw.get("paper_status") != contract.operational_status
            or raw.get("live_status") != "LIVE_DISABLED"
        ):
            raise ValueError("forward paper event safety status changed")
        if "implementation_provenance" in evidence:
            provenance = evidence.get("implementation_provenance")
            if not isinstance(provenance, Mapping):
                raise ValueError("forward implementation provenance is invalid")
            validate_forward_implementation_provenance(provenance)
        events.append(dict(raw))
        previous_hash = event_hash
        previous_time = recorded
    expected = _ledger_document(contract, events)
    if payload.get("content_sha256") != expected["content_sha256"]:
        raise ValueError("forward paper ledger content hash changed")
    return expected


def load_forward_paper_ledger(
    path: Path,
    *,
    contract: ForwardPaperContract,
) -> dict[str, object]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError("forward paper ledger cannot be read") from exc
    if not isinstance(payload, Mapping):
        raise ValueError("forward paper ledger document is invalid")
    return validate_forward_paper_ledger(payload, contract=contract)


def _append_forward_paper_event_unlocked(
    path: Path,
    *,
    contract: ForwardPaperContract,
    session: date,
    phase: str,
    status: str,
    evidence: Mapping[str, object],
    recorded_at: datetime,
) -> tuple[dict[str, object], dict[str, object], bool]:
    if phase not in _PHASES or status not in _STATUSES:
        raise ValueError("unsupported forward paper phase/status")
    if isinstance(session, datetime) or not isinstance(session, date):
        raise TypeError("forward paper session must be a date")
    recorded = normalize_datetime(recorded_at, "recorded_at")
    if recorded.date() < session:
        raise ValueError("forward paper event predates its session")
    existing = (
        load_forward_paper_ledger(path, contract=contract)
        if path.is_file()
        else _ledger_document(contract, ())
    )
    events = list(existing["events"])
    if events:
        previous_recorded = normalize_datetime(
            datetime.fromisoformat(str(events[-1]["recorded_at"])),
            "previous_recorded_at",
        )
        if recorded < previous_recorded:
            raise ValueError("forward paper events are not chronological")
    evidence_value = dict(evidence)
    evidence_sha256 = sha256_json(evidence_value)
    latest_phase_event = next(
        (
            row
            for row in reversed(events)
            if row["session"] == session.isoformat() and row["phase"] == phase
        ),
        None,
    )
    if (
        latest_phase_event is not None
        and latest_phase_event["status"] == status
        and latest_phase_event["evidence_sha256"] == evidence_sha256
    ):
        return existing, dict(latest_phase_event), True
    previous_hash = None if not events else str(events[-1]["event_sha256"])
    stable: dict[str, object] = {
        "schema": FORWARD_PAPER_EVENT_SCHEMA,
        "session": session.isoformat(),
        "recorded_at": recorded.isoformat(),
        "phase": phase,
        "status": status,
        "contract_id": contract.contract_id,
        "strategy_parameter_set_id": contract.strategy_parameter_set_id,
        "previous_event_sha256": previous_hash,
        "evidence": evidence_value,
        "evidence_sha256": evidence_sha256,
        "real_account_accessed": False,
        "real_order_transport_enabled": False,
        "paper_status": contract.operational_status,
        "live_status": "LIVE_DISABLED",
    }
    event = {**stable, "event_sha256": sha256_json(stable)}
    events.append(event)
    document = validate_forward_paper_ledger(
        _ledger_document(contract, events),
        contract=contract,
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8", newline="\n") as handle:
        json.dump(document, handle, ensure_ascii=False, sort_keys=True, indent=2)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)
    return document, event, False


def append_forward_paper_event(
    path: Path,
    *,
    contract: ForwardPaperContract,
    session: date,
    phase: str,
    status: str,
    evidence: Mapping[str, object],
    recorded_at: datetime,
) -> tuple[dict[str, object], dict[str, object], bool]:
    """Append one event under an OS-level lock shared by every process."""

    lock_path = path.with_suffix(path.suffix + ".lock")
    with interprocess_file_lock(lock_path):
        return _append_forward_paper_event_unlocked(
            path,
            contract=contract,
            session=session,
            phase=phase,
            status=status,
            evidence=evidence,
            recorded_at=recorded_at,
        )


__all__ = (
    "FORWARD_IMPLEMENTATION_CONTINUITY_SCHEMA",
    "FORWARD_IMPLEMENTATION_PROVENANCE_SCHEMA",
    "FORWARD_PAPER_CONTRACT_SCHEMA",
    "FORWARD_PAPER_EVENT_SCHEMA",
    "FORWARD_PAPER_LEDGER_SCHEMA",
    "FORWARD_PAPER_SESSION_DELIVERY_SCHEMA",
    "FROZEN_RESEARCH_PARAMETER_SET_ID",
    "FROZEN_HUMAN_REVIEW_SCREENING_PARAMETER_SET_ID",
    "FROZEN_TECHNICAL_ALIGNMENT_PARAMETER_SET_ID",
    "FROZEN_TECHNICAL_APPROXIMATION_PARAMETER_SET_ID",
    "ForwardPaperContract",
    "append_forward_paper_event",
    "audit_forward_implementation_continuity",
    "audit_forward_paper_session_delivery",
    "load_forward_paper_ledger",
    "load_frozen_forward_contract",
    "sha256_file",
    "validate_forward_implementation_provenance",
    "validate_forward_paper_ledger",
)
