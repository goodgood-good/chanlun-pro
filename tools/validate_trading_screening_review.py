"""Validate one immutable live-screening review boundary in a child process.

The tool is deliberately read-only.  ``app.py`` uses it only for large
full-market publications so the CPU-heavy semantic walk cannot monopolize the
Web interpreter's GIL.
"""

from __future__ import annotations

import argparse
from collections.abc import Mapping, Sequence
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import re
import sys
from uuid import uuid4
from zoneinfo import ZoneInfo


PROJECT_ROOT = Path(__file__).resolve().parents[1]
for value in (PROJECT_ROOT / "src", PROJECT_ROOT / "web" / "chanlun_chart"):
    rendered = str(value)
    if rendered not in sys.path:
        sys.path.insert(0, rendered)

from chanlun.decision_support.trading_system.v3_live_human_review import (
    live_screening_snapshot_content_sha256,
    live_human_review_document,
    validate_live_review_snapshot,
)
from chanlun.decision_support.fingerprints import sha256_json
from chanlun.decision_support.trading_system.decision_source_provenance import (
    current_decision_source_snapshot,
)
from chanlun.decision_support.trading_system.file_lock import (
    interprocess_file_lock,
)
from chanlun.decision_support.trading_system.live_review_materialization import (
    LIVE_REVIEW_CANDIDATE_DETAIL_SCHEMA,
    LIVE_REVIEW_WEB_INDEX_SCHEMA,
    live_review_materialization_receipt,
    live_review_web_bundle_receipt,
)
from chanlun.decision_support.trading_system.v3_human_review_screening import (
    HumanReviewAlert,
    parse_human_review_alert,
    validate_human_review_screen_document,
)


_SHA256 = re.compile(r"^sha256:[0-9a-f]{64}$")


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--input", type=Path, required=True)
    result.add_argument("--expected-sha256", required=True)
    result.add_argument("--archive-root", type=Path)
    result.add_argument("--repository-root", type=Path)
    return result


def _write_json_atomic(path: Path, payload: Mapping[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.{uuid4().hex}.tmp")
    try:
        with temporary.open("x", encoding="utf-8", newline="\n") as handle:
            json.dump(
                payload,
                handle,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return "sha256:" + digest.hexdigest()


def _evidence_id(value: object) -> str | None:
    if not isinstance(value, Mapping):
        return None
    evidence_id = value.get("evidence_id")
    return evidence_id if isinstance(evidence_id, str) else None


def _write_detail_store(
    path: Path,
    *,
    report_content_sha256: str,
    queue: Sequence[object],
) -> dict[str, dict[str, object]]:
    """Write canonical JSONL candidates and return exact random-access spans."""

    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.{uuid4().hex}.tmp")
    locators: dict[str, dict[str, object]] = {}
    try:
        with temporary.open("xb") as handle:
            for raw in queue:
                if not isinstance(raw, Mapping):
                    raise ValueError("human review candidate is not portable")
                candidate_id = raw.get("candidate_id")
                if (
                    not isinstance(candidate_id, str)
                    or _SHA256.fullmatch(candidate_id) is None
                    or candidate_id in locators
                ):
                    raise ValueError("human review candidate identity is invalid")
                detail = {
                    "schema": LIVE_REVIEW_CANDIDATE_DETAIL_SCHEMA,
                    "source_report_content_sha256": report_content_sha256,
                    "candidate_id": candidate_id,
                    "candidate": raw,
                    "highest_status": "REVIEW_REQUIRED",
                    "automated_order_authorized": False,
                    "real_account_accessed": False,
                    "real_order_transport_enabled": False,
                    "live_status": "LIVE_DISABLED",
                }
                encoded = (
                    json.dumps(
                        detail,
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    ).encode("utf-8")
                    + b"\n"
                )
                offset = handle.tell()
                handle.write(encoded)
                locators[candidate_id] = {
                    "offset": offset,
                    "length": len(encoded),
                    "line_sha256": "sha256:"
                    + hashlib.sha256(encoded).hexdigest(),
                }
            handle.flush()
            os.fsync(handle.fileno())
        if path.is_file():
            if _sha256_file(path) != _sha256_file(temporary):
                raise ValueError("live review detail store collision")
        else:
            os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
    return locators


def _materialize_web_bundle(
    *,
    report: Mapping[str, object],
    archive_root: Path,
    source_snapshot_content_sha256: str,
    decision_source_snapshot_id: str,
    alerts: Sequence[HumanReviewAlert] | None = None,
) -> tuple[Path, Path, dict[str, object]]:
    """Project one validated report into a compact index plus detail JSONL."""

    report_content_sha256 = str(report["content_sha256"])
    raw_queue = report.get("review_queue")
    if not isinstance(raw_queue, list):
        raise ValueError("human review queue is unavailable for Web bundle")
    parsed_alerts = (
        tuple(parse_human_review_alert(value) for value in raw_queue)
        if alerts is None
        else tuple(alerts)
    )
    alert_by_candidate_id = {
        value.candidate_id: value for value in parsed_alerts
    }
    if len(alert_by_candidate_id) != len(raw_queue):
        raise ValueError("human review Web bundle candidate set changed")
    artifact_root = archive_root / ".web"
    stem = report_content_sha256.removeprefix("sha256:")
    index_path = artifact_root / f"{stem}.index.json"
    detail_path = artifact_root / f"{stem}.details.jsonl"
    locators = _write_detail_store(
        detail_path,
        report_content_sha256=report_content_sha256,
        queue=raw_queue,
    )
    compact_queue: list[dict[str, object]] = []
    for raw in raw_queue:
        if not isinstance(raw, Mapping):
            raise ValueError("human review candidate is not portable")
        candidate_id = str(raw["candidate_id"])
        alert = alert_by_candidate_id.get(candidate_id)
        if alert is None:
            raise ValueError("human review Web bundle candidate is unavailable")
        compact = dict(raw)
        compact.setdefault("signal_lifecycle_id", alert.signal_lifecycle_id)
        sector_evidence = compact.pop("sector_higher_timeframe_evidence", None)
        market_symbol_evidence = compact.pop(
            "market_symbol_higher_timeframe_evidence",
            None,
        )
        ranking_evidence = compact.get("sector_ranking_evidence")
        market_symbol_support_count = 0
        if isinstance(market_symbol_evidence, Mapping):
            for side_name in ("market", "symbol_evidence"):
                side = market_symbol_evidence.get(side_name)
                if isinstance(side, Mapping) and isinstance(
                    side.get("source_support"), Mapping
                ):
                    market_symbol_support_count += 1
        ranking_profile = (
            ranking_evidence.get("source_profile")
            if isinstance(ranking_evidence, Mapping)
            else None
        )
        compact.update(
            {
                "evidence_detail_available": bool(
                    sector_evidence is not None
                    or market_symbol_evidence is not None
                    or ranking_evidence is not None
                ),
                "sector_higher_timeframe_evidence_id": _evidence_id(
                    sector_evidence
                ),
                "market_symbol_higher_timeframe_evidence_id": _evidence_id(
                    market_symbol_evidence
                ),
                "sector_ranking_evidence_id": _evidence_id(ranking_evidence),
                "sector_risk_gate_attestation": (
                    "SELF_CONTAINED"
                    if "sector_risk_gate" in raw
                    else "LEGACY_OMITTED_FAIL_CLOSED"
                ),
                "market_symbol_higher_timeframe_attestation": (
                    "SELF_CONTAINED"
                    if market_symbol_evidence is not None
                    else "LEGACY_SUMMARY_ONLY"
                ),
                "market_symbol_higher_timeframe_source_attestation": (
                    "SELF_CONTAINED"
                    if market_symbol_support_count == 2
                    else (
                        "PARTIAL_SOURCE_SUPPORT"
                        if market_symbol_support_count == 1
                        else (
                            "STRUCTURE_ONLY"
                            if market_symbol_evidence is not None
                            else "LEGACY_SUMMARY_ONLY"
                        )
                    )
                ),
                "sector_ranking_attestation": (
                    "FULL_STRUCTURAL_COMPONENTS"
                    if ranking_profile == "LIVE_FULL_RANKING"
                    else (
                        "HISTORICAL_TRIGGER_SUMMARY_NO_COMPONENTS"
                        if ranking_evidence is not None
                        else "LEGACY_NOT_ATTACHED"
                    )
                ),
                "detail_locator": locators[candidate_id],
            }
        )
        compact_queue.append(compact)
    event_study = report.get("event_study")
    event_study_summary = (
        event_study.get("summary")
        if isinstance(event_study, Mapping)
        and isinstance(event_study.get("summary"), Mapping)
        else {}
    )
    stable: dict[str, object] = {
        "schema": LIVE_REVIEW_WEB_INDEX_SCHEMA,
        "source_report_content_sha256": report_content_sha256,
        "source_snapshot_content_sha256": source_snapshot_content_sha256,
        "decision_source_snapshot_id": decision_source_snapshot_id,
        "input_hashes": report.get("input_hashes") or {},
        "sample": report.get("sample") or {},
        "scope": report.get("scope") or {},
        "candidate_funnel": report.get("candidate_funnel") or {},
        "signal_counts": report.get("signal_counts") or {},
        "event_study_summary": event_study_summary,
        "data_caveats": list(report.get("data_caveats") or ()),
        "division_of_responsibility": (
            report.get("division_of_responsibility") or {}
        ),
        "review_queue": compact_queue,
        "review_queue_count": len(compact_queue),
        "highest_status": "REVIEW_REQUIRED",
        "human_confirmation_required": True,
        "automated_order_authorized": False,
        "orders_created": 0,
        "fills_created": 0,
        "live_status": "LIVE_DISABLED",
    }
    index = {**stable, "content_sha256": sha256_json(stable)}
    _write_json_atomic(index_path, index)
    return index_path, detail_path, index


def _materialize_human_review_report(
    *,
    payload: Mapping[str, object],
    source_path: Path,
    source_stat: os.stat_result,
    expected_sha256: str,
    archive_root: Path,
    repository_root: Path,
) -> dict[str, object]:
    try:
        review_at = datetime.fromisoformat(str(payload["as_of"]))
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("live screening review cutoff is invalid") from exc
    if review_at.tzinfo is None:
        raise ValueError("live screening review cutoff must be timezone-aware")
    session = review_at.astimezone(ZoneInfo("Asia/Shanghai")).date()
    decision_sources = current_decision_source_snapshot(repository_root)
    report = live_human_review_document(
        live_snapshot=payload,
        source_snapshot_sha256=expected_sha256,
        session=session,
        decision_source_snapshot=decision_sources,
    )
    validated_alerts = validate_human_review_screen_document(report)
    content_sha256 = str(report["content_sha256"])
    report_path = (
        archive_root
        / session.isoformat()
        / f"{content_sha256.removeprefix('sha256:')}.json"
    )
    receipt_path = archive_root / ".current_live_review.json"
    web_receipt_path = archive_root / ".current_live_review_web_bundle.json"
    lock_path = archive_root / ".live_review_archive.lock"
    current_stat = source_path.stat()
    if (
        current_stat.st_size != source_stat.st_size
        or current_stat.st_mtime_ns != source_stat.st_mtime_ns
    ):
        raise ValueError("live screening snapshot changed during validation")
    with interprocess_file_lock(lock_path, timeout_seconds=30.0):
        if report_path.is_file():
            existing = json.loads(report_path.read_text(encoding="utf-8"))
            validate_human_review_screen_document(existing)
            if existing.get("content_sha256") != content_sha256:
                raise ValueError("live human review archive collision")
        else:
            _write_json_atomic(report_path, report)
        report_stat = report_path.stat()
        input_hashes = report.get("input_hashes")
        decision_source_id = (
            input_hashes.get("decision_source_snapshot_id")
            if isinstance(input_hashes, Mapping)
            else None
        )
        if not isinstance(decision_source_id, str):
            raise ValueError(
                "live human review decision source identity is unavailable"
            )
        index_path, detail_path, web_index = _materialize_web_bundle(
            report=report,
            archive_root=archive_root,
            source_snapshot_content_sha256=expected_sha256,
            decision_source_snapshot_id=decision_source_id,
            alerts=validated_alerts,
        )
        receipt = live_review_materialization_receipt(
            source_path=source_path,
            source_stat=source_stat,
            source_snapshot_content_sha256=expected_sha256,
            report_path=report_path,
            report_stat=report_stat,
            report_content_sha256=content_sha256,
            decision_source_snapshot_id=decision_source_id,
            archive_root=archive_root,
        )
        web_receipt = live_review_web_bundle_receipt(
            source_path=source_path,
            source_stat=source_stat,
            source_snapshot_content_sha256=expected_sha256,
            report_path=report_path,
            report_stat=report_stat,
            report_content_sha256=content_sha256,
            index_path=index_path,
            index_stat=index_path.stat(),
            index_content_sha256=str(web_index["content_sha256"]),
            detail_path=detail_path,
            detail_stat=detail_path.stat(),
            decision_source_snapshot_id=decision_source_id,
            archive_root=archive_root,
        )
        _write_json_atomic(receipt_path, receipt)
        _write_json_atomic(web_receipt_path, web_receipt)
    return {
        "human_review_report_path": str(report_path.resolve()),
        "human_review_report_content_sha256": content_sha256,
        "materialization_receipt": str(receipt_path.resolve()),
        "human_review_web_index_path": str(index_path.resolve()),
        "human_review_detail_store_path": str(detail_path.resolve()),
        "web_bundle_receipt": str(web_receipt_path.resolve()),
    }


def validate_document(
    *,
    path: Path,
    expected_sha256: str,
    archive_root: Path | None = None,
    repository_root: Path | None = None,
) -> dict[str, object]:
    if _SHA256.fullmatch(expected_sha256) is None:
        raise ValueError("expected-sha256 must be a canonical sha256 identity")
    if (archive_root is None) != (repository_root is None):
        raise ValueError(
            "archive-root and repository-root must be provided together"
        )
    source_stat = path.stat()
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, Mapping):
        raise ValueError("screening snapshot must be a mapping")
    declared = payload.get("snapshot_content_sha256")
    if declared != expected_sha256:
        return {
            "schema": "chanlun-screening-review-validator/v1",
            "snapshot_content_sha256": expected_sha256,
            "ready": False,
            "reason_code": "SNAPSHOT_IDENTITY_INVALID",
            "real_account_accessed": False,
            "real_order_transport_enabled": False,
            "automated_order_authorized": False,
            "live_status": "LIVE_DISABLED",
        }
    materialization: dict[str, object] = {}
    error: str | None = None
    try:
        if live_screening_snapshot_content_sha256(payload) != expected_sha256:
            raise ValueError("screening snapshot content hash mismatch")
        if archive_root is None or repository_root is None:
            validate_live_review_snapshot(payload)
    except (ArithmeticError, KeyError, TypeError, ValueError) as exc:
        ready = False
        reason_code = "REVIEW_BOUNDARY_INVALID"
        error = f"{type(exc).__name__}: {str(exc)[:240]}"
    else:
        ready = True
        reason_code = "READY"
        if archive_root is not None and repository_root is not None:
            try:
                materialization = _materialize_human_review_report(
                    payload=payload,
                    source_path=path,
                    source_stat=source_stat,
                    expected_sha256=expected_sha256,
                    archive_root=archive_root.resolve(),
                    repository_root=repository_root.resolve(),
                )
            except (ArithmeticError, KeyError, OSError, TypeError, ValueError) as exc:
                ready = False
                reason_code = "HUMAN_REVIEW_MATERIALIZATION_FAILED"
                error = f"{type(exc).__name__}: {str(exc)[:240]}"
    return {
        "schema": "chanlun-screening-review-validator/v1",
        "snapshot_content_sha256": expected_sha256,
        "ready": ready,
        "reason_code": reason_code,
        "real_account_accessed": False,
        "real_order_transport_enabled": False,
        "automated_order_authorized": False,
        "live_status": "LIVE_DISABLED",
        **({} if error is None else {"error": error}),
        **materialization,
    }


def main(argv: Sequence[str] | None = None) -> int:
    args = parser().parse_args(argv)
    document = validate_document(
        path=args.input.resolve(),
        expected_sha256=args.expected_sha256,
        archive_root=(
            None if args.archive_root is None else args.archive_root.resolve()
        ),
        repository_root=(
            None
            if args.repository_root is None
            else args.repository_root.resolve()
        ),
    )
    print(
        json.dumps(
            document,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    )
    return 0 if document["ready"] is True else 3


if __name__ == "__main__":
    raise SystemExit(main())
