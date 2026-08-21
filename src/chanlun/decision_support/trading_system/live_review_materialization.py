"""Authenticated hand-off from the large screening validator to the Web app.

The full-market screening and human-review documents are intentionally large.
They are validated in an isolated child process so Flask never spends minutes
holding the Web interpreter's GIL.  This module defines the small, immutable
receipt that lets both Web services reuse that result after a restart without
weakening source, implementation, file-content, or LIVE_DISABLED boundaries.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from functools import lru_cache
import hashlib
import json
import os
from pathlib import Path
import re

from chanlun.decision_support.fingerprints import sha256_json


LIVE_REVIEW_MATERIALIZATION_RECEIPT_SCHEMA = (
    "chanlun-live-human-review-materialization-receipt"
)
LIVE_REVIEW_WEB_BUNDLE_RECEIPT_SCHEMA = (
    "chanlun-live-human-review-web-bundle-receipt"
)
LIVE_REVIEW_WEB_INDEX_SCHEMA = "chanlun-live-human-review-web-index"
LIVE_REVIEW_CANDIDATE_DETAIL_SCHEMA = (
    "chanlun-live-human-review-candidate-detail"
)
_SHA256 = re.compile(r"^sha256:[0-9a-f]{64}$")
_STABLE_FIELDS = frozenset(
    {
        "schema",
        "source_path",
        "source_size",
        "source_mtime_ns",
        "source_file_sha256",
        "source_snapshot_content_sha256",
        "report_path",
        "report_size",
        "report_mtime_ns",
        "report_file_sha256",
        "report_content_sha256",
        "decision_source_snapshot_id",
        "highest_status",
        "automated_order_authorized",
        "real_account_accessed",
        "real_order_transport_enabled",
        "live_status",
    }
)

_WEB_BUNDLE_STABLE_FIELDS = frozenset(
    {
        "schema",
        "source_path",
        "source_size",
        "source_mtime_ns",
        "source_file_sha256",
        "source_snapshot_content_sha256",
        "report_path",
        "report_size",
        "report_mtime_ns",
        "report_file_sha256",
        "report_content_sha256",
        "index_path",
        "index_size",
        "index_mtime_ns",
        "index_file_sha256",
        "index_content_sha256",
        "detail_path",
        "detail_size",
        "detail_mtime_ns",
        "detail_file_sha256",
        "decision_source_snapshot_id",
        "highest_status",
        "automated_order_authorized",
        "real_account_accessed",
        "real_order_transport_enabled",
        "live_status",
    }
)


@dataclass(frozen=True, slots=True)
class LiveReviewWebBundle:
    """Exact child-validated files used by the lightweight review page."""

    report_path: Path
    index_path: Path
    detail_path: Path
    source_snapshot_content_sha256: str
    report_content_sha256: str
    index_content_sha256: str
    decision_source_snapshot_id: str
    source_current: bool


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return "sha256:" + digest.hexdigest()


@lru_cache(maxsize=16)
def _cached_sha256_file(
    rendered_path: str,
    size: int,
    mtime_ns: int,
) -> str:
    """Hash unchanged immutable files once per Web process."""

    path = Path(rendered_path)
    stat = path.stat()
    if stat.st_size != size or stat.st_mtime_ns != mtime_ns:
        raise OSError("materialized review file identity changed")
    return _sha256_file(path)


def live_review_materialization_receipt(
    *,
    source_path: Path,
    source_stat: os.stat_result,
    source_snapshot_content_sha256: str,
    report_path: Path,
    report_stat: os.stat_result,
    report_content_sha256: str,
    decision_source_snapshot_id: str,
    archive_root: Path,
) -> dict[str, object]:
    """Build the exact receipt after child-process validation succeeds."""

    for label, value in (
        ("source snapshot", source_snapshot_content_sha256),
        ("report", report_content_sha256),
        ("decision source", decision_source_snapshot_id),
    ):
        if _SHA256.fullmatch(value) is None:
            raise ValueError(f"{label} identity is invalid")
    source = source_path.resolve()
    root = archive_root.resolve()
    report = report_path.resolve()
    try:
        relative_report = report.relative_to(root)
    except ValueError as exc:
        raise ValueError("materialized review report escapes archive root") from exc
    if not relative_report.parts:
        raise ValueError("materialized review report path is empty")
    stable: dict[str, object] = {
        "schema": LIVE_REVIEW_MATERIALIZATION_RECEIPT_SCHEMA,
        "source_path": str(source),
        "source_size": int(source_stat.st_size),
        "source_mtime_ns": int(source_stat.st_mtime_ns),
        "source_file_sha256": _cached_sha256_file(
            str(source),
            int(source_stat.st_size),
            int(source_stat.st_mtime_ns),
        ),
        "source_snapshot_content_sha256": source_snapshot_content_sha256,
        "report_path": relative_report.as_posix(),
        "report_size": int(report_stat.st_size),
        "report_mtime_ns": int(report_stat.st_mtime_ns),
        "report_file_sha256": _cached_sha256_file(
            str(report),
            int(report_stat.st_size),
            int(report_stat.st_mtime_ns),
        ),
        "report_content_sha256": report_content_sha256,
        "decision_source_snapshot_id": decision_source_snapshot_id,
        "highest_status": "REVIEW_REQUIRED",
        "automated_order_authorized": False,
        "real_account_accessed": False,
        "real_order_transport_enabled": False,
        "live_status": "LIVE_DISABLED",
    }
    return {**stable, "content_sha256": sha256_json(stable)}


def resolve_live_review_materialization_receipt(
    *,
    source_path: Path,
    archive_root: Path,
    expected_source_snapshot_content_sha256: str | None = None,
    expected_decision_source_snapshot_id: str | None = None,
) -> Path | None:
    """Return the exact child-validated report or fail closed with ``None``.

    The semantic report was already deeply validated by its producing child.
    The restart path therefore verifies the small receipt plus raw SHA-256 of
    both immutable files.  File hashes are memoized by exact stat identity, so
    repeated health/page reads do not rescan roughly 200 MiB.
    """

    source = source_path.resolve()
    root = archive_root.resolve()
    receipt_path = root / ".current_live_review.json"
    if not receipt_path.is_file():
        return None
    try:
        import json

        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        if not isinstance(receipt, Mapping):
            return None
        if set(receipt) != {*_STABLE_FIELDS, "content_sha256"}:
            return None
        stable = {
            key: value
            for key, value in receipt.items()
            if key != "content_sha256"
        }
        source_semantic = str(
            receipt.get("source_snapshot_content_sha256") or ""
        )
        decision_source = str(receipt.get("decision_source_snapshot_id") or "")
        report_semantic = str(receipt.get("report_content_sha256") or "")
        source_file_hash = str(receipt.get("source_file_sha256") or "")
        report_file_hash = str(receipt.get("report_file_sha256") or "")
        if (
            receipt.get("schema")
            != LIVE_REVIEW_MATERIALIZATION_RECEIPT_SCHEMA
            or receipt.get("content_sha256") != sha256_json(stable)
            or receipt.get("highest_status") != "REVIEW_REQUIRED"
            or receipt.get("automated_order_authorized") is not False
            or receipt.get("real_account_accessed") is not False
            or receipt.get("real_order_transport_enabled") is not False
            or receipt.get("live_status") != "LIVE_DISABLED"
            or receipt.get("source_path") != str(source)
            or any(
                _SHA256.fullmatch(value) is None
                for value in (
                    source_semantic,
                    decision_source,
                    report_semantic,
                    source_file_hash,
                    report_file_hash,
                )
            )
            or (
                expected_source_snapshot_content_sha256 is not None
                and source_semantic
                != expected_source_snapshot_content_sha256
            )
            or (
                expected_decision_source_snapshot_id is not None
                and decision_source != expected_decision_source_snapshot_id
            )
        ):
            return None
        source_stat = source.stat()
        if (
            receipt.get("source_size") != int(source_stat.st_size)
            or receipt.get("source_mtime_ns") != int(source_stat.st_mtime_ns)
        ):
            return None
        relative = Path(str(receipt.get("report_path") or ""))
        if relative.is_absolute() or not relative.parts:
            return None
        report_path = (root / relative).resolve()
        if not report_path.is_relative_to(root):
            return None
        if report_path.name != f"{report_semantic.removeprefix('sha256:')}.json":
            return None
        report_stat = report_path.stat()
        if (
            receipt.get("report_size") != int(report_stat.st_size)
            or receipt.get("report_mtime_ns") != int(report_stat.st_mtime_ns)
        ):
            return None
        if (
            _cached_sha256_file(
                str(source),
                int(source_stat.st_size),
                int(source_stat.st_mtime_ns),
            )
            != source_file_hash
            or _cached_sha256_file(
                str(report_path),
                int(report_stat.st_size),
                int(report_stat.st_mtime_ns),
            )
            != report_file_hash
        ):
            return None
    except (OSError, TypeError, ValueError):
        return None
    return report_path


def live_review_web_bundle_receipt(
    *,
    source_path: Path,
    source_stat: os.stat_result,
    source_snapshot_content_sha256: str,
    report_path: Path,
    report_stat: os.stat_result,
    report_content_sha256: str,
    index_path: Path,
    index_stat: os.stat_result,
    index_content_sha256: str,
    detail_path: Path,
    detail_stat: os.stat_result,
    decision_source_snapshot_id: str,
    archive_root: Path,
) -> dict[str, object]:
    """Bind the compact Web index and random-access detail store.

    The source report remains the canonical audit artifact.  These two files
    are presentation projections produced only after that report has passed
    the common semantic validator; none of their fields can authorize an
    order or alter a candidate identity.
    """

    for label, value in (
        ("source snapshot", source_snapshot_content_sha256),
        ("report", report_content_sha256),
        ("index", index_content_sha256),
        ("decision source", decision_source_snapshot_id),
    ):
        if _SHA256.fullmatch(value) is None:
            raise ValueError(f"{label} identity is invalid")
    root = archive_root.resolve()
    source = source_path.resolve()
    artifacts: dict[str, Path] = {
        "report": report_path.resolve(),
        "index": index_path.resolve(),
        "detail": detail_path.resolve(),
    }
    relatives: dict[str, Path] = {}
    for label, path in artifacts.items():
        try:
            relative = path.relative_to(root)
        except ValueError as exc:
            raise ValueError(f"{label} artifact escapes archive root") from exc
        if not relative.parts:
            raise ValueError(f"{label} artifact path is empty")
        relatives[label] = relative
    stable: dict[str, object] = {
        "schema": LIVE_REVIEW_WEB_BUNDLE_RECEIPT_SCHEMA,
        "source_path": str(source),
        "source_size": int(source_stat.st_size),
        "source_mtime_ns": int(source_stat.st_mtime_ns),
        # The canonical receipt is built immediately before this Web receipt in
        # the same validator process.  Reuse hashes only under the exact path,
        # size and nanosecond-mtime identity so the second receipt does not read
        # the 150 MiB source and 70 MiB report all over again.
        "source_file_sha256": _cached_sha256_file(
            str(source),
            int(source_stat.st_size),
            int(source_stat.st_mtime_ns),
        ),
        "source_snapshot_content_sha256": source_snapshot_content_sha256,
        "report_path": relatives["report"].as_posix(),
        "report_size": int(report_stat.st_size),
        "report_mtime_ns": int(report_stat.st_mtime_ns),
        "report_file_sha256": _cached_sha256_file(
            str(artifacts["report"]),
            int(report_stat.st_size),
            int(report_stat.st_mtime_ns),
        ),
        "report_content_sha256": report_content_sha256,
        "index_path": relatives["index"].as_posix(),
        "index_size": int(index_stat.st_size),
        "index_mtime_ns": int(index_stat.st_mtime_ns),
        "index_file_sha256": _cached_sha256_file(
            str(artifacts["index"]),
            int(index_stat.st_size),
            int(index_stat.st_mtime_ns),
        ),
        "index_content_sha256": index_content_sha256,
        "detail_path": relatives["detail"].as_posix(),
        "detail_size": int(detail_stat.st_size),
        "detail_mtime_ns": int(detail_stat.st_mtime_ns),
        "detail_file_sha256": _cached_sha256_file(
            str(artifacts["detail"]),
            int(detail_stat.st_size),
            int(detail_stat.st_mtime_ns),
        ),
        "decision_source_snapshot_id": decision_source_snapshot_id,
        "highest_status": "REVIEW_REQUIRED",
        "automated_order_authorized": False,
        "real_account_accessed": False,
        "real_order_transport_enabled": False,
        "live_status": "LIVE_DISABLED",
    }
    return {**stable, "content_sha256": sha256_json(stable)}


def resolve_live_review_web_bundle_receipt(
    *,
    source_path: Path,
    archive_root: Path,
    expected_source_snapshot_content_sha256: str | None = None,
    expected_decision_source_snapshot_id: str | None = None,
    require_current_source: bool = True,
) -> LiveReviewWebBundle | None:
    """Resolve an exact lightweight Web bundle or fail closed with ``None``."""

    source = source_path.resolve()
    root = archive_root.resolve()
    receipt_path = root / ".current_live_review_web_bundle.json"
    if not receipt_path.is_file():
        return None
    try:
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        if not isinstance(receipt, Mapping):
            return None
        if set(receipt) != {*_WEB_BUNDLE_STABLE_FIELDS, "content_sha256"}:
            return None
        stable = {
            key: value for key, value in receipt.items() if key != "content_sha256"
        }
        source_semantic = str(
            receipt.get("source_snapshot_content_sha256") or ""
        )
        report_semantic = str(receipt.get("report_content_sha256") or "")
        index_semantic = str(receipt.get("index_content_sha256") or "")
        decision_source = str(receipt.get("decision_source_snapshot_id") or "")
        file_hashes = tuple(
            str(receipt.get(f"{label}_file_sha256") or "")
            for label in ("source", "report", "index", "detail")
        )
        if (
            receipt.get("schema") != LIVE_REVIEW_WEB_BUNDLE_RECEIPT_SCHEMA
            or receipt.get("content_sha256") != sha256_json(stable)
            or receipt.get("highest_status") != "REVIEW_REQUIRED"
            or receipt.get("automated_order_authorized") is not False
            or receipt.get("real_account_accessed") is not False
            or receipt.get("real_order_transport_enabled") is not False
            or receipt.get("live_status") != "LIVE_DISABLED"
            or receipt.get("source_path") != str(source)
            or any(
                _SHA256.fullmatch(value) is None
                for value in (
                    source_semantic,
                    report_semantic,
                    index_semantic,
                    decision_source,
                    *file_hashes,
                )
            )
            or (
                expected_source_snapshot_content_sha256 is not None
                and source_semantic
                != expected_source_snapshot_content_sha256
            )
            or (
                expected_decision_source_snapshot_id is not None
                and decision_source != expected_decision_source_snapshot_id
            )
        ):
            return None
        source_current = False
        try:
            source_stat = source.stat()
            source_current = bool(
                receipt.get("source_size") == int(source_stat.st_size)
                and receipt.get("source_mtime_ns")
                == int(source_stat.st_mtime_ns)
                and _cached_sha256_file(
                    str(source),
                    int(source_stat.st_size),
                    int(source_stat.st_mtime_ns),
                )
                == receipt.get("source_file_sha256")
            )
        except OSError:
            source_current = False
        if require_current_source and not source_current:
            return None
        paths: dict[str, Path] = {}
        for label in ("report", "index", "detail"):
            relative = Path(str(receipt.get(f"{label}_path") or ""))
            if relative.is_absolute() or not relative.parts:
                return None
            path = (root / relative).resolve()
            if not path.is_relative_to(root):
                return None
            stat = path.stat()
            if (
                receipt.get(f"{label}_size") != int(stat.st_size)
                or receipt.get(f"{label}_mtime_ns") != int(stat.st_mtime_ns)
                or _cached_sha256_file(
                    str(path), int(stat.st_size), int(stat.st_mtime_ns)
                )
                != receipt.get(f"{label}_file_sha256")
            ):
                return None
            paths[label] = path
        if (
            paths["report"].name
            != f"{report_semantic.removeprefix('sha256:')}.json"
            or paths["index"].name
            != f"{report_semantic.removeprefix('sha256:')}.index.json"
            or paths["detail"].name
            != f"{report_semantic.removeprefix('sha256:')}.details.jsonl"
        ):
            return None
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        return None
    return LiveReviewWebBundle(
        report_path=paths["report"],
        index_path=paths["index"],
        detail_path=paths["detail"],
        source_snapshot_content_sha256=source_semantic,
        report_content_sha256=report_semantic,
        index_content_sha256=index_semantic,
        decision_source_snapshot_id=decision_source,
        source_current=source_current,
    )


__all__ = (
    "LIVE_REVIEW_CANDIDATE_DETAIL_SCHEMA",
    "LIVE_REVIEW_MATERIALIZATION_RECEIPT_SCHEMA",
    "LIVE_REVIEW_WEB_BUNDLE_RECEIPT_SCHEMA",
    "LIVE_REVIEW_WEB_INDEX_SCHEMA",
    "LiveReviewWebBundle",
    "live_review_materialization_receipt",
    "live_review_web_bundle_receipt",
    "resolve_live_review_materialization_receipt",
    "resolve_live_review_web_bundle_receipt",
)
