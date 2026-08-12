#!/usr/bin/env python3
"""Run the frozen strict strategy human-review forward control plane without broker access.

``capture`` records the current QMT GICS3 catalog. ``evaluate`` checks
same-session sector evidence and
completed 1m/5m market data, then hash-archives the page-independent staged
screening snapshot and settles explicitly human-confirmed isolated virtual
intents from later completed 1m bars.  Point-in-time metadata gaps stay explicit warnings in
this current-QMT/manual-confirmation flow. A read-only virtual cash/fee book may be reconstructed, but
without immutable daily marks it cannot report portfolio performance.  No path
builds real orders or broker fills, and this command never imports an account
or order API.
"""

from __future__ import annotations

import argparse
from collections.abc import Callable
from dataclasses import asdict, is_dataclass
from datetime import date, datetime, time, timedelta
from decimal import Decimal, ROUND_HALF_UP
from functools import lru_cache
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
from typing import Mapping, Sequence
from zoneinfo import ZoneInfo

import pandas as pd


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SOURCE_ROOT = PROJECT_ROOT / "src"
for value in (PROJECT_ROOT, SOURCE_ROOT):
    if str(value) not in sys.path:
        sys.path.insert(0, str(value))

from chanlun.decision_support.trading_system.backtest.fixed_year import (  # noqa: E402
    load_qmt_frame,
)
from chanlun.decision_support.trading_system.backtest.pit_metadata import (  # noqa: E402
    load_snapshot,
    qmt_factors_from_rows,
    qmt_native_code,
)
from chanlun.decision_support.trading_system.backtest.qmt_local_cache import (  # noqa: E402
    QMT_LOCAL_DATA_ENV,
)
from chanlun.decision_support.trading_system.qmt_causal_factor_adjustment import (  # noqa: E402
    QMT_CAUSAL_FACTOR_ADJUSTMENT_CONTRACT_ID,
    QmtCausalFactorEvent,
    apply_qmt_causal_factor_adjustment,
    qmt_causal_factor_events_from_frame,
    qmt_causal_factor_revision,
)
from chanlun.decision_support.fingerprints import (  # noqa: E402
    normalize_datetime,
    sha256_json,
)
from chanlun.decision_support.trading_system.decision_source_provenance import (  # noqa: E402
    FORWARD_PIPELINE_TOOL_PATHS as _FORWARD_PIPELINE_TOOL_PATHS,
    current_forward_implementation_provenance,
    current_decision_source_snapshot,
)
from chanlun.decision_support.trading_system.candidate_warmup_diagnostics import (  # noqa: E402
    DEFAULT_CANDIDATE_LIMIT,
    candidate_warmup_diagnostic_path,
    candidate_warmup_parameter_set_id,
    validate_candidate_warmup_diagnostic_document,
)
from chanlun.decision_support.trading_system.forward_paper import (  # noqa: E402
    append_forward_paper_event,
    audit_forward_implementation_continuity,
    audit_forward_paper_session_delivery,
    load_forward_paper_ledger,
    load_forward_contract,
    sha256_file,
)
from chanlun.decision_support.trading_system.trading_session import (  # noqa: E402
    DEFAULT_OFFICIAL_TRADING_CALENDAR_PATH,
    authoritative_trading_session_evidence,
    resolve_trading_session_requirement,
)
from chanlun.exchange.qmt_screening_sector_source import (  # noqa: E402
    qmt_trading_session_evidence,
)
from chanlun.decision_support.trading_system.a_share_minute_grid import (  # noqa: E402
    validate_a_share_completed_one_minute_interval,
)
from chanlun.decision_support.trading_system.bar_execution import (  # noqa: E402
    STRICT_BAR_CROSS_RULE,
    STRICT_BAR_EXECUTION_TIMESTAMP_RULE,
    STRICT_BAR_PRICE_RULE,
    STRICT_BAR_VOLUME_PARTICIPATION,
)
from chanlun.decision_support.trading_system.human_paper_ledger import (  # noqa: E402
    HumanPaperMinuteBar,
    HumanPaperOperationsCancellation,
    audit_human_paper_portfolio_rejection_evidence,
    audit_human_paper_entry_boundary_attestations,
    audit_human_paper_entry_selection_attestations,
    audit_human_paper_entry_selection_source_bindings,
    audit_human_paper_execution_evidence,
    audit_human_paper_execution_rejection_evidence,
    audit_human_paper_operations_cancellation_evidence,
    audit_human_paper_pending_continuity,
    human_paper_oldest_open_lot_sessions,
    human_paper_portfolio_rejected_intent_ids,
    human_paper_position_quantities,
    human_paper_terminal_intent_ids,
    latest_human_paper_pending_continuity,
    load_human_paper_ledger,
    settle_human_paper_intents_with_portfolio_controls,
)
from chanlun.decision_support.trading_system.human_paper_accounting import (  # noqa: E402
    audit_human_paper_portfolio_decisions,
    audit_human_paper_portfolio_fill_decisions,
    load_human_paper_accounting_parameters,
    rebuild_human_paper_accounting,
)
from chanlun.decision_support.trading_system.human_paper_valuation import (  # noqa: E402
    audit_human_paper_valuation_evidence,
    build_human_paper_valuation_document,
    validate_human_paper_valuation_sources,
)
from chanlun.decision_support.market_rules import is_st_name  # noqa: E402
from chanlun.decision_support.trading_system.file_lock import (  # noqa: E402
    interprocess_file_lock,
)
from chanlun.decision_support.trading_system.human_assisted_decision import (  # noqa: E402
    validate_human_assisted_contract_document,
)
from chanlun.decision_support.trading_system.human_review_screening import (  # noqa: E402
    HumanReviewAlert,
    ReviewPriceBar,
    load_human_review_feedback_ledger,
    validate_human_review_screen_document,
)
from chanlun.decision_support.trading_system.forward_review_markout import (  # noqa: E402
    FORWARD_REVIEW_SESSION_QUALIFICATION_SCHEMA,
    FORWARD_REVIEW_SOURCE_AUDIT_CONTRACT_ID,
    ForwardReviewSample,
    build_forward_review_markout,
    qualified_forward_review_session_dates,
    review_price_bars_revision,
    select_first_strategic_buy_samples,
)
from chanlun.decision_support.trading_system.forward_warmup_structure_lineage import (  # noqa: E402
    ForwardWarmupLineageSessionSnapshot,
    build_forward_warmup_structure_lineage_rollup,
)
from chanlun.decision_support.trading_system.qmt_same_base_stream import (  # noqa: E402
    QMT_COMPLETED_ONE_MINUTE_GRID_REVISION,
    build_qmt_same_base_stream_frames,
    normalize_qmt_opening_events_for_completed_minutes,
)
from chanlun.decision_support.trading_system.qmt_instrument_status_snapshot import (  # noqa: E402
    capture_qmt_instrument_status_snapshot,
)
from chanlun.decision_support.trading_system.live_human_review import (  # noqa: E402
    live_human_review_document,
    live_screening_snapshot_content_sha256,
    validate_live_review_snapshot,
)
from chanlun.decision_support.trading_system.qmt_sector_ledger import (  # noqa: E402
    audit_forward_sector_capture_readiness,
    audit_sector_capture_receipts,
    load_sector_ledger,
)
from tools.snapshot_qmt_gics3_sector_ledger import (  # noqa: E402
    capture_daily,
    parser as capture_parser,
)


CN = ZoneInfo("Asia/Shanghai")
DAILY_SECTOR_CAPTURE_DUE = time(9, 10)
DEFAULT_ROOT = Path(".cache/chanlun_human_review_forward")
DEFAULT_PARAMETERS = Path(
    "config/decision_support/human_review_parameters.json"
)
DEFAULT_SECTOR_LEDGER = Path(
    ".cache/chanlun_qmt_sector_ledger/qmt_gics3_catalog_ledger.json"
)
DEFAULT_PIT = Path(
    "audit/chanlun_trading_system_backtest/fixed_year_2025_2026/pit_metadata.json"
)
DEFAULT_HUMAN_PAPER_LEDGER = Path(
    ".cache/chanlun_human_review/paper_ledger.json"
)
DEFAULT_HUMAN_FEEDBACK_LEDGER = Path(
    ".cache/chanlun_human_review/feedback_ledger.json"
)
FORWARD_SESSION_MANIFEST_SCHEMA = "chanlun-forward-session-manifest"
FORWARD_ATTEMPT_RECEIPT_SCHEMA = "chanlun-forward-evaluation-attempt"
FORWARD_PIPELINE_TOOL_PATHS = _FORWARD_PIPELINE_TOOL_PATHS
def _is_sha256_identity(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 71
        and value.startswith("sha256:")
        and all(character in "0123456789abcdef" for character in value[7:])
    )


def _is_screening_policy_identity(value: object) -> bool:
    return _is_sha256_identity(value)


def _is_decision_core_identity(value: object) -> bool:
    return _is_sha256_identity(value)


@lru_cache(maxsize=1)
def _implementation_provenance() -> dict[str, object]:
    return current_forward_implementation_provenance(PROJECT_ROOT)


def _current_implementation_provenance() -> dict[str, object]:
    """Re-read executable sources after a long command, bypassing start cache."""

    return current_forward_implementation_provenance(PROJECT_ROOT)


def _parse_date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("date must use YYYY-MM-DD") from exc


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser(description=__doc__)
    value.add_argument("--root", type=Path, default=DEFAULT_ROOT)
    value.add_argument("--parameter-snapshot", type=Path, default=DEFAULT_PARAMETERS)
    value.add_argument("--sector-ledger", type=Path, default=DEFAULT_SECTOR_LEDGER)
    value.add_argument("--pit-snapshot", type=Path, default=DEFAULT_PIT)
    value.add_argument("--qmt-local-data-dir", type=Path)
    value.add_argument(
        "--trading-calendar",
        type=Path,
        default=DEFAULT_OFFICIAL_TRADING_CALENDAR_PATH,
    )
    value.add_argument("--live-screening-snapshot", type=Path)
    value.add_argument(
        "--human-paper-ledger",
        type=Path,
        default=DEFAULT_HUMAN_PAPER_LEDGER,
    )
    value.add_argument(
        "--human-feedback-ledger",
        type=Path,
        default=DEFAULT_HUMAN_FEEDBACK_LEDGER,
    )
    value.add_argument("--session", type=_parse_date)
    commands = value.add_subparsers(dest="command", required=True)
    commands.add_parser("start")
    capture = commands.add_parser("capture")
    capture.add_argument("--source", choices=("auto", "native", "local"), default="auto")
    commands.add_parser("evaluate")
    commands.add_parser("status")
    return value


def _now() -> datetime:
    return datetime.now(CN)


def _session(args: argparse.Namespace) -> date:
    return args.session or _now().date()


def _required_sector_capture_session(
    args: argparse.Namespace,
    *,
    observed_at: datetime,
    trading_session_evidence: Mapping[str, object],
) -> date | None:
    """Return a due session only after QMT calendar evidence resolves it."""

    observed_at = normalize_datetime(observed_at, "observed_at").astimezone(CN)
    session = _session(args)
    requirement = resolve_trading_session_requirement(
        trading_session_evidence,
        session=session,
        observed_at=observed_at,
    )
    if (
        session != observed_at.date()
        or observed_at.time() < DAILY_SECTOR_CAPTURE_DUE
        or requirement["required"] is not True
    ):
        return None
    return session


def _paths(args: argparse.Namespace) -> tuple[Path, Path]:
    root = args.root.resolve()
    return root, root / "forward_paper_ledger.json"


def _jsonable(value: object) -> object:
    if is_dataclass(value) and not isinstance(value, type):
        return _jsonable(asdict(value))
    if isinstance(value, Mapping):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_jsonable(item) for item in value]
    if isinstance(value, (date, datetime, Path, pd.Timestamp)):
        return str(value)
    if isinstance(value, Decimal):
        return format(value, "f")
    return value


def _print(value: Mapping[str, object]) -> None:
    print(json.dumps(_jsonable(value), ensure_ascii=False, indent=2), flush=True)


def _json_bytes(payload: Mapping[str, object]) -> bytes:
    return (
        json.dumps(
            _jsonable(payload),
            ensure_ascii=False,
            sort_keys=True,
            indent=2,
        )
        + "\n"
    ).encode("utf-8")


def _atomic_json(path: Path, payload: Mapping[str, object]) -> None:
    _atomic_bytes(path, _json_bytes(payload))


def _atomic_bytes(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("wb") as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def _immutable_json_object(
    session_root: Path,
    *,
    kind: str,
    payload: Mapping[str, object],
) -> tuple[Path, str]:
    """Write a content-addressed JSON object without ever replacing it."""

    raw = _json_bytes(payload)
    file_sha256 = "sha256:" + hashlib.sha256(raw).hexdigest()
    path = session_root / "objects" / kind / f"{file_sha256[7:]}.json"
    if path.is_file():
        if path.read_bytes() != raw:
            raise RuntimeError("content-addressed forward object hash collision")
    else:
        _atomic_bytes(path, raw)
    return path, file_sha256


def _immutable_semantic_json_object(
    session_root: Path,
    *,
    kind: str,
    payload: Mapping[str, object],
) -> Path:
    """Persist a JSON document under its independently verified content identity.

    Execution fills retain the document's semantic ``content_sha256`` rather
    than its pretty-printed file hash.  Naming the immutable object by that
    identity makes every fill directly resolvable even after a same-session
    retry updates the convenience ``latest`` file.
    """

    content_sha256 = payload.get("content_sha256")
    if not _is_sha256_identity(content_sha256):
        raise ValueError("semantic JSON object content identity is invalid")
    stable = dict(payload)
    stable.pop("content_sha256", None)
    if content_sha256 != sha256_json(stable):
        raise ValueError("semantic JSON object content hash mismatch")
    raw = _json_bytes(payload)
    path = session_root / "objects" / kind / f"{str(content_sha256)[7:]}.json"
    if path.is_file():
        if path.read_bytes() != raw:
            raise RuntimeError("semantic content-addressed object hash collision")
    else:
        _atomic_bytes(path, raw)
    return path


def _forward_attempt_identity_document(
    attempt: Mapping[str, object],
) -> dict[str, object]:
    """Return the exact current attempt identity fields."""

    identity_fields = {
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
    }
    if set(attempt) != identity_fields | {"attempt_id"}:
        raise RuntimeError("forward attempt identity fields changed")
    return {key: attempt[key] for key in identity_fields}


def _load_session_manifest(
    path: Path,
    *,
    session: date,
    contract_id: str,
    strategy_parameter_set_id: str,
) -> dict[str, object] | None:
    if not path.is_file():
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise RuntimeError("forward session manifest cannot be read") from exc
    if not isinstance(value, Mapping):
        raise RuntimeError("forward session manifest is invalid")
    stable = {key: value[key] for key in value if key != "content_sha256"}
    if (
        value.get("schema") != FORWARD_SESSION_MANIFEST_SCHEMA
        or value.get("session") != session.isoformat()
        or value.get("contract_id") != contract_id
        or value.get("strategy_parameter_set_id") != strategy_parameter_set_id
        or value.get("live_status") != "LIVE_DISABLED"
        or value.get("content_sha256") != sha256_json(stable)
        or not isinstance(value.get("attempts"), list)
    ):
        raise RuntimeError("forward session manifest contract or hash changed")
    attempts = value["attempts"]
    ids = tuple(
        str(item.get("attempt_id"))
        for item in attempts
        if isinstance(item, Mapping)
    )
    if len(ids) != len(attempts) or len(ids) != len(set(ids)):
        raise RuntimeError("forward session manifest attempt identities are invalid")
    for item in attempts:
        if not isinstance(item, Mapping):
            raise RuntimeError("forward session manifest attempt is invalid")
        identity = _forward_attempt_identity_document(item)
        if (
            item.get("attempt_id") != sha256_json(identity)
            or identity.get("schema") != FORWARD_ATTEMPT_RECEIPT_SCHEMA
            or identity.get("session") != session.isoformat()
            or identity.get("contract_id") != contract_id
            or identity.get("strategy_parameter_set_id")
            != strategy_parameter_set_id
            or identity.get("highest_status") != "REVIEW_REQUIRED"
            or identity.get("live_status") != "LIVE_DISABLED"
        ):
            raise RuntimeError("forward session attempt identity changed")
    if any(
        not _is_screening_policy_identity(item.get("screening_policy_id"))
        for item in attempts
        if isinstance(item, Mapping)
    ):
        raise RuntimeError("forward session screening policy identity is invalid")
    if any(
        not _is_decision_core_identity(item.get("decision_core_id"))
        for item in attempts
        if isinstance(item, Mapping)
    ):
        raise RuntimeError("forward session decision core identity is invalid")
    for item in attempts:
        _attempt_screening_policy_id(path.parent, item)
        _attempt_decision_core_id(path.parent, item)
        _report, alerts = _attempt_human_review_report(path.parent, item)
        if len(alerts) != item.get("candidate_count"):
            raise RuntimeError("forward attempt candidate count changed")
    promoted = value.get("promoted_attempt_id")
    if (not ids and promoted is not None) or (ids and promoted not in ids):
        raise RuntimeError("forward session promoted attempt is invalid")
    if value.get("attempt_count") != len(ids):
        raise RuntimeError("forward session manifest attempt count changed")
    if value.get("promoted_sample_count") != (1 if ids else 0):
        raise RuntimeError("forward session promoted sample count changed")
    promoted_policy = value.get("promoted_screening_policy_id")
    if ids and not _is_screening_policy_identity(promoted_policy):
        raise RuntimeError("forward session promoted policy identity is invalid")
    if ids:
        promoted_attempt = next(
            item
            for item in attempts
            if isinstance(item, Mapping) and item.get("attempt_id") == promoted
        )
        if (
            _attempt_screening_policy_id(path.parent, promoted_attempt)
            != promoted_policy
        ):
            raise RuntimeError("forward session promoted policy provenance changed")
    promoted_core = value.get("promoted_decision_core_id")
    if ids and not _is_decision_core_identity(promoted_core):
        raise RuntimeError("forward session promoted decision core identity is invalid")
    if ids:
        promoted_attempt = next(
            item
            for item in attempts
            if isinstance(item, Mapping) and item.get("attempt_id") == promoted
        )
        if _attempt_decision_core_id(path.parent, promoted_attempt) != promoted_core:
            raise RuntimeError(
                "forward session promoted decision core provenance changed"
            )
    return dict(value)


def _attempt_live_snapshot(
    session_root: Path,
    attempt: Mapping[str, object],
) -> tuple[Mapping[str, object], Mapping[str, object]]:
    """Load and fully verify an attempt's immutable live-screen object."""

    identity = attempt.get("live_object")
    if not isinstance(identity, Mapping):
        raise RuntimeError("forward attempt live object identity is unavailable")
    candidate = (session_root / str(identity.get("path"))).resolve()
    try:
        candidate.relative_to(session_root.resolve())
    except ValueError as exc:
        raise RuntimeError("forward attempt live object escaped its session") from exc
    if (
        not candidate.is_file()
        or sha256_file(candidate) != identity.get("file_sha256")
    ):
        raise RuntimeError("forward attempt live object file identity changed")
    try:
        document = json.loads(candidate.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise RuntimeError("forward attempt live object cannot be read") from exc
    if not isinstance(document, Mapping):
        raise RuntimeError("forward attempt live object is invalid")
    stable = {key: document[key] for key in document if key != "content_sha256"}
    if (
        document.get("content_sha256") != identity.get("content_sha256")
        or document.get("content_sha256") != sha256_json(stable)
    ):
        raise RuntimeError("forward attempt live object content identity changed")
    snapshot = document.get("snapshot")
    if not isinstance(snapshot, Mapping):
        raise RuntimeError("forward attempt screening snapshot is unavailable")
    source_identity = document.get("source_content_sha256")
    payload_identity = document.get("source_payload_sha256")
    try:
        exact_payload_identity = sha256_json(snapshot)
        semantic_identity = live_screening_snapshot_content_sha256(snapshot)
    except (TypeError, ValueError) as exc:
        raise RuntimeError(
            "forward attempt screening source identity is invalid"
        ) from exc
    source_valid = (
        payload_identity == exact_payload_identity
        and source_identity == semantic_identity
    )
    if (
        not source_valid
        or attempt.get("source_content_sha256") != source_identity
    ):
        raise RuntimeError("forward attempt screening source identity changed")
    return document, snapshot


def _attempt_screening_policy_id(
    session_root: Path,
    attempt: Mapping[str, object],
) -> str:
    declared = attempt.get("screening_policy_id")
    if not _is_screening_policy_identity(declared):
        raise RuntimeError("forward attempt screening policy identity is invalid")
    document, snapshot = _attempt_live_snapshot(session_root, attempt)
    policy = snapshot.get("screening_policy")
    snapshot_policy_id = snapshot.get("screening_policy_id")
    top_policy_id = document.get("screening_policy_id")
    if (
        isinstance(policy, Mapping)
        and _is_sha256_identity(snapshot_policy_id)
        and snapshot_policy_id == sha256_json(policy)
        and top_policy_id == snapshot_policy_id
    ):
        derived = str(snapshot_policy_id)
    else:
        raise RuntimeError("forward attempt screening policy provenance changed")
    if declared != derived:
        raise RuntimeError("forward attempt screening policy provenance changed")
    return derived


def _attempt_decision_core_id(
    session_root: Path,
    attempt: Mapping[str, object],
) -> str:
    declared = attempt.get("decision_core_id")
    if not _is_decision_core_identity(declared):
        raise RuntimeError("forward attempt decision core identity is invalid")
    document, snapshot = _attempt_live_snapshot(session_root, attempt)
    core_document = snapshot.get("decision_core")
    snapshot_core_id = snapshot.get("decision_core_id")
    top_core_id = document.get("decision_core_id")
    if isinstance(core_document, Mapping):
        try:
            derived = validate_human_assisted_contract_document(core_document)
        except (TypeError, ValueError) as exc:
            raise RuntimeError(
                "forward attempt decision core provenance changed"
            ) from exc
        if snapshot_core_id != derived or top_core_id != derived:
            raise RuntimeError("forward attempt decision core provenance changed")
    else:
        raise RuntimeError("forward attempt decision core is unavailable")
    if declared != derived:
        raise RuntimeError("forward attempt decision core provenance changed")
    return derived


def _attempt_human_review_report(
    session_root: Path,
    attempt: Mapping[str, object],
) -> tuple[Mapping[str, object], tuple[HumanReviewAlert, ...]]:
    """Load and fully verify the review object paired with one attempt."""

    identity = attempt.get("human_review_object")
    if not isinstance(identity, Mapping):
        raise RuntimeError("forward attempt review object identity is unavailable")
    candidate = (session_root / str(identity.get("path"))).resolve()
    try:
        candidate.relative_to(session_root.resolve())
    except ValueError as exc:
        raise RuntimeError("forward review object escaped its session") from exc
    if (
        not candidate.is_file()
        or sha256_file(candidate) != identity.get("file_sha256")
    ):
        raise RuntimeError("forward review object file identity changed")
    try:
        report = json.loads(candidate.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise RuntimeError("forward review object cannot be read") from exc
    if not isinstance(report, Mapping):
        raise RuntimeError("forward review object is invalid")
    stable = {key: report[key] for key in report if key != "content_sha256"}
    if (
        report.get("content_sha256") != identity.get("content_sha256")
        or report.get("content_sha256") != sha256_json(stable)
    ):
        raise RuntimeError("forward review object content identity changed")
    try:
        alerts = validate_human_review_screen_document(report)
    except (TypeError, ValueError) as exc:
        raise RuntimeError("forward review object boundary changed") from exc
    if report.get("forward_paper_session") != attempt.get("session"):
        raise RuntimeError("forward review object session changed")
    input_hashes = report.get("input_hashes")
    if (
        not isinstance(input_hashes, Mapping)
        or input_hashes.get("live_screening_snapshot")
        != attempt.get("source_content_sha256")
    ):
        raise RuntimeError("forward review object source provenance changed")
    return report, alerts


def _record_forward_attempt(
    *,
    session_root: Path,
    session: date,
    contract_id: str,
    strategy_parameter_set_id: str,
    screening_policy_id: str,
    decision_core_id: str,
    source_content_sha256: str,
    live_object: Path,
    live_file_sha256: str,
    live_content_sha256: str,
    human_object: Path,
    human_file_sha256: str,
    human_content_sha256: str,
    candidate_count: int,
    scanner_error_count: int,
) -> tuple[dict[str, object], Path, str, Path, dict[str, object], bool]:
    """Append one immutable attempt and keep the first success as daily sample."""

    if not _is_sha256_identity(screening_policy_id):
        raise ValueError("forward attempt screening policy identity is invalid")
    if not _is_sha256_identity(decision_core_id):
        raise ValueError("forward attempt decision core identity is invalid")
    attempt_identity: dict[str, object] = {
        "schema": FORWARD_ATTEMPT_RECEIPT_SCHEMA,
        "session": session.isoformat(),
        "contract_id": contract_id,
        "strategy_parameter_set_id": strategy_parameter_set_id,
        "screening_policy_id": screening_policy_id,
        "decision_core_id": decision_core_id,
        "source_content_sha256": source_content_sha256,
        "live_object": {
            "path": str(live_object.relative_to(session_root)),
            "file_sha256": live_file_sha256,
            "content_sha256": live_content_sha256,
        },
        "human_review_object": {
            "path": str(human_object.relative_to(session_root)),
            "file_sha256": human_file_sha256,
            "content_sha256": human_content_sha256,
        },
        "candidate_count": candidate_count,
        "scanner_error_count": scanner_error_count,
        "highest_status": "REVIEW_REQUIRED",
        "live_status": "LIVE_DISABLED",
    }
    attempt_id = sha256_json(attempt_identity)
    attempt = {
        **attempt_identity,
        "attempt_id": attempt_id,
    }
    manifest_path = session_root / "forward_session_manifest.json"
    lock_path = session_root / ".forward_session_manifest.lock"
    with interprocess_file_lock(lock_path):
        existing = _load_session_manifest(
            manifest_path,
            session=session,
            contract_id=contract_id,
            strategy_parameter_set_id=strategy_parameter_set_id,
        )
        attempts = [] if existing is None else list(existing["attempts"])
        matching_index = next(
            (
                index
                for index, value in enumerate(attempts)
                if isinstance(value, Mapping)
                and value.get("attempt_id") == attempt_id
            ),
            None,
        )
        if matching_index is None:
            attempts.append(attempt)
        else:
            existing_attempt = attempts[matching_index]
            existing_policy = _attempt_screening_policy_id(
                session_root,
                existing_attempt,
            )
            if existing_policy != screening_policy_id:
                raise RuntimeError("forward attempt screening policy changed")
            existing_core = _attempt_decision_core_id(
                session_root,
                existing_attempt,
            )
            if existing_core != decision_core_id:
                raise RuntimeError("forward attempt decision core changed")
            attempts[matching_index] = {
                **dict(existing_attempt),
                "screening_policy_id": existing_policy,
                "decision_core_id": existing_core,
            }
        attempts = [
            {
                **dict(value),
                "screening_policy_id": _attempt_screening_policy_id(
                    session_root,
                    value,
                ),
                "decision_core_id": _attempt_decision_core_id(
                    session_root,
                    value,
                ),
            }
            for value in attempts
            if isinstance(value, Mapping)
        ]
        promoted_attempt_id = (
            attempt_id if existing is None else str(existing["promoted_attempt_id"])
        )
        promoted_attempt = next(
            value
            for value in attempts
            if isinstance(value, Mapping)
            and value.get("attempt_id") == promoted_attempt_id
        )
        promoted_screening_policy_id = _attempt_screening_policy_id(
            session_root,
            promoted_attempt,
        )
        promoted_decision_core_id = _attempt_decision_core_id(
            session_root,
            promoted_attempt,
        )
        manifest_stable: dict[str, object] = {
            "schema": FORWARD_SESSION_MANIFEST_SCHEMA,
            "session": session.isoformat(),
            "contract_id": contract_id,
            "strategy_parameter_set_id": strategy_parameter_set_id,
            "attempts": attempts,
            "attempt_count": len(attempts),
            "promoted_attempt_id": promoted_attempt_id,
            "promoted_screening_policy_id": promoted_screening_policy_id,
            "promoted_decision_core_id": promoted_decision_core_id,
            "promoted_sample_count": 1,
            "promotion_policy": "FIRST_VALID_EVALUATION_ONLY",
            "highest_status": "REVIEW_REQUIRED",
            "live_status": "LIVE_DISABLED",
        }
        manifest = {
            **manifest_stable,
            "content_sha256": sha256_json(manifest_stable),
        }
        _atomic_json(manifest_path, manifest)
    promoted = promoted_attempt_id == attempt_id
    recorded_attempt = next(
        value
        for value in attempts
        if isinstance(value, Mapping) and value.get("attempt_id") == attempt_id
    )
    receipt_stable = {**dict(recorded_attempt), "promoted_sample": promoted}
    receipt = {
        **receipt_stable,
        "content_sha256": sha256_json(receipt_stable),
    }
    receipt_path, receipt_sha256 = _immutable_json_object(
        session_root,
        kind="forward_evaluation_attempt",
        payload=receipt,
    )
    return (
        attempt,
        receipt_path,
        receipt_sha256,
        manifest_path,
        manifest,
        promoted,
    )


def _default_live_screening_snapshot() -> Path:
    from chanlun import config as chanlun_config

    return (
        Path(chanlun_config.get_data_path())
        / "decision_support"
        / "trading_screening_snapshot.json"
    )


def _archive_live_screening_snapshot(
    *,
    args: argparse.Namespace,
    session: date,
    expected_sector_catalog: Mapping[str, object],
) -> tuple[dict[str, object], dict[str, object]]:
    """Freeze the page-independent staged scanner as the daily forward screen."""

    source = (
        args.live_screening_snapshot.resolve()
        if args.live_screening_snapshot is not None
        else _default_live_screening_snapshot().resolve()
    )
    try:
        payload = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise RuntimeError("live screening snapshot is unreadable") from exc
    if not isinstance(payload, Mapping):
        raise RuntimeError("live screening snapshot is invalid")
    try:
        as_of = datetime.fromisoformat(str(payload["as_of"]))
        market_data_as_of = datetime.fromisoformat(
            str(payload["market_data_as_of"])
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise RuntimeError(
            "live screening snapshot market cutoff is invalid"
        ) from exc
    expected_market_close = datetime.combine(session, time(15), tzinfo=CN)
    audit = payload.get("scan_audit")
    signals = payload.get("signals")
    errors = payload.get("errors")
    screening_policy = payload.get("screening_policy")
    screening_policy_id = payload.get("screening_policy_id")
    decision_core_id = payload.get("decision_core_id")
    if not isinstance(audit, Mapping) or not isinstance(signals, list):
        raise RuntimeError("live screening snapshot coverage is unavailable")
    if not isinstance(errors, list):
        raise RuntimeError("live screening snapshot errors are unavailable")
    try:
        validate_live_review_snapshot(payload, session=session)
    except ValueError as exc:
        raise RuntimeError(
            "live screening snapshot review boundary is incomplete"
        ) from exc
    coverage_manifest = payload.get("coverage_manifest")
    expected_sector_catalog_revision = expected_sector_catalog.get(
        "catalog_revision"
    )
    captured_sector_rows = expected_sector_catalog.get("sectors")
    if (
        not isinstance(expected_sector_catalog_revision, str)
        or not expected_sector_catalog_revision.startswith("sha256:")
        or not isinstance(captured_sector_rows, (list, tuple))
    ):
        raise RuntimeError("same-session QMT sector catalog is malformed")
    captured_members: dict[str, set[str]] = {}
    for row in captured_sector_rows:
        if not isinstance(row, Mapping):
            raise RuntimeError("same-session QMT sector catalog is malformed")
        sector_id = row.get("sector_id")
        member_codes = row.get("member_codes")
        if (
            not isinstance(sector_id, str)
            or sector_id in captured_members
            or not isinstance(member_codes, (list, tuple))
            or any(not isinstance(value, str) for value in member_codes)
        ):
            raise RuntimeError("same-session QMT sector catalog is malformed")
        captured_members[sector_id] = set(member_codes)
    observed_sector_catalog_revision = (
        coverage_manifest.get("sector_catalog_revision")
        if isinstance(coverage_manifest, Mapping)
        else None
    )
    if observed_sector_catalog_revision != expected_sector_catalog_revision:
        raise RuntimeError(
            "live screening sector catalog revision does not match "
            "same-session QMT capture"
        )
    published_sectors = payload.get("sectors")
    if not isinstance(published_sectors, list) or any(
        not isinstance(row, Mapping)
        or row.get("sector_id") not in captured_members
        for row in published_sectors
    ):
        raise RuntimeError(
            "live screening sectors do not belong to same-session QMT capture"
        )
    strength_evidence = payload.get("sector_strength_evidence")
    strength_evidence_revision = payload.get(
        "sector_strength_evidence_revision"
    )
    strength_rows = (
        strength_evidence.get("sectors")
        if isinstance(strength_evidence, Mapping)
        else None
    )
    if not isinstance(strength_rows, list):
        raise RuntimeError("live screening sector strength evidence is unavailable")
    strength_members: dict[str, set[str]] = {}
    for row in strength_rows:
        if not isinstance(row, Mapping):
            raise RuntimeError("live screening sector strength evidence is malformed")
        sector_id = row.get("sector_id")
        member_symbols = row.get("member_symbols")
        if (
            not isinstance(sector_id, str)
            or sector_id in strength_members
            or not isinstance(member_symbols, list)
            or any(not isinstance(code, str) for code in member_symbols)
        ):
            raise RuntimeError("live screening sector strength evidence is malformed")
        strength_members[sector_id] = set(member_symbols)
    if any(
        strength_members.get(str(row.get("sector_id")))
        != captured_members[str(row.get("sector_id"))]
        for row in published_sectors
    ):
        raise RuntimeError(
            "live screening sector strength members do not match same-session "
            "QMT capture"
        )
    boundaries_valid = (
        payload.get("schema") == "chanlun-trading-screening"
        and payload.get("available") is True
        and payload.get("scan_state") == "complete"
        and payload.get("sector_first") is True
        and payload.get("read_only") is True
        and payload.get("research_only") is True
        and payload.get("no_order_execution") is True
        and as_of.tzinfo is not None
        and market_data_as_of.tzinfo is not None
        and as_of.astimezone(CN).date() == session
        and market_data_as_of.astimezone(CN).date() == session
        # 15:20 日级评估是收盘样本，仅日期相同并不足够：14:35 开始的长覆盖周期即使晚间
        # 才完成也不能晋级。复核截止点及其底层行情截止点都必须包含 15:00 收盘。
        and as_of.astimezone(CN) >= expected_market_close
        and market_data_as_of.astimezone(CN) >= expected_market_close
        and market_data_as_of <= as_of
        and audit.get("coverage_cycle_complete") is True
        and Decimal(str(audit.get("sector_completion_ratio"))) >= Decimal("0.80")
        and int(audit.get("pending_symbol_count") or 0) == 0
        and isinstance(payload.get("coverage_manifest"), Mapping)
        and payload["coverage_manifest"].get("complete") is True
        and isinstance(payload.get("snapshot_content_sha256"), str)
        and payload.get("snapshot_content_sha256")
        == live_screening_snapshot_content_sha256(payload)
        and isinstance(screening_policy, Mapping)
        and isinstance(screening_policy_id, str)
        and screening_policy_id.startswith("sha256:")
        and screening_policy_id == sha256_json(screening_policy)
        and audit.get("full_market_history_scan") is False
    )
    if not boundaries_valid:
        raise RuntimeError("live screening snapshot review boundary is incomplete")
    for signal in signals:
        if not isinstance(signal, Mapping):
            raise RuntimeError("live screening signal is invalid")
        try:
            observed_at = datetime.fromisoformat(str(signal["observed_at"]))
        except (KeyError, TypeError, ValueError) as exc:
            raise RuntimeError("live screening signal time is invalid") from exc
        if observed_at.tzinfo is None or observed_at > as_of:
            raise RuntimeError("live screening signal has future or naive time")
        if signal.get("signal_id") is None or signal.get("code") is None:
            raise RuntimeError("live screening signal provenance is incomplete")
        selection_sources = signal.get("selection_sources")
        sector = signal.get("sector")
        if (
            isinstance(selection_sources, list)
            and "QMT_SECTOR_TRIGGER" in selection_sources
            and (
                not isinstance(sector, Mapping)
                or signal.get("code")
                not in captured_members.get(str(sector.get("sector_id")), set())
            )
        ):
            raise RuntimeError(
                "live screening sector-triggered signal is not a member of "
                "the same-session QMT sector"
            )
    contract = load_forward_contract(args.parameter_snapshot.resolve())
    source_payload_sha256 = sha256_json(payload)
    # 完全相同的决策事实必须与页面候选身份一致。生成时间、扫描节奏等运行字段仍由
    # ``source_payload_sha256`` 和不可变包装对象绑定，但不得创建第二个人工复核候选。
    source_sha256 = str(payload["snapshot_content_sha256"])
    stable: dict[str, object] = {
        "schema": "chanlun-forward-live-screening-snapshot",
        "session": session.isoformat(),
        # 归档身份时间使用不可变行情截止点；若使用墙钟写入时间，相同日级评估会产生
        # 新文件哈希并追加重复事件。
        "captured_at": as_of.isoformat(),
        "market_data_as_of": market_data_as_of.isoformat(),
        "source_path": str(source),
        "source_content_sha256": source_sha256,
        "source_payload_sha256": source_payload_sha256,
        "screening_policy_id": screening_policy_id,
        "decision_core_id": decision_core_id,
        "sector_catalog_revision": observed_sector_catalog_revision,
        "sector_strength_evidence_revision": strength_evidence_revision,
        "contract_id": contract.contract_id,
        "strategy_parameter_set_id": contract.strategy_parameter_set_id,
        "data_grade": "HUMAN_REVIEW_SCREENING",
        "highest_status": "REVIEW_REQUIRED",
        "live_status": "LIVE_DISABLED",
        "human_confirmation_required": True,
        "automated_order_authorized": False,
        "portfolio_backtest_performed": False,
        "orders_created": 0,
        "fills_created": 0,
        "positions_created": 0,
        "candidate_count": len(signals),
        "scanner_error_count": len(errors),
        "snapshot": dict(payload),
    }
    document = {**stable, "content_sha256": sha256_json(stable)}
    root, _ledger = _paths(args)
    session_root = root / "sessions" / session.isoformat()
    latest_output = session_root / "forward_live_screening_snapshot.json"
    latest_human_review_output = session_root / "forward_human_review_screen.json"
    human_review_document = live_human_review_document(
        live_snapshot=payload,
        source_snapshot_sha256=source_sha256,
        session=session,
        result_label="FORWARD_STAGED_LIVE_HUMAN_REVIEW_QUEUE",
        decision_source_snapshot=current_decision_source_snapshot(PROJECT_ROOT),
    )
    # 事件证据只指向内容寻址对象；具名文件只是页面和公开接口的当前视图别名。
    output, output_sha256 = _immutable_json_object(
        session_root,
        kind="forward_live_screening_snapshot",
        payload=document,
    )
    human_review_output, human_review_output_sha256 = _immutable_json_object(
        session_root,
        kind="forward_human_review_screen",
        payload=human_review_document,
    )
    _atomic_json(latest_output, document)
    _atomic_json(latest_human_review_output, human_review_document)
    (
        attempt,
        attempt_receipt,
        attempt_receipt_sha256,
        session_manifest,
        session_manifest_document,
        promoted_sample,
    ) = _record_forward_attempt(
        session_root=session_root,
        session=session,
        contract_id=contract.contract_id,
        strategy_parameter_set_id=contract.strategy_parameter_set_id,
        screening_policy_id=screening_policy_id,
        decision_core_id=str(decision_core_id),
        source_content_sha256=source_sha256,
        live_object=output,
        live_file_sha256=output_sha256,
        live_content_sha256=str(document["content_sha256"]),
        human_object=human_review_output,
        human_file_sha256=human_review_output_sha256,
        human_content_sha256=str(human_review_document["content_sha256"]),
        candidate_count=len(signals),
        scanner_error_count=len(errors),
    )
    evidence = {
        "result": str(output),
        "result_sha256": output_sha256,
        "latest_result": str(latest_output),
        "content_sha256": document["content_sha256"],
        "human_review_result": str(human_review_output),
        "human_review_result_sha256": human_review_output_sha256,
        "latest_human_review_result": str(latest_human_review_output),
        "human_review_content_sha256": human_review_document[
            "content_sha256"
        ],
        "attempt_id": attempt["attempt_id"],
        "attempt_receipt": str(attempt_receipt),
        "attempt_receipt_sha256": attempt_receipt_sha256,
        "session_manifest": str(session_manifest),
        "session_manifest_revision": session_manifest_document[
            "content_sha256"
        ],
        "session_attempt_count": session_manifest_document["attempt_count"],
        "promoted_sample": promoted_sample,
        "promoted_sample_count": session_manifest_document[
            "promoted_sample_count"
        ],
        "promoted_screening_policy_id": session_manifest_document[
            "promoted_screening_policy_id"
        ],
        "promoted_decision_core_id": session_manifest_document[
            "promoted_decision_core_id"
        ],
        "human_review_candidate_count": len(
            human_review_document["review_queue"]
        ),
        "source_content_sha256": source_sha256,
        "source_payload_sha256": source_payload_sha256,
        "screening_policy_id": screening_policy_id,
        "decision_core_id": decision_core_id,
        "sector_catalog_revision": observed_sector_catalog_revision,
        "sector_strength_evidence_revision": strength_evidence_revision,
        "candidate_count": len(signals),
        "scanner_error_count": len(errors),
        "coverage_cycle_completed_symbol_count": audit.get(
            "coverage_cycle_completed_symbol_count"
        ),
        "coverage_cycle_failed_symbol_count": audit.get(
            "coverage_cycle_failed_symbol_count"
        ),
        "sector_completed_count": audit.get("sector_completed_count"),
        "human_confirmation_required": True,
        "orders_created": 0,
        "fills_created": 0,
        "live_status": "LIVE_DISABLED",
    }
    return document, evidence


def _capture_forward_screening_instrument_status(
    *,
    args: argparse.Namespace,
    session: date,
    archived_screen: Mapping[str, object],
    sector_catalog: Mapping[str, object],
) -> dict[str, object]:
    """Persist after-close QMT status for today's screened candidates.

    The result is diagnostic input for *later* sessions.  It never changes the
    screen that produced ``archived_screen`` and is never allowed to relabel a
    same-session or historical missing bar after the fact.
    """

    snapshot = archived_screen.get("snapshot")
    signals = snapshot.get("signals") if isinstance(snapshot, Mapping) else None
    if not isinstance(signals, list):
        raise ValueError("archived screen has no candidate signal list")
    symbols: list[str] = []
    for row in signals:
        if not isinstance(row, Mapping) or not isinstance(row.get("code"), str):
            raise ValueError("archived screen candidate identity is invalid")
        symbols.append(str(row["code"]))
    catalog_entry_sha256 = sector_catalog.get("ledger_entry_sha256")
    screen_content_sha256 = archived_screen.get("source_content_sha256")
    if not _is_sha256_identity(catalog_entry_sha256) or not _is_sha256_identity(
        screen_content_sha256
    ):
        raise ValueError("forward status snapshot source identity is invalid")
    root, _ledger = _paths(args)
    result = capture_qmt_instrument_status_snapshot(
        output=(
            root
            / "sessions"
            / session.isoformat()
            / "qmt_instrument_status_snapshot.json"
        ),
        session=session,
        sector_catalog_entry_sha256=str(catalog_entry_sha256),
        source_screen_content_sha256=str(screen_content_sha256),
        symbols=tuple(symbols),
    )
    return {
        **result.evidence(),
        "coverage_scope": "SCREENING_SIGNAL_SYMBOLS_ONLY",
        "can_explain_same_session_decision": False,
        "can_explain_prior_historical_session": False,
        "future_consumer_connected": False,
        "disclosure": (
            "captured for later-session causal adjudication only; current "
            "native-daily gaps remain fail-closed until a separately audited "
            "consumer is connected"
        ),
    }


def _qmt_human_paper_execution_fact(
    *,
    symbol: str,
    session: date,
    factor_start: date,
) -> dict[str, object]:
    """Read one symbol's same-session execution facts without tick/account APIs."""

    from xtquant import xtdata

    native = qmt_native_code(symbol)
    detail = xtdata.get_instrument_detail(native, iscomplete=False)
    if not isinstance(detail, Mapping):
        raise RuntimeError("QMT instrument detail is unavailable")
    raw_trading_day = str(detail.get("TradingDay") or "").strip()
    try:
        trading_day = datetime.strptime(raw_trading_day, "%Y%m%d").date()
    except ValueError as exc:
        raise RuntimeError("QMT instrument trading day is invalid") from exc
    if trading_day != session:
        raise RuntimeError("QMT instrument detail is not from the requested session")
    name = str(detail.get("InstrumentName") or "").strip()
    if not name:
        raise RuntimeError("QMT instrument name is unavailable")
    if detail.get("IsTrading") not in {True, False, 0, 1}:
        raise RuntimeError("QMT IsTrading fact is unavailable")
    try:
        status = int(detail["InstrumentStatus"])
        pre_close = Decimal(str(detail["PreClose"]))
        limit_up = Decimal(str(detail["UpStopPrice"]))
        limit_down = Decimal(str(detail["DownStopPrice"]))
        price_tick = Decimal(str(detail["PriceTick"]))
    except (KeyError, TypeError, ValueError) as exc:
        raise RuntimeError("QMT execution price/status fact is unavailable") from exc
    if (
        status < 0
        or not all(
            value.is_finite()
            for value in (pre_close, limit_up, limit_down, price_tick)
        )
        or pre_close <= 0
        or limit_down <= 0
        or limit_up <= limit_down
        or price_tick <= 0
    ):
        raise RuntimeError("QMT execution price/status fact is invalid")

    frame = xtdata.get_divid_factors(
        native,
        factor_start.strftime("%Y%m%d"),
        session.strftime("%Y%m%d"),
    )
    if not hasattr(frame, "iterrows"):
        raise RuntimeError("QMT corporate-action response is unavailable")
    raw_factors: list[dict[str, object]] = []
    for index, row in frame.iterrows():
        raw_factors.append(
            {
                "effective_on": str(index),
                **{
                    field: row.get(field, 0)
                    for field in (
                        "interest",
                        "stockBonus",
                        "stockGift",
                        "allotNum",
                        "allotPrice",
                        "gugai",
                        "dr",
                    )
                },
            }
        )
    factors = qmt_factors_from_rows(
        code=symbol,
        rows=raw_factors,
        not_before=factor_start,
        not_after=session,
    )
    actions = [
        {
            "effective_on": value.effective_on.isoformat(),
            "interest": format(value.interest, "f"),
            "stock_bonus": format(value.stock_bonus, "f"),
            "stock_gift": format(value.stock_gift, "f"),
            "allot_num": format(value.allot_num, "f"),
            "allot_price": format(value.allot_price, "f"),
            "gugai": format(value.gugai, "f"),
            "raw_price_divisor": format(value.raw_price_divisor, "f"),
        }
        for value in factors
    ]
    expiry_text = str(detail.get("ExpireDate") or "").strip()
    expired = False
    expiry_date: date | None = None
    if expiry_text not in {"", "0", "99999999"}:
        try:
            expiry_date = datetime.strptime(expiry_text, "%Y%m%d").date()
            expired = expiry_date < session
        except ValueError as exc:
            raise RuntimeError("QMT instrument expiry date is invalid") from exc
    # ``IsTrading`` 是墙钟标志，15:00 收盘后通常为假，恰好也是日级评估器运行时段，
    # 因此不能表示“本交易日停牌”。QMT 用 ``InstrumentStatus`` 表示停牌状态；同交易日
    # 1m K 线缺失仍是结算循环中第二项独立关闭失败检查。
    suspended = status >= 1
    return {
        "symbol": symbol,
        "native_code": native,
        "session": session.isoformat(),
        "trading_day": trading_day.isoformat(),
        "instrument_name": name,
        "instrument_status": status,
        "is_trading": bool(detail["IsTrading"]),
        "suspended": suspended,
        "expired": expired,
        "expiry_date": (
            None if expiry_date is None else expiry_date.isoformat()
        ),
        "is_st": is_st_name(name),
        "pre_close": format(pre_close, "f"),
        "limit_up": format(limit_up, "f"),
        "limit_down": format(limit_down, "f"),
        "price_tick": format(price_tick, "f"),
        "corporate_actions": actions,
        "source_methods": (
            "QMT_GET_INSTRUMENT_DETAIL",
            "QMT_GET_DIVID_FACTORS",
        ),
        "tick_data_used": False,
        "account_api_used": False,
    }


def _qmt_human_paper_trading_sessions(
    *,
    start: date,
    end: date,
) -> tuple[date, ...]:
    """Read the historical SH calendar without tick/account/order APIs."""

    if end < start:
        return ()
    from xtquant import xtdata

    response = xtdata.get_trading_dates(
        "SH",
        start.strftime("%Y%m%d"),
        end.strftime("%Y%m%d"),
        -1,
    )
    if type(response) is not list:
        raise RuntimeError("QMT pending-intent trading calendar is unavailable")
    try:
        sessions = tuple(
            sorted(
                {
                    pd.to_datetime(value, unit="ms", utc=True)
                    .tz_convert("Asia/Shanghai")
                    .date()
                    for value in response
                    if not isinstance(value, bool)
                }
            )
        )
    except Exception as exc:
        raise RuntimeError("QMT pending-intent trading calendar is invalid") from exc
    if any(value < start or value > end for value in sessions):
        raise RuntimeError("QMT pending-intent trading calendar escaped its interval")
    return sessions


def _human_paper_execution_snapshot(
    *,
    args: argparse.Namespace,
    session: date,
    pending: Sequence[Mapping[str, object]],
    ledger_events: Sequence[Mapping[str, object]],
) -> tuple[dict[str, object], dict[str, Mapping[str, object]]]:
    """Capture immutable same-session QMT facts for the paper-exposed symbols."""

    root, _ledger = _paths(args)
    path = root / "sessions" / session.isoformat() / "paper_execution_facts.json"
    positions = human_paper_position_quantities(ledger_events)
    symbols = tuple(
        sorted(
            {str(value["symbol"]) for value in pending}
            | set(positions)
        )
    )
    oldest_open_lot = human_paper_oldest_open_lot_sessions(ledger_events)
    if set(positions) != set(oldest_open_lot):
        raise RuntimeError("virtual position lot provenance is inconsistent")

    facts: list[dict[str, object]] = []
    errors: list[dict[str, str]] = []
    captured_at = _now()
    if captured_at.date() != session:
        errors.extend(
            {
                "symbol": symbol,
                "reason": "SAME_SESSION_QMT_EXECUTION_FACT_CAPTURE_UNAVAILABLE",
            }
            for symbol in symbols
        )
    else:
        for symbol in symbols:
            relevant = tuple(value for value in pending if value["symbol"] == symbol)
            factor_start = min(
                (
                    oldest_open_lot.get(symbol, session),
                    *(
                        datetime.fromisoformat(str(value["created_at"])).date()
                        for value in relevant
                    ),
                )
            )
            try:
                fact = _qmt_human_paper_execution_fact(
                    symbol=symbol,
                    session=session,
                    factor_start=factor_start,
                )
            except Exception as exc:
                errors.append(
                    {
                        "symbol": symbol,
                        "reason": "QMT_EXECUTION_FACT_CAPTURE_FAILED",
                        "detail": f"{type(exc).__name__}: {str(exc)[:240]}",
                    }
                )
                continue
            actions = tuple(fact["corporate_actions"])
            position_action_conflict = bool(positions.get(symbol, 0)) and any(
                oldest_open_lot.get(symbol, session)
                <= date.fromisoformat(str(action["effective_on"]))
                <= session
                for action in actions
            )
            if position_action_conflict:
                errors.append(
                    {
                        "symbol": symbol,
                        "reason": "VIRTUAL_POSITION_CORPORATE_ACTION_RECONCILIATION_REQUIRED",
                    }
                )
            facts.append(
                {
                    **fact,
                    "factor_start": factor_start.isoformat(),
                    "virtual_position_quantity": positions.get(symbol, 0),
                    "oldest_virtual_acquired_session": (
                        None
                        if symbol not in oldest_open_lot
                        else oldest_open_lot[symbol].isoformat()
                    ),
                    "position_corporate_action_conflict": position_action_conflict,
                    "security_status_complete": True,
                    "corporate_action_state_complete": not position_action_conflict,
                    "buy_eligible": (
                        not bool(fact["suspended"])
                        and not bool(fact["expired"])
                        and not bool(fact["is_st"])
                    ),
                    "sell_eligible": (
                        not bool(fact["suspended"])
                        and not bool(fact["expired"])
                    ),
                }
            )
    stable: dict[str, object] = {
        "schema": "chanlun-human-paper-execution-facts",
        "session": session.isoformat(),
        "captured_at": captured_at.isoformat(),
        "symbols": facts,
        "errors": errors,
        "requested_symbol_count": len(symbols),
        "complete_symbol_count": len(facts),
        "all_complete": (
            len(facts) == len(symbols)
            and not errors
            and all(
                bool(value["security_status_complete"])
                and bool(value["corporate_action_state_complete"])
                for value in facts
            )
        ),
        "source": "QMT_READ_ONLY_INSTRUMENT_DETAIL_AND_DIVID_FACTORS",
        "minimum_market_data_frequency": "1m",
        "tick_data_used": False,
        "account_api_used": False,
        "broker_transport_available": False,
        "live_status": "LIVE_DISABLED",
    }
    stable = dict(_jsonable(stable))
    document = {**stable, "content_sha256": sha256_json(stable)}
    _immutable_semantic_json_object(
        root / "sessions" / session.isoformat(),
        kind="paper_execution_facts",
        payload=document,
    )
    _atomic_json(path, document)
    return document, {str(value["symbol"]): value for value in facts}


def _human_paper_entry_selection_source_alerts(
    *,
    args: argparse.Namespace,
    events: tuple[Mapping[str, object], ...],
) -> tuple[
    dict[str, tuple[HumanReviewAlert, ...]],
    dict[str, dict[str, object]],
]:
    """Resolve immutable review reports without importing the Web service."""

    wanted = {
        str(event["payload"].get("source_screen_content_sha256") or "")
        for event in events
        if event.get("kind") == "INTENT"
        and isinstance(event.get("payload"), Mapping)
        and event["payload"].get("side") == "BUY"
    }
    if not wanted:
        return {}, {}
    candidates: set[Path] = set()
    live_snapshot = getattr(args, "live_screening_snapshot", None)
    if live_snapshot is not None:
        candidates.add(Path(live_snapshot).resolve())
    candidates.add(
        args.parameter_snapshot.resolve().parent / "human_review_screen.json"
    )
    live_archive_root = args.human_paper_ledger.resolve().parent / "live_screens"
    if live_archive_root.is_dir():
        candidates.update(live_archive_root.glob("*/*.json"))
    forward_sessions = _paths(args)[0] / "sessions"
    if forward_sessions.is_dir():
        candidates.update(
            forward_sessions.glob("*/forward_human_review_screen.json")
        )
        candidates.update(
            forward_sessions.glob(
                "*/objects/forward_human_review_screen/*.json"
            )
        )

    resolved: dict[str, tuple[HumanReviewAlert, ...]] = {}
    reports: dict[str, dict[str, object]] = {}
    for path in sorted(candidates, key=str):
        if not path.is_file():
            continue
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(payload, Mapping):
                continue
            source_hash = str(payload.get("content_sha256") or "")
            if source_hash not in wanted:
                continue
            alerts = validate_human_review_screen_document(payload)
        except (OSError, UnicodeError, json.JSONDecodeError, ValueError):
            continue
        previous = resolved.get(source_hash)
        if previous is not None and previous != alerts:
            raise RuntimeError("duplicate source report identity changed")
        previous_report = reports.get(source_hash)
        if previous_report is not None and previous_report != dict(payload):
            raise RuntimeError("duplicate source report content changed")
        resolved[source_hash] = alerts
        reports[source_hash] = dict(payload)
    return resolved, reports


def _human_paper_entry_selection_settlement_gate(
    *,
    args: argparse.Namespace,
    events: tuple[Mapping[str, object], ...],
    pending: tuple[Mapping[str, object], ...],
    archive_session: date | None = None,
) -> dict[str, object]:
    """Reconstruct every pending BUY's exact QMT sector admission proof.

    This is an execution-time gate, not merely a page audit.  A missing or
    invalid catalog archive blocks only the affected BUY; the intent remains
    pending so a later archive repair can make it eligible without rewriting
    history.  SELL intents are intentionally outside this gate.
    """

    sector_path = args.sector_ledger.resolve()
    catalog_entries: tuple[Mapping[str, object], ...] = ()
    catalog_status = "MISSING"
    catalog_error: str | None = None
    catalog_content_sha256: str | None = None
    try:
        sector_ledger = load_sector_ledger(sector_path)
        catalog_entries = tuple(sector_ledger["entries"])
        catalog_content_sha256 = str(sector_ledger["content_sha256"])
        catalog_status = "VALID"
    except (KeyError, TypeError, ValueError) as exc:
        catalog_status = "INVALID" if sector_path.is_file() else "MISSING"
        catalog_error = f"{type(exc).__name__}: {str(exc)[:240]}"

    attestation_audit = audit_human_paper_entry_selection_attestations(
        events,
        sector_catalog_entries=catalog_entries,
    )
    source_alerts, source_reports = _human_paper_entry_selection_source_alerts(
        args=args,
        events=events,
    )
    source_audit = audit_human_paper_entry_selection_source_bindings(
        events,
        alerts_by_source_content_sha256=source_alerts,
    )
    pending_buy_ids = sorted(
        str(value["intent_id"])
        for value in pending
        if value.get("side") == "BUY"
    )
    verified_ids = set(attestation_audit["verified_buy_intent_ids"]) & set(
        source_audit["verified_required_buy_intent_ids"]
    )
    verified_pending_ids = sorted(set(pending_buy_ids) & verified_ids)
    blocked_ids = sorted(set(pending_buy_ids) - verified_ids)
    verified_pending_id_set = set(verified_pending_ids)
    required_source_hashes = sorted(
        {
            str(value.get("source_screen_content_sha256") or "")
            for value in pending
            if str(value.get("intent_id") or "")
            in verified_pending_id_set
        }
    )
    source_objects: list[dict[str, object]] = []
    if archive_session is not None:
        session_root = _paths(args)[0] / "sessions" / archive_session.isoformat()
        for source_hash in required_source_hashes:
            report = source_reports.get(source_hash)
            alerts = source_alerts.get(source_hash)
            if report is None or alerts is None:
                raise RuntimeError(
                    "verified paper entry source report is unavailable"
                )
            object_path = _immutable_semantic_json_object(
                session_root,
                kind="paper_entry_selection_source_report",
                payload=report,
            )
            source_objects.append(
                {
                    "source_content_sha256": source_hash,
                    "path": str(object_path.relative_to(session_root)),
                    "file_sha256": sha256_file(object_path),
                    "candidate_ids": sorted(
                        value.candidate_id for value in alerts
                    ),
                    "verified_pending_buy_intent_ids": sorted(
                        str(value["intent_id"])
                        for value in pending
                        if str(
                            value.get("source_screen_content_sha256") or ""
                        )
                        == source_hash
                        and str(value.get("intent_id") or "")
                        in verified_pending_id_set
                    ),
                    "live_status": "LIVE_DISABLED",
                }
            )
    archived_source_hashes = {
        str(value["source_content_sha256"]) for value in source_objects
    }
    archive_performed = archive_session is not None
    all_required_sources_archived = (
        archive_performed
        and archived_source_hashes == set(required_source_hashes)
    )
    source_report_archive = {
        "schema": "chanlun-human-paper-entry-source-report-archive",
        "status": (
            "COMPLETE"
            if archive_performed and required_source_hashes
            else "NO_REQUIRED_SOURCE_REPORTS"
            if archive_performed
            else "NOT_REQUESTED_STATUS_VIEW"
        ),
        "archive_performed": archive_performed,
        "required_source_report_count": len(required_source_hashes),
        "required_source_content_sha256s": required_source_hashes,
        "archived_source_report_count": len(source_objects),
        "objects": source_objects,
        "all_required_source_reports_archived": (
            all_required_sources_archived
        ),
        "broker_transport_available": False,
        "live_status": "LIVE_DISABLED",
    }
    status = (
        "BLOCKED"
        if blocked_ids
        else "READY"
        if pending_buy_ids
        else "NO_PENDING_BUYS"
    )
    return {
        "schema": "chanlun-human-paper-entry-selection-settlement-gate",
        "status": status,
        "sector_catalog_ledger": str(sector_path),
        "sector_catalog_ledger_status": catalog_status,
        "sector_catalog_ledger_content_sha256": catalog_content_sha256,
        "sector_catalog_ledger_error": catalog_error,
        "pending_buy_intent_count": len(pending_buy_ids),
        "pending_buy_intent_ids": pending_buy_ids,
        "verified_pending_buy_intent_count": len(verified_pending_ids),
        "verified_pending_buy_intent_ids": verified_pending_ids,
        "blocked_pending_buy_intent_count": len(blocked_ids),
        "blocked_pending_buy_intent_ids": blocked_ids,
        "attestation_audit": attestation_audit,
        "source_binding_audit": source_audit,
        "source_report_archive": source_report_archive,
        # 在任何结果离开函数前，``_settle_human_paper`` 会用精确结算后账本前缀替换该占位；
        # 直接状态视图不修改或伪造账本归档，因此此处保留 ``None``。
        "paper_ledger_prefix_archive": None,
        "immutable_source_ranking_required_before_virtual_buy_fill": True,
        "exact_qmt_sector_admission_required_before_virtual_buy_fill": True,
        "paper_ledger_prefix_required_for_independent_replay": True,
        "blocked_buy_remains_pending": True,
        "persistent_sell_processing_continues": True,
        "tick_data_used": False,
        "broker_transport_available": False,
        "live_status": "LIVE_DISABLED",
    }


def _archive_human_paper_ledger_prefix(
    *,
    args: argparse.Namespace,
    session: date,
    ledger: Mapping[str, object],
) -> dict[str, object]:
    """Freeze the exact ledger prefix consumed by one settlement result."""

    events = ledger.get("events")
    content_sha256 = str(ledger.get("content_sha256") or "")
    if not isinstance(events, list) or not _is_sha256_identity(content_sha256):
        raise RuntimeError("human paper ledger prefix is invalid")
    last_event_id: str | None = None
    if events:
        last = events[-1]
        if not isinstance(last, Mapping) or not _is_sha256_identity(
            last.get("event_id")
        ):
            raise RuntimeError("human paper ledger prefix tail is invalid")
        last_event_id = str(last["event_id"])
    session_root = _paths(args)[0] / "sessions" / session.isoformat()
    object_path = _immutable_semantic_json_object(
        session_root,
        kind="human_paper_ledger_prefix",
        payload=ledger,
    )
    # 发布回执前通过权威账本校验器重新打开，以验证每个事件身份、链路和载荷。
    archived = load_human_paper_ledger(object_path)
    if archived.get("content_sha256") != content_sha256:
        raise RuntimeError("archived human paper ledger prefix changed")
    return {
        "schema": "chanlun-human-paper-ledger-prefix-archive",
        "status": "COMPLETE",
        "archive_performed": True,
        "paper_ledger_content_sha256": content_sha256,
        "path": str(object_path.relative_to(session_root)),
        "file_sha256": sha256_file(object_path),
        "event_count": len(events),
        "last_event_id": last_event_id,
        "broker_transport_available": False,
        "automated_order_authorized": False,
        "live_status": "LIVE_DISABLED",
    }


def _settle_human_paper(
    *,
    args: argparse.Namespace,
    session: date,
) -> dict[str, object]:
    """Settle pending human intents from later completed local 1m bars only."""

    path = args.human_paper_ledger.resolve()
    before = load_human_paper_ledger(path)
    accounting_parameters = load_human_paper_accounting_parameters(
        args.parameter_snapshot.resolve()
    )
    prior_execution_evidence = audit_human_paper_execution_evidence(
        tuple(before["events"]),
        forward_root=_paths(args)[0],
    )
    if (
        any(event.get("kind") == "FILL" for event in before["events"])
        and prior_execution_evidence.get("status") != "COMPLETE"
    ):
        raise RuntimeError(
            "existing virtual fills have incomplete immutable execution evidence: "
            f"{prior_execution_evidence.get('status') or 'INVALID'}"
        )
    prior_execution_rejection_evidence = (
        audit_human_paper_execution_rejection_evidence(
            tuple(before["events"]),
            forward_root=_paths(args)[0],
        )
    )
    if (
        any(
            event.get("kind") == "EXECUTION_REJECT"
            for event in before["events"]
        )
        and prior_execution_rejection_evidence.get("status") != "COMPLETE"
    ):
        raise RuntimeError(
            "existing virtual execution rejections have incomplete immutable "
            "evidence: "
            f"{prior_execution_rejection_evidence.get('status') or 'INVALID'}"
        )
    prior_operations_cancellation_evidence = (
        audit_human_paper_operations_cancellation_evidence(
            tuple(before["events"]),
            forward_root=_paths(args)[0],
        )
    )
    if (
        any(
            event.get("kind") == "OPERATIONS_CANCEL"
            for event in before["events"]
        )
        and prior_operations_cancellation_evidence.get("status")
        != "COMPLETE"
    ):
        raise RuntimeError(
            "existing optional-BUY operations cancellations have incomplete "
            "immutable evidence: "
            f"{prior_operations_cancellation_evidence.get('status') or 'INVALID'}"
        )
    prior_portfolio_rejection_evidence = (
        audit_human_paper_portfolio_rejection_evidence(
            tuple(before["events"]),
            forward_root=_paths(args)[0],
        )
    )
    if (
        any(
            event.get("kind") == "PORTFOLIO_REJECT"
            for event in before["events"]
        )
        and prior_portfolio_rejection_evidence.get("status") != "COMPLETE"
    ):
        raise RuntimeError(
            "existing virtual portfolio rejections have incomplete immutable "
            "execution evidence: "
            f"{prior_portfolio_rejection_evidence.get('status') or 'INVALID'}"
        )
    prior_portfolio_decision_audit = audit_human_paper_portfolio_decisions(
        tuple(before["events"]),
        parameters=accounting_parameters,
    )
    if (
        any(
            event.get("kind") == "PORTFOLIO_REJECT"
            for event in before["events"]
        )
        and prior_portfolio_decision_audit.get("status") != "COMPLETE"
    ):
        raise RuntimeError(
            "existing virtual portfolio rejection decisions cannot be "
            "reconstructed: "
            f"{prior_portfolio_decision_audit.get('status') or 'INVALID'}"
        )
    prior_portfolio_fill_decision_audit = (
        audit_human_paper_portfolio_fill_decisions(
            tuple(before["events"]),
            parameters=accounting_parameters,
        )
    )
    if (
        any(
            event.get("kind") == "FILL"
            and isinstance(event.get("payload"), Mapping)
            and "portfolio_decision_sha256" in event["payload"]
            for event in before["events"]
        )
        and prior_portfolio_fill_decision_audit.get("status") != "COMPLETE"
    ):
        raise RuntimeError(
            "existing virtual portfolio fill approvals cannot be reconstructed: "
            f"{prior_portfolio_fill_decision_audit.get('status') or 'INVALID'}"
        )
    terminal_intent_ids = human_paper_terminal_intent_ids(before["events"])
    pending = tuple(
        event["payload"]
        for event in before["events"]
        if event.get("kind") == "INTENT"
        and isinstance(event.get("payload"), Mapping)
        and event["payload"].get("status") == "PENDING"
        and event["payload"].get("intent_id") not in terminal_intent_ids
    )
    entry_selection_gate = _human_paper_entry_selection_settlement_gate(
        args=args,
        events=tuple(before["events"]),
        pending=pending,
        archive_session=session,
    )
    entry_selection_gate["paper_ledger_prefix_archive"] = (
        _archive_human_paper_ledger_prefix(
            args=args,
            session=session,
            ledger=before,
        )
    )
    if not pending:
        pending_continuity = audit_human_paper_pending_continuity(
            tuple(before["events"]),
            forward_root=_paths(args)[0],
            current_session=session,
            trading_sessions=(),
        )
        return {
            "status": "NO_PENDING_VIRTUAL_INTENTS",
            "paper_ledger": str(path),
            "pending_intent_count": 0,
            "new_virtual_fill_count": 0,
            "pending_continuity": pending_continuity,
            "entry_selection_settlement_gate": entry_selection_gate,
            "entry_selection_blocked_buy_intent_count": 0,
            "exact_qmt_sector_admission_required_before_virtual_buy_fill": True,
            "prior_execution_evidence": prior_execution_evidence,
            "execution_rejection_evidence": (
                prior_execution_rejection_evidence
            ),
            "operations_cancellation_evidence": (
                prior_operations_cancellation_evidence
            ),
            "portfolio_rejection_evidence": prior_portfolio_rejection_evidence,
            "portfolio_decision_audit": prior_portfolio_decision_audit,
            "portfolio_fill_decision_audit": (
                prior_portfolio_fill_decision_audit
            ),
            "content_sha256": before["content_sha256"],
            "cash_and_slot_pretrade_enforced": True,
            "slot_fraction_notional_gate_evaluable": True,
            "account_exposure_notional_gate_evaluable": True,
            "synchronous_open_position_one_minute_marks_required": True,
            "unresolved_position_marks_block_new_buys": True,
            "portfolio_approved_fill_ledger_prefix_recomputed": True,
            "one_security_one_strategic_slot_enforced": True,
            "terminal_signal_lifecycle_one_shot_enforced": True,
            "fixed_one_lot_tactical_review_only": True,
            "strategic_buy_confirmation_bar_price_cap_enforced": True,
            "strategic_buy_entire_bar_strict_cross_enforced": True,
            "strategic_buy_five_percent_bar_volume_cap_enforced": True,
            "persistent_sell_five_percent_bar_volume_cap_enforced": True,
            "adverse_observed_bar_extreme_fill_price_enforced": True,
            "completed_bar_close_fill_timestamp_enforced": True,
            "strategic_buy_one_locator_bar_ttl_enforced": True,
            "strategic_buy_causal_full_1m_window_prechecked": True,
            "full_session_240_bar_grid_required": True,
            "opening_auction_event_merged_into_0931": True,
            "optional_buy_data_fault_cancelled": True,
            "optional_buy_security_gate_cancelled": True,
            "execution_fact_incomplete_optional_buy_cancelled": True,
            "persistent_exit_independent_symbol_continues": True,
            "persistent_exit_security_blocked_remains_pending": True,
            "persistent_exit_fact_incomplete_remains_pending": True,
            "new_operations_cancellation_count": 0,
            "total_operations_cancellation_count": sum(
                event.get("kind") == "OPERATIONS_CANCEL"
                for event in before["events"]
            ),
            "unresolved_persistent_exit_intent_count": 0,
            "structure_anchor_never_used_as_execution_cap": True,
            "strategic_exit_persistent_until_fill": True,
            "broker_transport_available": False,
            "live_status": "LIVE_DISABLED",
        }
    total_pending = pending
    earliest_session = min(
        datetime.fromisoformat(str(value["earliest_fill_at"])).date()
        for value in pending
    )
    prior_sessions = _qmt_human_paper_trading_sessions(
        start=earliest_session,
        end=session - timedelta(days=1),
    )
    pending_continuity = audit_human_paper_pending_continuity(
        tuple(before["events"]),
        forward_root=_paths(args)[0],
        current_session=session,
        trading_sessions=prior_sessions,
    )
    gap_intent_ids = set(pending_continuity["gap_intent_ids"])
    pending = tuple(
        value for value in pending if str(value["intent_id"]) not in gap_intent_ids
    )
    if not pending:
        return {
            "status": "VIRTUAL_SETTLEMENT_BLOCKED_BY_CAUSAL_GAP",
            "paper_ledger": str(path),
            "pending_intent_count": len(total_pending),
            "causally_eligible_intent_count": 0,
            "causal_gap_blocked_intent_count": len(gap_intent_ids),
            "new_virtual_fill_count": 0,
            "content_sha256": before["content_sha256"],
            "pending_continuity": pending_continuity,
            "entry_selection_settlement_gate": entry_selection_gate,
            "entry_selection_blocked_buy_intent_count": len(
                entry_selection_gate["blocked_pending_buy_intent_ids"]
            ),
            "exact_qmt_sector_admission_required_before_virtual_buy_fill": True,
            "prior_execution_evidence": prior_execution_evidence,
            "execution_rejection_evidence": (
                prior_execution_rejection_evidence
            ),
            "operations_cancellation_evidence": (
                prior_operations_cancellation_evidence
            ),
            "portfolio_rejection_evidence": prior_portfolio_rejection_evidence,
            "portfolio_decision_audit": prior_portfolio_decision_audit,
            "portfolio_fill_decision_audit": (
                prior_portfolio_fill_decision_audit
            ),
            "cash_and_slot_pretrade_enforced": True,
            "slot_fraction_notional_gate_evaluable": True,
            "account_exposure_notional_gate_evaluable": True,
            "synchronous_open_position_one_minute_marks_required": True,
            "unresolved_position_marks_block_new_buys": True,
            "portfolio_approved_fill_ledger_prefix_recomputed": True,
            "one_security_one_strategic_slot_enforced": True,
            "terminal_signal_lifecycle_one_shot_enforced": True,
            "fixed_one_lot_tactical_review_only": True,
            "strategic_buy_confirmation_bar_price_cap_enforced": True,
            "strategic_buy_entire_bar_strict_cross_enforced": True,
            "strategic_buy_five_percent_bar_volume_cap_enforced": True,
            "persistent_sell_five_percent_bar_volume_cap_enforced": True,
            "adverse_observed_bar_extreme_fill_price_enforced": True,
            "completed_bar_close_fill_timestamp_enforced": True,
            "strategic_buy_one_locator_bar_ttl_enforced": True,
            "strategic_buy_causal_full_1m_window_prechecked": True,
            "full_session_240_bar_grid_required": True,
            "opening_auction_event_merged_into_0931": True,
            "optional_buy_data_fault_cancelled": True,
            "optional_buy_security_gate_cancelled": True,
            "execution_fact_incomplete_optional_buy_cancelled": True,
            "persistent_exit_independent_symbol_continues": True,
            "persistent_exit_security_blocked_remains_pending": True,
            "persistent_exit_fact_incomplete_remains_pending": True,
            "new_operations_cancellation_count": 0,
            "total_operations_cancellation_count": sum(
                event.get("kind") == "OPERATIONS_CANCEL"
                for event in before["events"]
            ),
            "unresolved_persistent_exit_intent_count": 0,
            "structure_anchor_never_used_as_execution_cap": True,
            "strategic_exit_persistent_until_fill": True,
            "broker_transport_available": False,
            "live_status": "LIVE_DISABLED",
        }
    causally_eligible_pending = pending
    entry_selection_blocked_ids = set(
        entry_selection_gate["blocked_pending_buy_intent_ids"]
    )
    causally_eligible_entry_selection_blocked_ids = sorted(
        str(value["intent_id"])
        for value in causally_eligible_pending
        if str(value["intent_id"]) in entry_selection_blocked_ids
    )
    pending = tuple(
        value
        for value in causally_eligible_pending
        if str(value["intent_id"])
        not in entry_selection_blocked_ids
    )
    if not pending:
        return {
            "status": "VIRTUAL_SETTLEMENT_BLOCKED_BY_ENTRY_SELECTION_EVIDENCE",
            "paper_ledger": str(path),
            "pending_intent_count": len(total_pending),
            "causally_eligible_intent_count": len(causally_eligible_pending),
            "settlement_eligible_intent_count": 0,
            "causal_gap_blocked_intent_count": len(gap_intent_ids),
            "entry_selection_blocked_buy_intent_count": len(
                causally_eligible_entry_selection_blocked_ids
            ),
            "entry_selection_blocked_buy_intent_ids": (
                causally_eligible_entry_selection_blocked_ids
            ),
            "new_virtual_fill_count": 0,
            "content_sha256": before["content_sha256"],
            "pending_continuity": pending_continuity,
            "entry_selection_settlement_gate": entry_selection_gate,
            "prior_execution_evidence": prior_execution_evidence,
            "execution_rejection_evidence": (
                prior_execution_rejection_evidence
            ),
            "operations_cancellation_evidence": (
                prior_operations_cancellation_evidence
            ),
            "portfolio_rejection_evidence": prior_portfolio_rejection_evidence,
            "portfolio_decision_audit": prior_portfolio_decision_audit,
            "portfolio_fill_decision_audit": (
                prior_portfolio_fill_decision_audit
            ),
            "cash_and_slot_pretrade_enforced": True,
            "exact_qmt_sector_admission_required_before_virtual_buy_fill": True,
            "entry_selection_blocked_buy_remains_pending": True,
            "persistent_exit_independent_symbol_continues": True,
            "broker_transport_available": False,
            "live_status": "LIVE_DISABLED",
        }
    execution_snapshot, execution_facts = _human_paper_execution_snapshot(
        args=args,
        session=session,
        pending=pending,
        ledger_events=tuple(before["events"]),
    )
    execution_fact_snapshot_sha256 = str(execution_snapshot["content_sha256"])
    execution_snapshot_object = (
        _paths(args)[0]
        / "sessions"
        / session.isoformat()
        / "objects"
        / "paper_execution_facts"
        / f"{execution_fact_snapshot_sha256[7:]}.json"
    )
    if not execution_snapshot_object.is_file():
        raise RuntimeError("immutable paper execution fact object is unavailable")
    end_at = datetime.combine(session, time(15), tzinfo=CN)
    session_start = datetime.combine(session, time(9, 30), tzinfo=CN)
    bar_payloads_by_symbol: dict[str, tuple[dict[str, object], ...]] = {}
    bar_grid_audits: list[dict[str, object]] = []
    bar_grid_error_count = 0
    unresolved_grid_symbols: set[str] = set()
    position_quantities = human_paper_position_quantities(tuple(before["events"]))
    execution_symbols = (
        {str(value["symbol"]) for value in pending}
        | set(position_quantities)
    )
    for symbol in sorted(execution_symbols):
        fact = execution_facts.get(symbol)
        if fact is None:
            bar_payloads_by_symbol[symbol] = ()
            bar_grid_error_count += 1
            unresolved_grid_symbols.add(symbol)
            bar_grid_audits.append(
                {
                    "symbol": symbol,
                    "status": "EXECUTION_FACT_MISSING_FAIL_CLOSED",
                    "native_row_count": 0,
                    "normalized_row_count": 0,
                    "complete_sessions": [],
                    "session_issues": [
                        {
                            "session": session.isoformat(),
                            "code": "QMT_EXECUTION_FACT_REQUIRED_BEFORE_BAR_GRID",
                            "observed_rows": 0,
                            "detail": "same-session instrument facts are unavailable",
                        }
                    ],
                    "source_base_stream_revision": None,
                }
            )
            continue
        symbol_pending_sides = {
            str(value["side"])
            for value in pending
            if str(value["symbol"]) == symbol
        }
        requires_buy_grid = (
            "BUY" in symbol_pending_sides and fact["buy_eligible"] is True
        )
        requires_sell_or_mark_grid = (
            "SELL" in symbol_pending_sides
            or position_quantities.get(symbol, 0) > 0
        ) and fact["sell_eligible"] is True
        requires_bar_grid = bool(
            requires_buy_grid or requires_sell_or_mark_grid
        )
        if not requires_bar_grid:
            bar_payloads_by_symbol[symbol] = ()
            bar_grid_audits.append(
                {
                    "symbol": symbol,
                    "status": "NOT_REQUIRED_INSTRUMENT_INELIGIBLE",
                    "native_row_count": 0,
                    "normalized_row_count": 0,
                    "complete_sessions": [],
                    "session_issues": [],
                    "source_base_stream_revision": None,
                }
            )
            continue
        frame: pd.DataFrame | None = None
        try:
            frame = load_qmt_frame(
                symbol,
                "1m",
                start_at=session_start,
                end_at=end_at,
            )
            stream = build_qmt_same_base_stream_frames(
                symbol=symbol,
                one_minute_frame=frame,
                decision_time=end_at,
                expected_sessions=(session,),
            )
            grid_complete = (
                stream.complete_sessions == (session,)
                and not stream.session_issues
                and len(stream.one_minute) == 240
            )
            if not grid_complete:
                bar_grid_error_count += 1
                unresolved_grid_symbols.add(symbol)
                session_rows = stream.one_minute.iloc[0:0]
                grid_status = "INCOMPLETE_FAIL_CLOSED"
            else:
                session_rows = stream.one_minute
                grid_status = "COMPLETE"
            bar_grid_audits.append(
                {
                    "symbol": symbol,
                    "status": grid_status,
                    "native_row_count": len(frame),
                    "normalized_row_count": len(session_rows),
                    "complete_sessions": [
                        value.isoformat() for value in stream.complete_sessions
                    ],
                    "session_issues": [
                        {
                            "session": value.session.isoformat(),
                            "code": value.code,
                            "observed_rows": value.observed_rows,
                            "detail": value.detail,
                        }
                        for value in stream.session_issues
                    ],
                    "source_base_stream_revision": (
                        stream.source_base_stream_revision
                    ),
                }
            )
        except Exception as exc:
            bar_grid_error_count += 1
            unresolved_grid_symbols.add(symbol)
            session_rows = pd.DataFrame()
            native_row_count = 0 if frame is None else len(frame)
            bar_grid_audits.append(
                {
                    "symbol": symbol,
                    "status": "INVALID_FAIL_CLOSED",
                    "native_row_count": native_row_count,
                    "normalized_row_count": 0,
                    "complete_sessions": [],
                    "session_issues": [
                        {
                            "session": session.isoformat(),
                            "code": "QMT_EXECUTION_ONE_MINUTE_GRID_INVALID",
                            "observed_rows": native_row_count,
                            "detail": f"{type(exc).__name__}: {str(exc)[:240]}",
                        }
                    ],
                    "source_base_stream_revision": None,
                }
            )
        bars: list[dict[str, object]] = []
        for row in session_rows.itertuples(index=False):
            closed_at = pd.Timestamp(row.date).to_pydatetime()
            opened_at = closed_at - pd.Timedelta(minutes=1).to_pytimedelta()
            validate_a_share_completed_one_minute_interval(opened_at, closed_at)
            open_price = Decimal(str(row.open))
            high = Decimal(str(row.high))
            low = Decimal(str(row.low))
            close = Decimal(str(row.close))
            volume = Decimal(str(row.volume))
            one_price_bar = high == low
            limit_up = Decimal(str(fact["limit_up"]))
            limit_down = Decimal(str(fact["limit_down"]))
            bars.append(
                {
                    "symbol": symbol,
                    "opened_at": opened_at.isoformat(),
                    "closed_at": closed_at.isoformat(),
                    "open": format(open_price, "f"),
                    "high": format(high, "f"),
                    "low": format(low, "f"),
                    "close": format(close, "f"),
                    "volume": format(volume, "f"),
                    "complete": True,
                    "suspended": bool(fact["suspended"]),
                    # 无 tick 时，仅当整根 1m 柱锁在 QMT 当日限价上才判封板。
                    "limit_up_locked": one_price_bar and high == limit_up,
                    "limit_down_locked": one_price_bar and low == limit_down,
                    "buy_eligible": bool(fact["buy_eligible"]),
                    "sell_eligible": bool(fact["sell_eligible"]),
                    "security_status_complete": bool(
                        fact["security_status_complete"]
                    ),
                    "corporate_action_state_complete": bool(
                        fact["corporate_action_state_complete"]
                    ),
                }
            )
        bar_payloads_by_symbol[symbol] = tuple(bars)

    execution_evidence_stable: dict[str, object] = {
        "schema": "chanlun-human-paper-execution-evidence",
        "session": session.isoformat(),
        "captured_at": execution_snapshot["captured_at"],
        "execution_fact_snapshot_sha256": execution_fact_snapshot_sha256,
        "pending_intent_ids": sorted(str(value["intent_id"]) for value in pending),
        "bars_by_symbol": {
            symbol: list(values)
            for symbol, values in sorted(bar_payloads_by_symbol.items())
        },
        "bar_grid_audits": bar_grid_audits,
        "all_required_bar_grids_complete": bar_grid_error_count == 0,
        "fill_model": STRICT_BAR_PRICE_RULE,
        "fill_timestamp_rule": STRICT_BAR_EXECUTION_TIMESTAMP_RULE,
        "buy_strict_cross_rule": STRICT_BAR_CROSS_RULE,
        "buy_max_bar_volume_participation": format(
            STRICT_BAR_VOLUME_PARTICIPATION,
            "f",
        ),
        "minimum_market_data_frequency": "1m",
        "tick_data_used": False,
        "account_api_used": False,
        "broker_transport_available": False,
        "live_status": "LIVE_DISABLED",
    }
    execution_evidence = {
        **execution_evidence_stable,
        "content_sha256": sha256_json(execution_evidence_stable),
    }
    execution_evidence_object = _immutable_semantic_json_object(
        _paths(args)[0] / "sessions" / session.isoformat(),
        kind="paper_execution_evidence",
        payload=execution_evidence,
    )
    _atomic_json(
        _paths(args)[0]
        / "sessions"
        / session.isoformat()
        / "paper_execution_evidence.json",
        execution_evidence,
    )
    execution_evidence_sha256 = str(execution_evidence["content_sha256"])
    bars_by_symbol = {
        symbol: tuple(
            HumanPaperMinuteBar(
                symbol=str(value["symbol"]),
                opened_at=datetime.fromisoformat(str(value["opened_at"])),
                closed_at=datetime.fromisoformat(str(value["closed_at"])),
                open=Decimal(str(value["open"])),
                high=Decimal(str(value["high"])),
                low=Decimal(str(value["low"])),
                close=Decimal(str(value["close"])),
                volume=Decimal(str(value["volume"])),
                complete=bool(value["complete"]),
                suspended=bool(value["suspended"]),
                limit_up_locked=bool(value["limit_up_locked"]),
                limit_down_locked=bool(value["limit_down_locked"]),
                buy_eligible=bool(value["buy_eligible"]),
                sell_eligible=bool(value["sell_eligible"]),
                security_status_complete=bool(value["security_status_complete"]),
                corporate_action_state_complete=bool(
                    value["corporate_action_state_complete"]
                ),
                execution_snapshot_sha256=execution_evidence_sha256,
            )
            for value in values
        )
        for symbol, values in bar_payloads_by_symbol.items()
    }
    grid_status_by_symbol = {
        str(value["symbol"]): str(value["status"])
        for value in bar_grid_audits
    }
    security_gate_cancelled_buy_intents = tuple(
        value
        for value in pending
        if value.get("side") == "BUY"
        if execution_facts.get(str(value["symbol"])) is not None
        and execution_facts[str(value["symbol"])].get(
            "security_status_complete"
        )
        is True
        and execution_facts[str(value["symbol"])].get("buy_eligible") is False
    )
    security_cancelled_intent_ids = {
        str(value["intent_id"])
        for value in security_gate_cancelled_buy_intents
    }
    data_fault_cancelled_buy_intents = tuple(
        value
        for value in pending
        if value.get("side") == "BUY"
        if str(value["intent_id"]) not in security_cancelled_intent_ids
        and (
            str(value["symbol"]) in unresolved_grid_symbols
            or (
                execution_facts.get(str(value["symbol"])) is not None
                and (
                    execution_facts[str(value["symbol"])].get(
                        "security_status_complete"
                    )
                    is not True
                    or execution_facts[str(value["symbol"])].get(
                        "corporate_action_state_complete"
                    )
                    is not True
                )
            )
        )
    )
    unresolved_persistent_exit_intents = tuple(
        value
        for value in pending
        if value.get("side") == "SELL"
        and (
            (
                str(value["symbol"]) in unresolved_grid_symbols
                and (
                    execution_facts.get(str(value["symbol"])) is None
                    or execution_facts[str(value["symbol"])].get("sell_eligible")
                    is True
                )
            )
            or (
                execution_facts.get(str(value["symbol"])) is not None
                and (
                    execution_facts[str(value["symbol"])].get("sell_eligible")
                    is False
                    or execution_facts[str(value["symbol"])].get(
                        "security_status_complete"
                    )
                    is not True
                    or execution_facts[str(value["symbol"])].get(
                        "corporate_action_state_complete"
                    )
                    is not True
                )
            )
        )
    )
    security_blocked_persistent_exit_count = sum(
        execution_facts.get(str(value["symbol"])) is not None
        and execution_facts[str(value["symbol"])].get("sell_eligible") is False
        for value in unresolved_persistent_exit_intents
    )
    fact_incomplete_persistent_exit_count = sum(
        execution_facts.get(str(value["symbol"])) is not None
        and (
            execution_facts[str(value["symbol"])].get(
                "security_status_complete"
            )
            is not True
            or execution_facts[str(value["symbol"])].get(
                "corporate_action_state_complete"
            )
            is not True
        )
        for value in unresolved_persistent_exit_intents
    )
    cancellation_time = normalize_datetime(
        datetime.fromisoformat(str(execution_snapshot["captured_at"])),
        "captured_at",
    )
    operations_cancellations = tuple(
        HumanPaperOperationsCancellation(
            intent_id=str(value["intent_id"]),
            symbol=str(value["symbol"]),
            candidate_id=str(value["candidate_id"]),
            signal_lifecycle_id=(
                None
                if value.get("signal_lifecycle_id") is None
                else str(value["signal_lifecycle_id"])
            ),
            cancelled_at=cancellation_time,
            execution_fact_snapshot_sha256=execution_fact_snapshot_sha256,
            execution_evidence_snapshot_sha256=execution_evidence_sha256,
            grid_status=grid_status_by_symbol[str(value["symbol"])],
            reason_code=(
                "OPTIONAL_BUY_CANCELLED_BY_SECURITY_GATE"
                if value in security_gate_cancelled_buy_intents
                else "OPTIONAL_BUY_CANCELLED_BY_EXECUTION_DATA_HALT"
            ),
            operations_state=(
                "SECURITY_GATE_CLOSED"
                if value in security_gate_cancelled_buy_intents
                else "OPERATIONS_HALT"
            ),
        )
        for value in (
            *data_fault_cancelled_buy_intents,
            *security_gate_cancelled_buy_intents,
        )
    )
    before_fill_count = sum(
        event.get("kind") == "FILL" for event in before["events"]
    )
    before_execution_rejection_count = sum(
        event.get("kind") == "EXECUTION_REJECT" for event in before["events"]
    )
    before_operations_cancellation_count = sum(
        event.get("kind") == "OPERATIONS_CANCEL" for event in before["events"]
    )
    after, capital_evaluations = settle_human_paper_intents_with_portfolio_controls(
        path,
        bars_by_symbol=bars_by_symbol,
        accounting_parameters=accounting_parameters,
        operations_cancellations=operations_cancellations,
        entry_provenance_blocked_intent_ids=(
            causally_eligible_entry_selection_blocked_ids
        ),
        causal_gap_blocked_intent_ids=sorted(gap_intent_ids),
    )
    entry_selection_gate["paper_ledger_prefix_archive"] = (
        _archive_human_paper_ledger_prefix(
            args=args,
            session=session,
            ledger=after,
        )
    )
    after_fill_count = sum(
        event.get("kind") == "FILL" for event in after["events"]
    )
    after_execution_rejection_count = sum(
        event.get("kind") == "EXECUTION_REJECT" for event in after["events"]
    )
    after_operations_cancellation_count = sum(
        event.get("kind") == "OPERATIONS_CANCEL" for event in after["events"]
    )
    execution_rejection_evidence = (
        audit_human_paper_execution_rejection_evidence(
            tuple(after["events"]),
            forward_root=_paths(args)[0],
        )
    )
    if (
        after_execution_rejection_count
        and execution_rejection_evidence.get("status") != "COMPLETE"
    ):
        raise RuntimeError(
            "virtual execution rejection immutable evidence is incomplete: "
            f"{execution_rejection_evidence.get('status') or 'INVALID'}"
        )
    operations_cancellation_evidence = (
        audit_human_paper_operations_cancellation_evidence(
            tuple(after["events"]),
            forward_root=_paths(args)[0],
        )
    )
    if (
        after_operations_cancellation_count
        and operations_cancellation_evidence.get("status") != "COMPLETE"
    ):
        raise RuntimeError(
            "optional-BUY operations cancellation immutable evidence is "
            "incomplete: "
            f"{operations_cancellation_evidence.get('status') or 'INVALID'}"
        )
    portfolio_rejection_evidence = audit_human_paper_portfolio_rejection_evidence(
        tuple(after["events"]),
        forward_root=_paths(args)[0],
    )
    if (
        any(
            event.get("kind") == "PORTFOLIO_REJECT"
            for event in after["events"]
        )
        and portfolio_rejection_evidence.get("status") != "COMPLETE"
    ):
        raise RuntimeError(
            "virtual portfolio rejection immutable execution evidence is incomplete: "
            f"{portfolio_rejection_evidence.get('status') or 'INVALID'}"
        )
    portfolio_decision_audit = audit_human_paper_portfolio_decisions(
        tuple(after["events"]),
        parameters=accounting_parameters,
    )
    if (
        any(
            event.get("kind") == "PORTFOLIO_REJECT"
            for event in after["events"]
        )
        and portfolio_decision_audit.get("status") != "COMPLETE"
    ):
        raise RuntimeError(
            "virtual portfolio rejection decision reconstruction failed: "
            f"{portfolio_decision_audit.get('status') or 'INVALID'}"
        )
    portfolio_fill_decision_audit = (
        audit_human_paper_portfolio_fill_decisions(
            tuple(after["events"]),
            parameters=accounting_parameters,
        )
    )
    if (
        any(
            event.get("kind") == "FILL"
            and isinstance(event.get("payload"), Mapping)
            and "portfolio_decision_sha256" in event["payload"]
            for event in after["events"]
        )
        and portfolio_fill_decision_audit.get("status") != "COMPLETE"
    ):
        raise RuntimeError(
            "virtual portfolio fill approval reconstruction failed: "
            f"{portfolio_fill_decision_audit.get('status') or 'INVALID'}"
        )
    unresolved_mark_count = sum(
        value.get("result") == "PORTFOLIO_MARKS_UNRESOLVED"
        for value in capital_evaluations
    )
    return {
        "status": (
            "VIRTUAL_SETTLEMENT_PARTIALLY_BLOCKED_BY_CAUSAL_GAP"
            if gap_intent_ids
            else "VIRTUAL_SETTLEMENT_PARTIALLY_BLOCKED_BY_ENTRY_SELECTION_EVIDENCE"
            if causally_eligible_entry_selection_blocked_ids
            else "VIRTUAL_SETTLEMENT_PARTIALLY_BLOCKED_BY_POSITION_MARKS"
            if unresolved_mark_count
            else "VIRTUAL_SETTLEMENT_PARTIALLY_BLOCKED_BY_EXECUTION_FACTS"
            if fact_incomplete_persistent_exit_count
            else "VIRTUAL_SETTLEMENT_PARTIALLY_BLOCKED_BY_SECURITY_GATE"
            if security_blocked_persistent_exit_count
            else "VIRTUAL_SETTLEMENT_PARTIALLY_BLOCKED_BY_1M_GRID"
            if bar_grid_error_count and execution_snapshot["all_complete"]
            else "VIRTUAL_SETTLEMENT_READY"
            if execution_snapshot["all_complete"]
            else "VIRTUAL_SETTLEMENT_PARTIALLY_BLOCKED"
        ),
        "paper_ledger": str(path),
        "pending_intent_count": len(total_pending),
        "causally_eligible_intent_count": len(causally_eligible_pending),
        "settlement_eligible_intent_count": len(pending),
        "causal_gap_blocked_intent_count": len(gap_intent_ids),
        "entry_selection_blocked_buy_intent_count": len(
            causally_eligible_entry_selection_blocked_ids
        ),
        "entry_selection_blocked_buy_intent_ids": (
            causally_eligible_entry_selection_blocked_ids
        ),
        "pending_continuity": pending_continuity,
        "entry_selection_settlement_gate": entry_selection_gate,
        "prior_execution_evidence": prior_execution_evidence,
        "execution_rejection_evidence": execution_rejection_evidence,
        "operations_cancellation_evidence": (
            operations_cancellation_evidence
        ),
        "portfolio_rejection_evidence": portfolio_rejection_evidence,
        "portfolio_decision_audit": portfolio_decision_audit,
        "portfolio_fill_decision_audit": portfolio_fill_decision_audit,
        "new_virtual_fill_count": after_fill_count - before_fill_count,
        "total_virtual_fill_count": after_fill_count,
        "new_execution_rejection_count": (
            after_execution_rejection_count
            - before_execution_rejection_count
        ),
        "total_execution_rejection_count": after_execution_rejection_count,
        "new_operations_cancellation_count": (
            after_operations_cancellation_count
            - before_operations_cancellation_count
        ),
        "total_operations_cancellation_count": (
            after_operations_cancellation_count
        ),
        "cash_and_slot_pretrade_enforced": True,
        "capital_evaluation_count": len(capital_evaluations),
        "portfolio_rejection_count": sum(
            value.get("result") == "PORTFOLIO_REJECTED"
            for value in capital_evaluations
        ),
        "portfolio_mark_unresolved_count": unresolved_mark_count,
        "capital_evaluations": list(capital_evaluations),
        "slot_fraction_notional_gate_evaluable": True,
        "account_exposure_notional_gate_evaluable": True,
        "synchronous_open_position_one_minute_marks_required": True,
        "unresolved_position_marks_block_new_buys": True,
        "exact_qmt_sector_admission_required_before_virtual_buy_fill": True,
        "entry_selection_blocked_buy_remains_pending": True,
        "portfolio_approved_fill_ledger_prefix_recomputed": True,
        "one_security_one_strategic_slot_enforced": True,
        "terminal_signal_lifecycle_one_shot_enforced": True,
        "fixed_one_lot_tactical_review_only": True,
        "strategic_buy_confirmation_bar_price_cap_enforced": True,
        "strategic_buy_entire_bar_strict_cross_enforced": True,
        "strategic_buy_five_percent_bar_volume_cap_enforced": True,
        "persistent_sell_five_percent_bar_volume_cap_enforced": True,
        "adverse_observed_bar_extreme_fill_price_enforced": True,
        "completed_bar_close_fill_timestamp_enforced": True,
        "strategic_buy_one_locator_bar_ttl_enforced": True,
        "strategic_buy_causal_full_1m_window_prechecked": True,
        "full_session_240_bar_grid_required": True,
        "opening_auction_event_merged_into_0931": True,
        "optional_buy_data_fault_cancelled": True,
        "optional_buy_security_gate_cancelled": True,
        "execution_fact_incomplete_optional_buy_cancelled": True,
        "persistent_exit_independent_symbol_continues": True,
        "persistent_exit_security_blocked_remains_pending": True,
        "persistent_exit_fact_incomplete_remains_pending": True,
        "unresolved_persistent_exit_intent_count": len(
            unresolved_persistent_exit_intents
        ),
        "security_blocked_persistent_exit_intent_count": (
            security_blocked_persistent_exit_count
        ),
        "fact_incomplete_persistent_exit_intent_count": (
            fact_incomplete_persistent_exit_count
        ),
        "structure_anchor_never_used_as_execution_cap": True,
        "strategic_exit_persistent_until_fill": True,
        "fixed_one_lot_diagnostic": True,
        "content_sha256": after["content_sha256"],
        "execution_fact_snapshot": str(
            _paths(args)[0]
            / "sessions"
            / session.isoformat()
            / "paper_execution_facts.json"
        ),
        "execution_fact_object": str(execution_snapshot_object),
        "execution_fact_snapshot_sha256": execution_fact_snapshot_sha256,
        "execution_evidence_snapshot": str(
            _paths(args)[0]
            / "sessions"
            / session.isoformat()
            / "paper_execution_evidence.json"
        ),
        "execution_evidence_object": str(execution_evidence_object),
        "execution_evidence_snapshot_sha256": execution_evidence_sha256,
        "execution_fact_complete_symbol_count": execution_snapshot[
            "complete_symbol_count"
        ],
        "execution_fact_error_count": len(execution_snapshot["errors"]),
        "execution_fact_errors": execution_snapshot["errors"],
        "execution_bar_grid_error_count": bar_grid_error_count,
        "execution_bar_grid_audits": bar_grid_audits,
        "full_session_one_minute_grid_proven": bar_grid_error_count == 0,
        "fill_model": STRICT_BAR_PRICE_RULE,
        "fill_timestamp_rule": STRICT_BAR_EXECUTION_TIMESTAMP_RULE,
        "buy_strict_cross_rule": STRICT_BAR_CROSS_RULE,
        "buy_max_bar_volume_participation": format(
            STRICT_BAR_VOLUME_PARTICIPATION,
            "f",
        ),
        "tick_data_used": False,
        "broker_transport_available": False,
        "live_status": "LIVE_DISABLED",
    }


def _human_paper_valuation_result(
    *,
    document: Mapping[str, object],
    object_path: Path,
    alias_path: Path,
    reused: bool,
) -> dict[str, object]:
    complete = document.get("all_complete") is True
    return {
        "status": "VALUATION_COMPLETE" if complete else "VALUATION_INCOMPLETE",
        "session": document["session"],
        "valuation_object": str(object_path),
        "valuation_snapshot": str(alias_path) if complete else None,
        "valuation_content_sha256": document["content_sha256"],
        "paper_ledger_content_sha256": document[
            "paper_ledger_content_sha256"
        ],
        "accounting_content_sha256": document["accounting_content_sha256"],
        "position_count": document["position_count"],
        "complete_mark_count": len(document.get("marks") or ()),
        "error_count": len(document.get("errors") or ()),
        "errors": list(document.get("errors") or ()),
        "cash_balance": document["cash_balance"],
        "market_value": document["market_value"],
        "equity": document["equity"],
        "pnl_from_initial_cash": document["pnl_from_initial_cash"],
        "equity_curve_point_available": document[
            "equity_curve_point_available"
        ],
        "performance_evaluable": False,
        "reused": reused,
        "minimum_market_data_frequency": "1m",
        "tick_data_used": False,
        "broker_transport_available": False,
        "live_status": "LIVE_DISABLED",
    }


def _capture_human_paper_valuation(
    *,
    args: argparse.Namespace,
    session: date,
) -> dict[str, object]:
    """Freeze a same-session close mark for the human virtual book.

    This is deliberately separate from order matching.  A position is valued
    only when the exact completed 15:00 one-minute bar and the same-session
    QMT security/corporate-action facts are both available.  Incomplete
    attempts remain immutable diagnostic objects but are never promoted to the
    daily equity alias.
    """

    root, _forward_ledger = _paths(args)
    paper_path = args.human_paper_ledger.resolve()
    paper = load_human_paper_ledger(paper_path)
    paper_events = tuple(paper["events"])
    accounting_parameters = load_human_paper_accounting_parameters(
        args.parameter_snapshot.resolve()
    )
    execution_audit = audit_human_paper_execution_evidence(
        paper_events,
        forward_root=root,
    )
    accounting = rebuild_human_paper_accounting(
        paper_events,
        parameters=accounting_parameters,
        execution_evidence_status=str(execution_audit.get("status") or "INVALID"),
    )
    session_root = root / "sessions" / session.isoformat()
    alias_path = session_root / "paper_valuation.json"

    # 已晋级收盘点对其精确账本/核算身份不可变；同交易日重试逐字节复用。若虚拟账本在
    # 晋级后变化，应关闭失败而不是改写历史。
    if alias_path.is_file():
        try:
            raw = json.loads(alias_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise RuntimeError("promoted paper valuation is unreadable") from exc
        if not isinstance(raw, Mapping):
            raise RuntimeError("promoted paper valuation is malformed")
        try:
            existing = validate_human_paper_valuation_sources(
                raw,
                paper_events=paper_events,
                accounting_parameters=accounting_parameters,
                forward_root=root,
            )
        except (TypeError, ValueError) as exc:
            raise RuntimeError("promoted paper valuation is invalid") from exc
        identity = str(existing["content_sha256"])
        object_path = (
            session_root
            / "objects"
            / "paper_valuation"
            / f"{identity[7:]}.json"
        )
        if not object_path.is_file():
            raise RuntimeError("promoted paper valuation immutable object is missing")
        try:
            immutable = json.loads(object_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise RuntimeError("paper valuation immutable object is unreadable") from exc
        if immutable != existing:
            raise RuntimeError("paper valuation alias and immutable object disagree")
        if (
            existing.get("session") != session.isoformat()
            or existing.get("paper_ledger_content_sha256")
            != paper["content_sha256"]
            or existing.get("accounting_content_sha256")
            != accounting["content_sha256"]
        ):
            raise RuntimeError(
                "virtual book changed after the daily valuation was promoted"
            )
        return _human_paper_valuation_result(
            document=existing,
            object_path=object_path,
            alias_path=alias_path,
            reused=True,
        )

    captured_at = _now()
    marks: list[dict[str, object]] = []
    errors: list[dict[str, object]] = []
    positions = accounting.get("positions")
    if not isinstance(positions, Mapping):
        raise RuntimeError("human paper accounting positions are unavailable")
    session_start = datetime.combine(session, time(9, 30), tzinfo=CN)
    session_close = datetime.combine(session, time(15), tzinfo=CN)
    cent = Decimal("0.01")
    for symbol, position in sorted(positions.items()):
        if not isinstance(position, Mapping):
            errors.append(
                {"symbol": str(symbol), "reason": "ACCOUNTING_POSITION_INVALID"}
            )
            continue
        try:
            quantity = int(position["quantity"])
            acquired_on = date.fromisoformat(
                str(position["oldest_acquired_session"])
            )
        except (KeyError, TypeError, ValueError):
            errors.append(
                {"symbol": str(symbol), "reason": "ACCOUNTING_POSITION_INVALID"}
            )
            continue
        try:
            fact = _qmt_human_paper_execution_fact(
                symbol=str(symbol),
                session=session,
                factor_start=acquired_on,
            )
        except Exception as exc:
            errors.append(
                {
                    "symbol": str(symbol),
                    "reason": "QMT_VALUATION_FACT_CAPTURE_FAILED",
                    "detail": f"{type(exc).__name__}: {str(exc)[:240]}",
                }
            )
            continue
        actions_after_acquisition = [
            dict(value)
            for value in fact.get("corporate_actions") or ()
            if acquired_on
            < date.fromisoformat(str(value["effective_on"]))
            <= session
        ]
        if actions_after_acquisition:
            errors.append(
                {
                    "symbol": str(symbol),
                    "reason": "VIRTUAL_POSITION_CORPORATE_ACTION_RECONCILIATION_REQUIRED",
                    "corporate_actions": actions_after_acquisition,
                }
            )
            continue
        if bool(fact.get("expired")):
            errors.append(
                {"symbol": str(symbol), "reason": "VALUATION_SYMBOL_EXPIRED"}
            )
            continue
        if bool(fact.get("suspended")):
            errors.append(
                {"symbol": str(symbol), "reason": "VALUATION_SYMBOL_SUSPENDED"}
            )
            continue
        try:
            frame = load_qmt_frame(
                str(symbol),
                "1m",
                start_at=session_start,
                end_at=session_close,
            )
            close_rows: list[tuple[object, datetime]] = []
            for row in frame.itertuples(index=False):
                timestamp = pd.Timestamp(row.date)
                if timestamp.tzinfo is None:
                    timestamp = timestamp.tz_localize(CN)
                else:
                    timestamp = timestamp.tz_convert(CN)
                closed_at = timestamp.to_pydatetime()
                if (
                    closed_at.date() == session
                    and closed_at.timetz().replace(tzinfo=None) == time(15)
                ):
                    close_rows.append((row, closed_at))
            if len(close_rows) != 1:
                raise RuntimeError("exact completed 15:00 one-minute bar is unavailable")
            row, closed_at = close_rows[0]
            open_price = Decimal(str(row.open))
            high = Decimal(str(row.high))
            low = Decimal(str(row.low))
            close = Decimal(str(row.close))
            volume = Decimal(str(row.volume))
            if (
                not all(
                    value.is_finite()
                    for value in (open_price, high, low, close, volume)
                )
                or min(open_price, close) < low
                or max(open_price, close) > high
                or low <= 0
                or volume < 0
            ):
                raise RuntimeError("15:00 one-minute valuation bar is invalid")
        except Exception as exc:
            errors.append(
                {
                    "symbol": str(symbol),
                    "reason": "VALUATION_CLOSE_BAR_UNAVAILABLE",
                    "detail": f"{type(exc).__name__}: {str(exc)[:240]}",
                }
            )
            continue
        market_value = (Decimal(quantity) * close).quantize(
            cent, rounding=ROUND_HALF_UP
        )
        marks.append(
            dict(
                _jsonable(
                    {
                        "symbol": str(symbol),
                        "quantity": quantity,
                        "opened_at": (closed_at - timedelta(minutes=1)).isoformat(),
                        "closed_at": closed_at.isoformat(),
                        "open": format(open_price, "f"),
                        "high": format(high, "f"),
                        "low": format(low, "f"),
                        "close": format(close, "f"),
                        "volume": format(volume, "f"),
                        "market_value": format(market_value, "f"),
                        "complete": True,
                        "suspended": False,
                        "security_status_complete": True,
                        "corporate_action_state_complete": True,
                        "oldest_acquired_session": acquired_on.isoformat(),
                        "instrument_fact": fact,
                        "qmt_transport": frame.attrs.get("qmt_transport"),
                        "qmt_local_cache_source_sha256": frame.attrs.get(
                            "qmt_local_cache_source_sha256"
                        ),
                    }
                )
            )
        )

    document = build_human_paper_valuation_document(
        session=session,
        captured_at=captured_at,
        paper_ledger_content_sha256=str(paper["content_sha256"]),
        accounting=accounting,
        marks=marks,
        errors=errors,
    )
    if document["all_complete"] is True:
        validate_human_paper_valuation_sources(
            document,
            paper_events=paper_events,
            accounting_parameters=accounting_parameters,
            forward_root=root,
        )
    object_path = _immutable_semantic_json_object(
        session_root,
        kind="paper_valuation",
        payload=document,
    )
    if document["all_complete"] is True:
        _atomic_json(alias_path, document)
    return _human_paper_valuation_result(
        document=document,
        object_path=object_path,
        alias_path=alias_path,
        reused=False,
    )


def _promoted_forward_review_reports(
    *,
    args: argparse.Namespace,
    through_session: date,
    eligible_sessions: frozenset[date] | None = None,
) -> tuple[tuple[date, str, Mapping[str, object]], ...]:
    """Load the first promoted immutable screen for each eligible session."""

    root, _ledger = _paths(args)
    contract = load_forward_contract(args.parameter_snapshot.resolve())
    output: list[tuple[date, str, Mapping[str, object]]] = []
    sessions_root = root / "sessions"
    if not sessions_root.is_dir():
        return ()
    for manifest_path in sorted(sessions_root.glob("*/forward_session_manifest.json")):
        try:
            source_session = date.fromisoformat(manifest_path.parent.name)
        except ValueError as exc:
            raise RuntimeError("forward session directory date is invalid") from exc
        if source_session > through_session:
            continue
        if eligible_sessions is not None and source_session not in eligible_sessions:
            continue
        manifest = _load_session_manifest(
            manifest_path,
            session=source_session,
            contract_id=contract.contract_id,
            strategy_parameter_set_id=contract.strategy_parameter_set_id,
        )
        if manifest is None:
            continue
        promoted_id = manifest["promoted_attempt_id"]
        promoted = next(
            (
                value
                for value in manifest["attempts"]
                if isinstance(value, Mapping)
                and value.get("attempt_id") == promoted_id
            ),
            None,
        )
        if promoted is None:
            raise RuntimeError("promoted forward review object is unavailable")
        session_root = manifest_path.parent.resolve()
        report, _alerts = _attempt_human_review_report(session_root, promoted)
        identity = promoted["human_review_object"]
        output.append(
            (
                source_session,
                str(identity["content_sha256"]),
                report,
            )
        )
    return tuple(output)


def _promoted_forward_warmup_lineage_sources(
    *,
    args: argparse.Namespace,
    through_session: date,
    eligible_sessions: frozenset[date] | None = None,
) -> tuple[ForwardWarmupLineageSessionSnapshot, ...]:
    """Load the validated live object behind each promoted daily sample."""

    root, _ledger = _paths(args)
    contract = load_forward_contract(args.parameter_snapshot.resolve())
    output: list[ForwardWarmupLineageSessionSnapshot] = []
    sessions_root = root / "sessions"
    if not sessions_root.is_dir():
        return ()
    for manifest_path in sorted(
        sessions_root.glob("*/forward_session_manifest.json")
    ):
        try:
            source_session = date.fromisoformat(manifest_path.parent.name)
        except ValueError as exc:
            raise RuntimeError("forward session directory date is invalid") from exc
        if source_session > through_session:
            continue
        if eligible_sessions is not None and source_session not in eligible_sessions:
            continue
        manifest = _load_session_manifest(
            manifest_path,
            session=source_session,
            contract_id=contract.contract_id,
            strategy_parameter_set_id=contract.strategy_parameter_set_id,
        )
        if manifest is None:
            continue
        promoted_id = manifest["promoted_attempt_id"]
        promoted = next(
            (
                value
                for value in manifest["attempts"]
                if isinstance(value, Mapping)
                and value.get("attempt_id") == promoted_id
            ),
            None,
        )
        if promoted is None:
            raise RuntimeError("promoted forward live object is unavailable")
        live, snapshot = _attempt_live_snapshot(
            manifest_path.parent.resolve(), promoted
        )
        _review_at, signals = validate_live_review_snapshot(
            snapshot,
            session=source_session,
        )
        identity = promoted.get("live_object")
        if not isinstance(identity, Mapping):
            raise RuntimeError("promoted forward live identity is unavailable")
        output.append(
            ForwardWarmupLineageSessionSnapshot(
                session=source_session,
                live_object_file_sha256=str(identity.get("file_sha256") or ""),
                live_object_content_sha256=str(
                    live.get("content_sha256") or ""
                ),
                snapshot_content_sha256=str(
                    live.get("source_content_sha256") or ""
                ),
                signals=tuple(signals),
            )
        )
    return tuple(output)


def _forward_review_session_qualification(
    *,
    args: argparse.Namespace,
    through_session: date,
) -> dict[str, object]:
    """Admit only earlier sessions whose full forward delivery is proven.

    The current session's manifest exists before its terminal ``EVALUATED``
    event is appended.  Counting it here would create a circular proof and let
    an ultimately blocked run inflate the cumulative sample, so it becomes
    eligible only when a later session independently reopens the complete
    Capture -> DataReady -> Evaluate chain.
    """

    root, ledger_path = _paths(args)
    observed_at = datetime.combine(through_session, time(15, 20), tzinfo=CN)
    sessions_root = root / "sessions"
    manifest_sessions: list[date] = []
    if sessions_root.is_dir():
        for manifest_path in sorted(
            sessions_root.glob("*/forward_session_manifest.json")
        ):
            try:
                source_session = date.fromisoformat(manifest_path.parent.name)
            except ValueError as exc:
                raise RuntimeError("forward session directory date is invalid") from exc
            if source_session <= through_session:
                manifest_sessions.append(source_session)

    events: tuple[Mapping[str, object], ...] = ()
    contract = load_forward_contract(args.parameter_snapshot.resolve())
    ledger_error: str | None = None
    ledger_content_sha256: str | None = None
    if ledger_path.is_file():
        try:
            ledger = load_forward_paper_ledger(ledger_path, contract=contract)
            events = tuple(ledger["events"])
            ledger_content_sha256 = str(ledger["content_sha256"])
        except (OSError, TypeError, ValueError) as exc:
            ledger_error = f"{type(exc).__name__}: {str(exc)[:200]}"
    else:
        ledger_error = "forward paper ledger is unavailable"

    qualified: list[str] = []
    qualified_evidence: list[dict[str, object]] = []
    excluded: list[dict[str, object]] = []
    sector_ledger = args.sector_ledger.resolve()
    for source_session in manifest_sessions:
        if source_session >= through_session:
            excluded.append(
                {
                    "session": source_session.isoformat(),
                    "reason_code": "CURRENT_SESSION_TERMINAL_EVENT_PENDING",
                }
            )
            continue
        if ledger_error is not None:
            excluded.append(
                {
                    "session": source_session.isoformat(),
                    "reason_code": "FORWARD_LEDGER_UNAVAILABLE",
                    "detail": ledger_error,
                }
            )
            continue
        try:
            trading_evidence = authoritative_trading_session_evidence(
                session=source_session,
                observed_at=observed_at,
                calendar_path=getattr(
                    args,
                    "trading_calendar",
                    DEFAULT_OFFICIAL_TRADING_CALENDAR_PATH,
                ),
                fallback_provider=qmt_trading_session_evidence,
            )
            sector_readiness = audit_forward_sector_capture_readiness(
                output=sector_ledger,
                session=source_session,
                decision_time=datetime.combine(
                    source_session,
                    time(15),
                    tzinfo=CN,
                ),
            )
            delivery = audit_forward_paper_session_delivery(
                events,
                session=source_session,
                observed_at=observed_at,
                sector_capture_readiness=sector_readiness,
                trading_session_evidence=trading_evidence,
                forward_root=root,
            )
        except (OSError, RuntimeError, TypeError, ValueError) as exc:
            excluded.append(
                {
                    "session": source_session.isoformat(),
                    "reason_code": "FORWARD_DELIVERY_AUDIT_FAILED",
                    "detail": f"{type(exc).__name__}: {str(exc)[:200]}",
                }
            )
            continue
        if delivery.get("ready") is True and delivery.get("reason_code") == "READY":
            qualified.append(source_session.isoformat())
            qualified_evidence.append(
                {
                    "session": source_session.isoformat(),
                    "delivery_audit": delivery,
                    "delivery_audit_content_sha256": sha256_json(delivery),
                }
            )
        else:
            excluded.append(
                {
                    "session": source_session.isoformat(),
                    "reason_code": str(
                        delivery.get("reason_code") or "FORWARD_DELIVERY_NOT_READY"
                    ),
                    "delivery_status": delivery.get("status"),
                    "delivery_audit": delivery,
                    "delivery_audit_content_sha256": sha256_json(delivery),
                }
            )

    stable: dict[str, object] = {
        "schema": FORWARD_REVIEW_SESSION_QUALIFICATION_SCHEMA,
        "through_session": through_session.isoformat(),
        "observed_at": observed_at.isoformat(),
        "qualified_sessions": qualified,
        "qualified_session_evidence": qualified_evidence,
        "excluded_sessions": excluded,
        "qualified_session_count": len(qualified),
        "excluded_session_count": len(excluded),
        "current_session_excluded_until_terminal_event": True,
        "forward_ledger_content_sha256": ledger_content_sha256,
        "real_account_accessed": False,
        "real_order_transport_enabled": False,
        "live_status": "LIVE_DISABLED",
    }
    return {**stable, "content_sha256": sha256_json(stable)}


def _qualified_forward_review_session_dates(
    qualification: Mapping[str, object],
    *,
    through_session: date,
) -> frozenset[date]:
    try:
        return qualified_forward_review_session_dates(
            qualification,
            through_session=through_session,
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise RuntimeError(
            "forward review session qualification is invalid"
        ) from exc


def _forward_review_price_bars(
    *,
    samples: Sequence[ForwardReviewSample],
    through_session: date,
) -> tuple[
    dict[str, tuple[ReviewPriceBar, ...]],
    dict[str, dict[str, object]],
]:
    """Read causally adjusted 1m bars without treating gaps as empty success."""

    end_at = datetime.combine(through_session, time(15), tzinfo=CN)
    bars_by_symbol: dict[str, tuple[ReviewPriceBar, ...]] = {}
    audits: dict[str, dict[str, object]] = {}
    symbols = sorted({sample.alert.symbol for sample in samples})
    for symbol in symbols:
        first_review = min(
            sample.alert.review_available_at
            for sample in samples
            if sample.alert.symbol == symbol
        )
        start_at = datetime.combine(first_review.date(), time(9, 30), tzinfo=CN)
        try:
            raw_frame = load_qmt_frame(
                symbol,
                "1m",
                start_at=start_at,
                end_at=end_at,
            )
            normalized_raw_frame = (
                normalize_qmt_opening_events_for_completed_minutes(raw_frame)
            )
            factor_events = _qmt_forward_review_factor_events(
                symbol=symbol,
                start=first_review.date(),
                end=through_session,
            )
            factor_revision = qmt_causal_factor_revision(
                members=(symbol,),
                events_by_code={symbol: factor_events},
                known_through=through_session,
            )
            frame = apply_qmt_causal_factor_adjustment(
                normalized_raw_frame,
                code=symbol,
                events=factor_events,
            )
            rows: list[ReviewPriceBar] = []
            for row in frame.itertuples(index=False):
                observed_at = pd.Timestamp(row.date).to_pydatetime()
                if observed_at > end_at:
                    continue
                rows.append(
                    ReviewPriceBar(
                        observed_at=observed_at,
                        high=Decimal(str(row.high)),
                        low=Decimal(str(row.low)),
                        close=Decimal(str(row.close)),
                    )
                )
            raw_bar_revision = sha256_json(
                {
                    "schema": "chanlun-forward-markout-raw-1m-bars",
                    "symbol": symbol,
                    "through_session": through_session.isoformat(),
                    "rows": tuple(
                        {
                            "observed_at": pd.Timestamp(row.date).isoformat(),
                            "open": format(Decimal(str(row.open)), "f"),
                            "high": format(Decimal(str(row.high)), "f"),
                            "low": format(Decimal(str(row.low)), "f"),
                            "close": format(Decimal(str(row.close)), "f"),
                            "volume": format(Decimal(str(row.volume)), "f"),
                        }
                        for row in raw_frame.itertuples(index=False)
                        if pd.Timestamp(row.date).to_pydatetime() <= end_at
                    ),
                }
            )
            adjusted_bar_revision = review_price_bars_revision(
                symbol=symbol,
                through_session=through_session,
                bars=rows,
            )
            bars_by_symbol[symbol] = tuple(rows)
            audits[symbol] = {
                "status": "AVAILABLE",
                "source_audit_contract_id": (
                    FORWARD_REVIEW_SOURCE_AUDIT_CONTRACT_ID
                ),
                "row_count": len(rows),
                "raw_row_count": len(raw_frame),
                "normalized_row_count": len(normalized_raw_frame),
                "first_at": (
                    None if not rows else rows[0].observed_at.isoformat()
                ),
                "last_at": (
                    None if not rows else rows[-1].observed_at.isoformat()
                ),
                "transport": raw_frame.attrs.get("qmt_transport"),
                "source_sha256": raw_frame.attrs.get(
                    "qmt_local_cache_source_sha256"
                ),
                "price_adjustment": "CAUSAL_KNOWN_EX_DATE_RAW_PRICE_DIVISOR",
                "opening_event_normalization": (
                    QMT_COMPLETED_ONE_MINUTE_GRID_REVISION
                ),
                "factor_contract_id": (
                    QMT_CAUSAL_FACTOR_ADJUSTMENT_CONTRACT_ID
                ),
                "factor_known_through": through_session.isoformat(),
                "factor_event_count": len(factor_events),
                "factor_revision": factor_revision,
                "factor_events": tuple(
                    event.canonical_payload() for event in factor_events
                ),
                "raw_bar_revision": raw_bar_revision,
                "adjusted_bar_revision": adjusted_bar_revision,
            }
        except Exception as exc:
            # 单个不可用标的必须明确记录，但不能抹除其他全部候选的因果观测。
            bars_by_symbol[symbol] = ()
            audits[symbol] = {
                "status": "UNAVAILABLE",
                "row_count": 0,
                "reason": f"{type(exc).__name__}: {str(exc)[:160]}",
            }
    return bars_by_symbol, audits


def _qmt_forward_review_factor_events(
    *,
    symbol: str,
    start: date,
    end: date,
) -> tuple[QmtCausalFactorEvent, ...]:
    """Load only factor events knowable by the mark-out cutoff."""

    if end < start:
        raise ValueError("forward review factor range is inverted")
    from xtquant import xtdata

    frame = xtdata.get_divid_factors(
        qmt_native_code(symbol),
        start.strftime("%Y%m%d"),
        end.strftime("%Y%m%d"),
    )
    return qmt_causal_factor_events_from_frame(
        code=symbol,
        frame=frame,
        not_before=start,
        not_after=end,
    )


def _forward_review_markout(
    *,
    args: argparse.Namespace,
    session: date,
) -> dict[str, object]:
    """Persist the cumulative diagnostic mark-out, separate from paper P&L."""

    qualification = _forward_review_session_qualification(
        args=args,
        through_session=session,
    )
    eligible_sessions = _qualified_forward_review_session_dates(
        qualification,
        through_session=session,
    )
    lineage_sources = _promoted_forward_warmup_lineage_sources(
        args=args,
        through_session=session,
        eligible_sessions=eligible_sessions,
    )
    lineage_rollup = build_forward_warmup_structure_lineage_rollup(
        lineage_sources,
        through_session=session,
        source_session_qualification_sha256=str(
            qualification["content_sha256"]
        ),
    )
    reports = _promoted_forward_review_reports(
        args=args,
        through_session=session,
        eligible_sessions=eligible_sessions,
    )
    samples = select_first_strategic_buy_samples(
        reports,
        through_session=session,
    )
    trading_sessions = (
        ()
        if not samples
        else _qmt_human_paper_trading_sessions(
            start=min(
                sample.alert.review_available_at.date() for sample in samples
            ),
            end=session,
        )
    )
    bars_by_symbol, source_audits = _forward_review_price_bars(
        samples=samples,
        through_session=session,
    )
    feedback_path = args.human_feedback_ledger.resolve()
    feedback: Sequence[Mapping[str, object]] = ()
    feedback_sha256 = None
    if feedback_path.is_file():
        feedback_ledger = load_human_review_feedback_ledger(feedback_path)
        feedback = tuple(feedback_ledger["entries"])
        feedback_sha256 = str(feedback_ledger["content_sha256"])
    report = build_forward_review_markout(
        samples,
        through_session=session,
        trading_sessions=trading_sessions,
        bars_by_symbol=bars_by_symbol,
        source_session_qualification=qualification,
        source_audits=source_audits,
        feedback=feedback,
    )
    root, _ledger = _paths(args)
    output, file_sha256 = _immutable_json_object(
        root,
        kind="forward_review_markout",
        payload=report,
    )
    lineage_output, lineage_file_sha256 = _immutable_json_object(
        root,
        kind="forward_warmup_structure_lineage_rollup",
        payload=lineage_rollup,
    )
    latest = root / "forward_review_markout.json"
    _atomic_json(latest, report)
    lineage_latest = root / "forward_warmup_structure_lineage_rollup.json"
    _atomic_json(lineage_latest, lineage_rollup)
    unavailable_count = sum(
        value.get("status") != "AVAILABLE" for value in source_audits.values()
    )
    return {
        "result": str(output),
        "result_file_sha256": file_sha256,
        "latest_result": str(latest),
        "content_sha256": report["content_sha256"],
        "through_session": session.isoformat(),
        "unique_lifecycle_count": report["sample"]["unique_lifecycle_count"],
        "feedback_linked_lifecycle_count": report["sample"][
            "feedback_linked_lifecycle_count"
        ],
        "eligible_by_horizon": report["sample"]["eligible_by_horizon"],
        "unavailable_symbol_count": unavailable_count,
        "feedback_ledger_content_sha256": feedback_sha256,
        "warmup_structure_lineage_rollup": {
            "result": str(lineage_output),
            "result_file_sha256": lineage_file_sha256,
            "latest_result": str(lineage_latest),
            "content_sha256": lineage_rollup["content_sha256"],
            "status": lineage_rollup["status"],
            "qualified_session_count": lineage_rollup[
                "qualified_session_count"
            ],
            "recorded_session_count": lineage_rollup[
                "recorded_session_count"
            ],
            "structure_event_count": lineage_rollup[
                "structure_event_count"
            ],
            "diagnostic_only": True,
            "live_status": "LIVE_DISABLED",
        },
        "source_session_qualification": qualification,
        "qualified_source_session_count": qualification[
            "qualified_session_count"
        ],
        "excluded_source_session_count": qualification[
            "excluded_session_count"
        ],
        "diagnostic_only": True,
        "portfolio_performance_evaluable": False,
        "orders_created": 0,
        "fills_created": 0,
        "live_status": "LIVE_DISABLED",
    }


def _candidate_warmup_diagnostic(
    *,
    args: argparse.Namespace,
    archived_screen_path: Path,
    source_content_sha256: str,
) -> dict[str, object]:
    """Build or reuse bounded multi-prefix evidence for the review page.

    This subprocess is deliberately outside the decision core.  A partial or
    failed diagnostic remains visible evidence but never blocks an otherwise
    valid ``EVALUATED`` event, changes ranking, or creates a virtual/real order.
    """

    root, _ledger = _paths(args)
    parameter_set_id = candidate_warmup_parameter_set_id()
    alias_path = candidate_warmup_diagnostic_path(
        root,
        source_content_sha256=source_content_sha256,
        parameter_set_id=parameter_set_id,
    )

    def load_alias() -> dict[str, object]:
        try:
            raw = json.loads(alias_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise RuntimeError("candidate warmup diagnostic is unreadable") from exc
        if not isinstance(raw, Mapping):
            raise RuntimeError("candidate warmup diagnostic is malformed")
        return validate_candidate_warmup_diagnostic_document(
            raw,
            expected_source_content_sha256=source_content_sha256,
            expected_parameter_set_id=parameter_set_id,
        )

    reused = alias_path.is_file()
    if reused:
        report = load_alias()
    else:
        alias_path.parent.mkdir(parents=True, exist_ok=True)
        command = (
            sys.executable,
            str(PROJECT_ROOT / "tools" / "audit_qmt_warmup_convergence.py"),
            "--snapshot",
            str(archived_screen_path),
            "--output",
            str(alias_path),
            "--limit",
            str(DEFAULT_CANDIDATE_LIMIT),
        )
        environment = os.environ.copy()
        if args.qmt_local_data_dir is not None:
            environment[QMT_LOCAL_DATA_ENV] = str(
                args.qmt_local_data_dir.resolve()
            )
        try:
            result = subprocess.run(
                command,
                cwd=PROJECT_ROOT,
                env=environment,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=600,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError(
                "candidate warmup diagnostic exceeded 600 seconds"
            ) from exc
        _atomic_bytes(
            alias_path.with_suffix(".log"),
            (result.stdout or "").encode("utf-8"),
        )
        # 退出码 3 表示工具的有效 PARTIAL 报告；证据是否可复用由文件契约决定，
        # 不能只看进程是否成功。
        if result.returncode not in {0, 3} or not alias_path.is_file():
            raise RuntimeError(
                "candidate warmup diagnostic process failed with "
                f"exit code {result.returncode}"
            )
        report = load_alias()

    object_path = _immutable_semantic_json_object(
        root,
        kind="candidate_warmup_diagnostic",
        payload=report,
    )
    return {
        "status": report["status"],
        "result": str(object_path),
        "latest_result": str(alias_path),
        "content_sha256": report["content_sha256"],
        "source_content_sha256": report["source_content_sha256"],
        "diagnostic_parameter_set_id": report[
            "diagnostic_parameter_set_id"
        ],
        "selected_candidate_count": len(report["codes"]),
        "error_count": len(report["errors"]),
        "reused": reused,
        "diagnostic_only": True,
        "active_gate_unchanged": True,
        "ranking_parameters_unchanged": True,
        "candidate_identity_unchanged": True,
        "paper_observation_eligibility_unchanged": True,
        "real_account_accessed": False,
        "real_order_transport_enabled": False,
        "automated_order_authorized": False,
        "live_status": "LIVE_DISABLED",
    }


def _append(
    *,
    args: argparse.Namespace,
    phase: str,
    status: str,
    evidence: Mapping[str, object],
) -> tuple[dict[str, object], dict[str, object], bool]:
    _root, ledger_path = _paths(args)
    contract = load_forward_contract(args.parameter_snapshot.resolve())
    frozen_implementation = _implementation_provenance()
    if _current_implementation_provenance() != frozen_implementation:
        # 子工具在评估期间从磁盘加载；若只在启动时哈希，运行中修改会在进程未实际使用的
        # 来源身份下执行。此时保持账本不变，并在一个稳定实现下重试。
        raise RuntimeError(
            "forward implementation changed while the command was running"
        )
    evidence_value = dict(evidence)
    if "implementation_provenance" in evidence_value:
        raise ValueError("implementation_provenance is reserved")
    evidence_value["implementation_provenance"] = frozen_implementation
    return append_forward_paper_event(
        ledger_path,
        contract=contract,
        session=_session(args),
        phase=phase,
        status=status,
        evidence=evidence_value,
        recorded_at=_now(),
    )


def _forward_implementation_continuity(
    *,
    args: argparse.Namespace,
    events: Sequence[Mapping[str, object]] | None = None,
    current_implementation: Mapping[str, object] | None = None,
) -> dict[str, object]:
    """Rebuild the same-session Capture-to-Evaluate source preflight."""

    if events is None:
        _root, ledger_path = _paths(args)
        contract = load_forward_contract(args.parameter_snapshot.resolve())
        events = (
            tuple(load_forward_paper_ledger(ledger_path, contract=contract)["events"])
            if ledger_path.is_file()
            else ()
        )
    return audit_forward_implementation_continuity(
        tuple(events),
        session=_session(args),
        current_implementation_provenance=(
            _implementation_provenance()
            if current_implementation is None
            else current_implementation
        ),
    )


def _start(args: argparse.Namespace) -> int:
    contract = load_forward_contract(args.parameter_snapshot.resolve())
    _document, event, reused = _append(
        args=args,
        phase="CONTROL",
        status="PAPER_STARTED",
        evidence={
            "authorization": "USER_REQUESTED_FORWARD_SIMULATION",
            "contract": contract.document(),
            "parameter_snapshot": str(args.parameter_snapshot.resolve()),
            "parameter_snapshot_sha256": contract.strategy_parameter_snapshot_sha256,
            "decision_core_shared_with_backtest": True,
            "automated_order_authorized": False,
            "human_confirmation_required": True,
            "real_account_access": False,
            "real_order_transport": False,
        },
    )
    _print({"event": event, "reused": reused})
    return 0


def _capture(args: argparse.Namespace) -> int:
    load_forward_contract(args.parameter_snapshot.resolve())
    sector_path = args.sector_ledger.resolve()
    capture_args = capture_parser().parse_args(
        [
            "--output",
            str(sector_path),
            "--source",
            args.source,
            *(
                []
                if args.qmt_local_data_dir is None
                else ["--qmt-local-data-dir", str(args.qmt_local_data_dir.resolve())]
            ),
        ]
    )
    try:
        receipt = capture_daily(capture_args)
    except Exception as exc:
        _document, event, _reused = _append(
            args=args,
            phase="CAPTURE",
            status="CAPTURE_FAILED",
            evidence={
                "error": f"{type(exc).__name__}: {exc}",
                "sector_ledger": str(sector_path),
            },
        )
        _print({"event": event})
        return 2
    receipt_path = Path(str(receipt["receipt_path"]))
    if date.fromisoformat(str(receipt["capture_session"])) != _session(args):
        raise RuntimeError("QMT sector receipt session differs from requested session")
    pit_path, pit_evidence, pit_reason = _session_pit_snapshot(
        args=args,
        session=_session(args),
    )
    _document, event, reused = _append(
        args=args,
        phase="CAPTURE",
        status="CAPTURED",
        evidence={
            "receipt": receipt,
            "receipt_sha256": sha256_file(receipt_path),
            "sector_ledger_sha256": sha256_file(sector_path),
            "pit_snapshot": str(pit_path),
            "pit_capture": pit_evidence,
            "pit_capture_reason": pit_reason,
        },
    )
    _print({"event": event, "reused": reused, "receipt": receipt})
    return 0


def _frame_session_audit(
    frame: pd.DataFrame,
    session: date,
    *,
    not_after: datetime | None = None,
) -> dict[str, object]:
    if frame.empty:
        return {"row_count": 0, "first_at": None, "last_at": None}
    dates = tuple(pd.Timestamp(value).to_pydatetime() for value in frame["date"])
    same = tuple(
        value
        for value in dates
        if value.date() == session and (not_after is None or value <= not_after)
    )
    return {
        "row_count": len(same),
        "first_at": None if not same else same[0].isoformat(),
        "last_at": None if not same else same[-1].isoformat(),
    }


def _qmt_rpc_market_frame(
    *,
    code: str,
    frequency: str,
    start_at: datetime,
    end_at: datetime,
    minimum_rows: int,
    skip_download: bool = True,
) -> pd.DataFrame:
    """从 QMT 读取同一交易日已完成的 K 线，默认不下载数据。

    本函数不导入或调用账户、持仓、订单和成交接口。默认只读；只有两个只读来源均证明
    数据不完整后，调用方才可显式传入 ``skip_download=False`` 执行一次有界官方 K 线
    刷新，且仍不得合成或推测市场事实。
    """

    from chanlun.exchange import Market, get_exchange
    from chanlun.exchange.price_basis import QMT_STRUCTURE_DIVIDEND_TYPE

    exchange = get_exchange(Market.A)
    loader = getattr(exchange, "klines", None)
    if not callable(loader):
        raise TypeError("QMT market-data RPC must expose klines")
    query_end = end_at + timedelta(
        minutes=1 if frequency == "1m" else 5
    )
    return loader(
        code,
        frequency,
        start_date=start_at.strftime("%Y%m%d%H%M%S"),
        # QMT 原生结束边界右开，因此请求到下一根 K 线边界，再由数据闸门按因果性截断到
        # ``end_at``；否则会静默遗漏 15:00 收盘 K 线。
        end_date=query_end.strftime("%Y%m%d%H%M%S"),
        args={
            "req_counts": minimum_rows,
            "skip_download": skip_download,
            "research_exact_end": True,
            "dividend_type": QMT_STRUCTURE_DIVIDEND_TYPE,
        },
    )


def _qmt_incremental_market_frame(**kwargs: object) -> pd.DataFrame:
    """Request one bounded official K-line refresh, never a tick conversion."""

    return _qmt_rpc_market_frame(**kwargs, skip_download=False)


def _completed_one_minute_to_five(
    frame: pd.DataFrame,
    *,
    session: date,
) -> pd.DataFrame:
    """Aggregate an exact A-share 240-minute grid into 48 completed 5m bars."""

    required = ("date", "open", "high", "low", "close", "volume")
    if not isinstance(frame, pd.DataFrame) or any(
        column not in frame.columns for column in required
    ):
        raise ValueError("completed 1m source is missing OHLCV columns")
    morning = tuple(
        datetime.combine(session, time(9, 31), tzinfo=CN)
        + timedelta(minutes=index)
        for index in range(120)
    )
    afternoon = tuple(
        datetime.combine(session, time(13, 1), tzinfo=CN)
        + timedelta(minutes=index)
        for index in range(120)
    )
    expected = (*morning, *afternoon)
    values = frame.loc[:, [column for column in frame.columns if column in {
        "code", *required
    }]].copy()
    dates = tuple(pd.Timestamp(value).to_pydatetime() for value in values["date"])
    positions = tuple(index for index, value in enumerate(dates) if value.date() == session)
    same_session = values.iloc[list(positions)].copy().reset_index(drop=True)
    actual = tuple(
        pd.Timestamp(value).to_pydatetime() for value in same_session["date"]
    )
    if actual != expected:
        raise ValueError("completed 1m source does not match the 240-bar session grid")

    rows: list[dict[str, object]] = []
    for session_offset in (0, 120):
        for offset in range(session_offset, session_offset + 120, 5):
            chunk = same_session.iloc[offset : offset + 5]
            row: dict[str, object] = {
                "date": actual[offset + 4],
                "open": chunk["open"].iloc[0],
                "high": chunk["high"].max(),
                "low": chunk["low"].min(),
                "close": chunk["close"].iloc[-1],
                "volume": chunk["volume"].sum(),
            }
            if "code" in same_session:
                row["code"] = same_session["code"].iloc[0]
            rows.append(row)
    columns = (
        ("code", "date", "open", "high", "low", "close", "volume")
        if "code" in same_session
        else ("date", "open", "high", "low", "close", "volume")
    )
    result = pd.DataFrame(rows, columns=columns)
    result.attrs = dict(frame.attrs)
    return result


def _market_data_gate(
    *,
    session: date,
    qmt_data_dir: Path,
    native_frame_provider: Callable[..., pd.DataFrame] | None = None,
    refresh_frame_provider: Callable[..., pd.DataFrame] | None = None,
) -> dict[str, object]:
    os.environ[QMT_LOCAL_DATA_ENV] = str(qmt_data_dir.resolve())
    start_at = datetime.combine(session, time(9, 30), tzinfo=CN)
    end_at = datetime.combine(session, time(15), tzinfo=CN)
    audits: dict[str, dict[str, object]] = {}
    selected_frames: dict[str, pd.DataFrame] = {}
    requirements = {"1m": 240, "5m": 48}
    reasons: list[str] = []

    def expected_last_at(frequency: str) -> datetime:
        # QMT 在区间结束时标记连续 1m/5m K 线；09:30 记录属于开盘集合竞价，不在 240 根
        # 连续一分钟 K 线中，完整连续交易时段结束于 15:00。
        return end_at

    def complete(
        frequency: str,
        audit: Mapping[str, object],
        minimum_rows: int,
    ) -> bool:
        last_at = audit.get("last_at")
        return (
            int(audit.get("row_count", 0)) >= minimum_rows
            and last_at is not None
            and datetime.fromisoformat(str(last_at))
            >= expected_last_at(frequency)
        )

    for frequency, minimum_rows in requirements.items():
        attempts: list[dict[str, object]] = []
        candidates: list[tuple[str, dict[str, object], pd.DataFrame]] = []
        if native_frame_provider is not None:
            try:
                native = native_frame_provider(
                    code="SH.600000",
                    frequency=frequency,
                    start_at=start_at,
                    end_at=end_at,
                    minimum_rows=minimum_rows,
                )
                native_audit = _frame_session_audit(
                    native,
                    session,
                    not_after=end_at,
                )
                attempts.append({"source": "QMT_RPC", **native_audit})
                candidates.append(("QMT_RPC", native_audit, native))
            except Exception as exc:
                attempts.append(
                    {
                        "source": "QMT_RPC",
                        "error_type": type(exc).__name__,
                        "reason": str(exc)[:160],
                    }
                )
        native_complete = any(
            source == "QMT_RPC" and complete(frequency, candidate, minimum_rows)
            for source, candidate, _frame in candidates
        )
        if not native_complete:
            try:
                local = load_qmt_frame(
                    "SH.600000",
                    frequency,
                    start_at=start_at,
                    end_at=end_at,
                )
                local_audit = _frame_session_audit(
                    local,
                    session,
                    not_after=end_at,
                )
                attempts.append({"source": "QMT_LOCAL_CACHE", **local_audit})
                candidates.append(("QMT_LOCAL_CACHE", local_audit, local))
            except Exception as exc:
                attempts.append(
                    {
                        "source": "QMT_LOCAL_CACHE",
                        "error_type": type(exc).__name__,
                        "reason": str(exc)[:160],
                    }
                )
        any_complete = any(
            complete(frequency, candidate, minimum_rows)
            for _source, candidate, _frame in candidates
        )
        if frequency == "5m" and not any_complete and "1m" in selected_frames:
            try:
                one_minute = selected_frames["1m"].copy()
                one_minute.attrs = dict(selected_frames["1m"].attrs)
                if "code" not in one_minute.columns:
                    one_minute.insert(0, "code", "SH.600000")
                derived = _completed_one_minute_to_five(
                    one_minute,
                    session=session,
                )
                derived_audit = _frame_session_audit(
                    derived,
                    session,
                    not_after=end_at,
                )
                attempts.append(
                    {
                        "source": "QMT_COMPLETED_1M_RESAMPLED_5M",
                        **derived_audit,
                    }
                )
                candidates.append(
                    (
                        "QMT_COMPLETED_1M_RESAMPLED_5M",
                        derived_audit,
                        derived,
                    )
                )
                any_complete = complete(frequency, derived_audit, minimum_rows)
            except Exception as exc:
                attempts.append(
                    {
                        "source": "QMT_COMPLETED_1M_RESAMPLED_5M",
                        "error_type": type(exc).__name__,
                        "reason": str(exc)[:160],
                    }
                )
        if not any_complete and refresh_frame_provider is not None:
            try:
                refreshed = refresh_frame_provider(
                    code="SH.600000",
                    frequency=frequency,
                    start_at=start_at,
                    end_at=end_at,
                    minimum_rows=minimum_rows,
                )
                refreshed_audit = _frame_session_audit(
                    refreshed,
                    session,
                    not_after=end_at,
                )
                attempts.append(
                    {
                        "source": "QMT_RPC_INCREMENTAL_REFRESH",
                        **refreshed_audit,
                    }
                )
                candidates.append(
                    ("QMT_RPC_INCREMENTAL_REFRESH", refreshed_audit, refreshed)
                )
            except Exception as exc:
                attempts.append(
                    {
                        "source": "QMT_RPC_INCREMENTAL_REFRESH",
                        "error_type": type(exc).__name__,
                        "reason": str(exc)[:160],
                    }
                )
        if candidates:
            source, selected, selected_frame = max(
                candidates,
                key=lambda item: (
                    complete(frequency, item[1], minimum_rows),
                    int(item[1]["row_count"]),
                    str(item[1]["last_at"] or ""),
                ),
            )
            selected_frames[frequency] = selected_frame
            audit = {
                **selected,
                "source": source,
                "fallback_used": source != "QMT_RPC"
                and native_frame_provider is not None,
                "incremental_refresh_used": (
                    source == "QMT_RPC_INCREMENTAL_REFRESH"
                ),
                "resampled_from_completed_one_minute": (
                    source == "QMT_COMPLETED_1M_RESAMPLED_5M"
                ),
                "attempts": tuple(attempts),
            }
        else:
            audit = {
                "row_count": 0,
                "first_at": None,
                "last_at": None,
                "source": "UNAVAILABLE",
                "fallback_used": native_frame_provider is not None,
                "incremental_refresh_used": False,
                "resampled_from_completed_one_minute": False,
                "attempts": tuple(attempts),
            }
        audit["minimum_rows"] = minimum_rows
        audit["expected_last_at"] = expected_last_at(frequency).isoformat()
        audits[frequency] = audit
        last_at = audit["last_at"]
        if int(audit["row_count"]) < minimum_rows:
            reasons.append(f"{frequency.upper()}_SESSION_ROWS_INCOMPLETE")
        if (
            last_at is None
            or datetime.fromisoformat(str(last_at))
            < expected_last_at(frequency)
        ):
            reasons.append(f"{frequency.upper()}_SESSION_CLOSE_INCOMPLETE")
    return {
        "complete": not reasons,
        "symbol": "SH.600000",
        "session": session.isoformat(),
        "frequencies": audits,
        "reason_codes": tuple(dict.fromkeys(reasons)),
        "native_rpc_market_data_attempted": native_frame_provider is not None,
        "native_rpc_skip_download": native_frame_provider is not None,
        "bounded_incremental_refresh_enabled": refresh_frame_provider is not None,
        "market_data_was_synthesized": False,
        "tick_data_used": False,
        "minimum_market_data_frequency": "1m",
        "real_account_accessed": False,
        "real_order_transport_enabled": False,
    }


def _session_pit_snapshot(
    *,
    args: argparse.Namespace,
    session: date,
) -> tuple[Path, dict[str, object], str | None]:
    """Use only a PIT snapshot completed before the session close."""

    root, _ledger = _paths(args)
    session_dir = root / "sessions" / session.isoformat()
    daily = session_dir / "pit_metadata.json"
    candidates = (daily, args.pit_snapshot.resolve())
    for path in candidates:
        if not path.is_file():
            continue
        try:
            snapshot = load_snapshot(path)
        except Exception:
            continue
        completed_at = datetime.fromtimestamp(path.stat().st_mtime, tz=CN)
        if (
            snapshot.source_end >= session
            and snapshot.captured_at.date() == session
            and completed_at <= datetime.combine(session, time(15), tzinfo=CN)
        ):
            return (
                path,
                {
                    "pit_source_end": snapshot.source_end.isoformat(),
                    "pit_captured_at": snapshot.captured_at.isoformat(),
                    "pit_completed_at": completed_at.isoformat(),
                    "pit_snapshot_sha256": sha256_file(path),
                    "pit_snapshot_refreshed": False,
                },
                None,
            )
    fallback = args.pit_snapshot.resolve()
    try:
        snapshot = load_snapshot(fallback)
        evidence = {
            "pit_source_end": snapshot.source_end.isoformat(),
            "pit_snapshot_sha256": sha256_file(fallback),
            "pit_snapshot_refreshed": False,
        }
    except Exception as exc:
        evidence = {"pit_error": f"{type(exc).__name__}: {exc}"}
    return fallback, evidence, "PIT_SECURITY_AND_ACTION_METADATA_NOT_CURRENT"


def _evaluate(args: argparse.Namespace) -> int:
    session = _session(args)
    contract = load_forward_contract(args.parameter_snapshot.resolve())
    implementation_continuity = _forward_implementation_continuity(args=args)
    if implementation_continuity["ready"] is not True:
        _document, blocked, blocked_reused = _append(
            args=args,
            phase="DECISION",
            status="EVALUATION_BLOCKED",
            evidence={
                "failed_step": "implementation_continuity_preflight",
                "implementation_continuity": implementation_continuity,
                "market_data_read": False,
                "pipeline_started": False,
            },
        )
        _print(
            {
                "decision_event": blocked,
                "decision_event_reused": blocked_reused,
                "implementation_continuity": implementation_continuity,
                "market_data_read": False,
                "pipeline_started": False,
            }
        )
        return 7
    sector_path = args.sector_ledger.resolve()
    decision_close = datetime.combine(session, time(15), tzinfo=CN)
    reasons: list[str] = []
    captured_sector_catalog: Mapping[str, object] | None = None
    evidence: dict[str, object] = {
        "session": session.isoformat(),
        "sector_ledger": str(sector_path),
        "pit_snapshot": str(args.pit_snapshot.resolve()),
        "qmt_local_data_dir": (
            None
            if args.qmt_local_data_dir is None
            else str(args.qmt_local_data_dir.resolve())
        ),
    }
    try:
        sector_capture_readiness = audit_forward_sector_capture_readiness(
            output=sector_path,
            session=session,
            decision_time=decision_close,
        )
        evidence["sector_capture_readiness"] = {
            key: value
            for key, value in sector_capture_readiness.items()
            if key not in {"catalog", "receipt_audit"}
        }
        if sector_capture_readiness["ready"] is not True:
            reasons.append(str(sector_capture_readiness["reason_code"]))
        else:
            catalog = sector_capture_readiness["catalog"]
            if not isinstance(catalog, Mapping):
                raise ValueError("ready sector capture has no catalog")
            evidence["sector_catalog_entry_sha256"] = catalog["ledger_entry_sha256"]
            evidence["sector_catalog_revision"] = catalog["catalog_revision"]
            captured_sector_catalog = catalog
            evidence["sector_capture_at"] = catalog["captured_at"]
            evidence["sector_count"] = len(catalog["sectors"])
            evidence["sector_ledger_sha256"] = sha256_file(sector_path)
    except Exception as exc:
        reasons.append("SECTOR_CAPTURE_LEDGER_INVALID")
        evidence["sector_error"] = f"{type(exc).__name__}: {exc}"
    if args.qmt_local_data_dir is None:
        reasons.append("QMT_LOCAL_DATA_DIRECTORY_UNAVAILABLE")
    else:
        try:
            market = _market_data_gate(
                session=session,
                qmt_data_dir=args.qmt_local_data_dir,
                native_frame_provider=_qmt_rpc_market_frame,
                refresh_frame_provider=_qmt_incremental_market_frame,
            )
            evidence["market_data_gate"] = market
            reasons.extend(str(value) for value in market["reason_codes"])
        except Exception as exc:
            reasons.append("QMT_MARKET_DATA_GATE_FAILED")
            evidence["market_data_error"] = f"{type(exc).__name__}: {exc}"
    pit_path, pit_evidence, pit_reason = _session_pit_snapshot(
        args=args,
        session=session,
    )
    evidence.update(pit_evidence)
    evidence["effective_pit_snapshot"] = str(pit_path)
    if pit_reason is not None:
        evidence["pit_warning"] = pit_reason
        evidence["pit_policy"] = (
            "WARNING_ONLY_FOR_CURRENT_QMT_HUMAN_REVIEW_SCREENING"
        )
    evidence["reason_codes"] = tuple(dict.fromkeys(reasons))
    if reasons:
        _document, event, reused = _append(
            args=args,
            phase="DATA_GATE",
            status="DATA_BLOCKED",
            evidence=evidence,
        )
        _print({"event": event, "reused": reused, "pipeline_started": False})
        return 3
    _document, event, reused = _append(
        args=args,
        phase="DATA_GATE",
        status="DATA_READY",
        evidence=evidence,
    )
    if contract.technical_mode == "HUMAN_REVIEW_SCREENING":
        if captured_sector_catalog is None:
            raise RuntimeError("same-session sector catalog identity is unavailable")
        try:
            archived, archive_evidence = _archive_live_screening_snapshot(
                args=args,
                session=session,
                expected_sector_catalog=captured_sector_catalog,
            )
        except Exception as exc:
            archive_evidence = {
                "failed_step": "live_screening_snapshot_validation",
                "error": f"{type(exc).__name__}: {exc}",
            }
            _document, blocked, blocked_reused = _append(
                args=args,
                phase="DECISION",
                status="EVALUATION_BLOCKED",
                evidence=archive_evidence,
            )
            _print(
                {
                    "data_event": event,
                    "data_event_reused": reused,
                    "decision_event": blocked,
                    "decision_event_reused": blocked_reused,
                    "pipeline_started": False,
                }
            )
            return 4
        try:
            archive_evidence["qmt_instrument_status_snapshot"] = (
                _capture_forward_screening_instrument_status(
                    args=args,
                    session=session,
                    archived_screen=archived,
                    sector_catalog=captured_sector_catalog,
                )
            )
        except Exception as exc:
            # 新证据只为后续交易日收集；失败不能削弱今日已有关闭失败数据闸门，也不能把
            # 先前有效人工复核屏幕变成不同交易决策。应明确保留覆盖缺口，并在下一次幂等
            # 评估时重试。
            archive_evidence["qmt_instrument_status_snapshot"] = {
                "status": "CAPTURE_INCOMPLETE",
                "session": session.isoformat(),
                "reason_code": "QMT_INSTRUMENT_STATUS_SNAPSHOT_UNAVAILABLE",
                "error": f"{type(exc).__name__}: {str(exc)[:240]}",
                "coverage_scope": "SCREENING_SIGNAL_SYMBOLS_ONLY",
                "can_explain_same_session_decision": False,
                "can_explain_prior_historical_session": False,
                "future_consumer_connected": False,
                "historical_backfill_allowed": False,
                "real_account_accessed": False,
                "real_order_transport_enabled": False,
                "live_status": "LIVE_DISABLED",
            }
        try:
            archive_evidence["human_paper_settlement"] = _settle_human_paper(
                args=args,
                session=session,
            )
        except Exception as exc:
            archive_evidence["human_paper_settlement"] = {
                "status": "VIRTUAL_SETTLEMENT_FAILED",
                "error": f"{type(exc).__name__}: {exc}",
                "broker_transport_available": False,
                "live_status": "LIVE_DISABLED",
            }
            # 屏幕对象已经是不可变且有用的复核事实，但已确认人工意图若无法结算，日级前向
            # 决策就未完成。此处返回成功会抑制任务计划程序的有界重试，可能让待处理虚拟
            # 意图永久悬空。应保留屏幕、关闭决策，并让幂等重试重新结算，始终不暴露下单传输。
            _document, blocked, blocked_reused = _append(
                args=args,
                phase="DECISION",
                status="EVALUATION_BLOCKED",
                evidence=archive_evidence,
            )
            _print(
                {
                    "data_event": event,
                    "data_event_reused": reused,
                    "decision_event": blocked,
                    "decision_event_reused": blocked_reused,
                    "pipeline_started": False,
                    "virtual_settlement_complete": False,
                }
            )
            return 5
        try:
            archive_evidence["human_paper_valuation"] = (
                _capture_human_paper_valuation(
                    args=args,
                    session=session,
                )
            )
        except Exception as exc:
            archive_evidence["human_paper_valuation"] = {
                "status": "VALUATION_FAILED",
                "error": f"{type(exc).__name__}: {str(exc)[:240]}",
                "equity_curve_point_available": False,
                "performance_evaluable": False,
                "minimum_market_data_frequency": "1m",
                "tick_data_used": False,
                "broker_transport_available": False,
                "live_status": "LIVE_DISABLED",
            }
            _document, blocked, blocked_reused = _append(
                args=args,
                phase="DECISION",
                status="EVALUATION_BLOCKED",
                evidence=archive_evidence,
            )
            _print(
                {
                    "data_event": event,
                    "data_event_reused": reused,
                    "decision_event": blocked,
                    "decision_event_reused": blocked_reused,
                    "pipeline_started": False,
                    "virtual_settlement_complete": True,
                    "daily_valuation_complete": False,
                }
            )
            return 6
        if (
            archive_evidence["human_paper_valuation"].get("status")
            != "VALUATION_COMPLETE"
        ):
            _document, blocked, blocked_reused = _append(
                args=args,
                phase="DECISION",
                status="EVALUATION_BLOCKED",
                evidence=archive_evidence,
            )
            _print(
                {
                    "data_event": event,
                    "data_event_reused": reused,
                    "decision_event": blocked,
                    "decision_event_reused": blocked_reused,
                    "pipeline_started": False,
                    "virtual_settlement_complete": True,
                    "daily_valuation_complete": False,
                }
            )
            return 6
        try:
            archive_evidence["forward_review_markout"] = (
                _forward_review_markout(
                    args=args,
                    session=session,
                )
            )
        except Exception as exc:
            # 后验收益是诊断只读模型，绝不能把有效不可变日级屏幕变成交易；但失败必须可见，
            # 不能误作零收益样本。
            archive_evidence["forward_review_markout"] = {
                "status": "MARKOUT_EVALUATION_FAILED",
                "error": f"{type(exc).__name__}: {str(exc)[:240]}",
                "diagnostic_only": True,
                "portfolio_performance_evaluable": False,
                "orders_created": 0,
                "fills_created": 0,
                "live_status": "LIVE_DISABLED",
            }
        try:
            archive_evidence["candidate_warmup_diagnostic"] = (
                _candidate_warmup_diagnostic(
                    args=args,
                    archived_screen_path=Path(str(archive_evidence["result"])),
                    source_content_sha256=str(
                        archive_evidence["source_content_sha256"]
                    ),
                )
            )
        except Exception as exc:
            # 深层预热证据是有界展示旁车；缺失必须明确，但不能改变归档屏幕、活动闸门、
            # 排序、虚拟资格或日级 EVALUATED 结果。
            archive_evidence["candidate_warmup_diagnostic"] = {
                "status": "DIAGNOSTIC_UNAVAILABLE",
                "reason_code": "CANDIDATE_WARMUP_DIAGNOSTIC_UNAVAILABLE",
                "error": f"{type(exc).__name__}: {str(exc)[:240]}",
                "diagnostic_only": True,
                "active_gate_unchanged": True,
                "ranking_parameters_unchanged": True,
                "candidate_identity_unchanged": True,
                "paper_observation_eligibility_unchanged": True,
                "portfolio_performance_evaluable": False,
                "real_account_accessed": False,
                "real_order_transport_enabled": False,
                "automated_order_authorized": False,
                "live_status": "LIVE_DISABLED",
            }
        _document, evaluated, evaluated_reused = _append(
            args=args,
            phase="DECISION",
            # 追加式账本保持冻结状态词表；下方证据模式和结果路径已把它标识为分阶段实时屏幕
            # 归档，若发明第二种成功状态，会让原本有效的账本无法被严格加载器读取。
            status="EVALUATED",
            evidence=archive_evidence,
        )
        _print(
            {
                "data_event": event,
                "data_event_reused": reused,
                "decision_event": evaluated,
                "decision_event_reused": evaluated_reused,
                "pipeline_started": False,
                "result": {
                    "schema": archived["schema"],
                    "candidate_count": archived["candidate_count"],
                    "scanner_error_count": archived["scanner_error_count"],
                    "orders_created": 0,
                    "fills_created": 0,
                    "highest_status": "REVIEW_REQUIRED",
                    "live_status": "LIVE_DISABLED",
                },
            }
        )
        return 0
def _status(args: argparse.Namespace) -> int:
    root, ledger_path = _paths(args)
    contract = load_forward_contract(args.parameter_snapshot.resolve())
    if not ledger_path.is_file():
        _print(
            {
                "status": "NOT_STARTED",
                "ledger": str(ledger_path),
                "contract": contract.document(),
            }
        )
        return 1
    ledger = load_forward_paper_ledger(ledger_path, contract=contract)
    events = tuple(ledger["events"])
    implementation_continuity = _forward_implementation_continuity(
        args=args,
        events=events,
        current_implementation=_current_implementation_provenance(),
    )
    status_observed_at = _now()
    trading_session_evidence = authoritative_trading_session_evidence(
        session=_session(args),
        observed_at=status_observed_at,
        calendar_path=getattr(
            args,
            "trading_calendar",
            DEFAULT_OFFICIAL_TRADING_CALENDAR_PATH,
        ),
        fallback_provider=qmt_trading_session_evidence,
    )
    paper_ledger = load_human_paper_ledger(args.human_paper_ledger.resolve())
    paper_terminal_intent_ids = human_paper_terminal_intent_ids(
        paper_ledger["events"]
    )
    paper_pending_intents = tuple(
        event["payload"]
        for event in paper_ledger["events"]
        if event.get("kind") == "INTENT"
        and isinstance(event.get("payload"), Mapping)
        and event["payload"].get("status") == "PENDING"
        and event["payload"].get("intent_id")
        not in paper_terminal_intent_ids
    )
    paper_entry_selection_settlement_gate = (
        _human_paper_entry_selection_settlement_gate(
            args=args,
            events=tuple(paper_ledger["events"]),
            pending=paper_pending_intents,
        )
    )
    paper_execution_evidence = audit_human_paper_execution_evidence(
        tuple(paper_ledger["events"]),
        forward_root=root,
    )
    paper_entry_boundary_attestation = (
        audit_human_paper_entry_boundary_attestations(
            tuple(paper_ledger["events"])
        )
    )
    paper_execution_rejection_evidence = (
        audit_human_paper_execution_rejection_evidence(
            tuple(paper_ledger["events"]),
            forward_root=root,
        )
    )
    paper_operations_cancellation_evidence = (
        audit_human_paper_operations_cancellation_evidence(
            tuple(paper_ledger["events"]),
            forward_root=root,
        )
    )
    paper_portfolio_rejection_evidence = (
        audit_human_paper_portfolio_rejection_evidence(
            tuple(paper_ledger["events"]),
            forward_root=root,
        )
    )
    status_accounting_parameters = None
    try:
        status_accounting_parameters = load_human_paper_accounting_parameters(
            args.parameter_snapshot.resolve()
        )
        paper_portfolio_decision_audit = audit_human_paper_portfolio_decisions(
            tuple(paper_ledger["events"]),
            parameters=status_accounting_parameters,
        )
        paper_portfolio_fill_decision_audit = (
            audit_human_paper_portfolio_fill_decisions(
                tuple(paper_ledger["events"]),
                parameters=status_accounting_parameters,
            )
        )
    except (OSError, TypeError, ValueError) as exc:
        paper_portfolio_decision_audit = {
            "schema": "chanlun-human-paper-portfolio-decision-audit",
            "status": "PARAMETER_SNAPSHOT_INVALID",
            "rejection_count": None,
            "verified_rejection_count": 0,
            "invalid_decisions": [
                {"reason": f"{type(exc).__name__}: {str(exc)[:200]}"}
            ],
            "broker_transport_available": False,
            "live_status": "LIVE_DISABLED",
        }
        paper_portfolio_fill_decision_audit = {
            "schema": (
                "chanlun-human-paper-portfolio-fill-decision-audit"
            ),
            "status": "PARAMETER_SNAPSHOT_INVALID",
            "approved_fill_count": None,
            "verified_approved_fill_count": 0,
            "invalid_decisions": [
                {"reason": f"{type(exc).__name__}: {str(exc)[:200]}"}
            ],
            "broker_transport_available": False,
            "live_status": "LIVE_DISABLED",
        }
    paper_pending_continuity = latest_human_paper_pending_continuity(
        tuple(paper_ledger["events"]),
        events,
    )
    try:
        if status_accounting_parameters is None:
            raise ValueError("human paper accounting parameters are unavailable")
        paper_accounting = rebuild_human_paper_accounting(
            tuple(paper_ledger["events"]),
            parameters=status_accounting_parameters,
            execution_evidence_status=str(
                paper_execution_evidence.get("status") or "INVALID"
            ),
        )
    except (OSError, TypeError, ValueError) as exc:
        paper_accounting = {
            "schema": "chanlun-human-paper-accounting",
            "status": "PARAMETER_SNAPSHOT_INVALID",
            "accounting_valid": False,
            "performance_evaluable": False,
            "reason_codes": [
                "FROZEN_ACCOUNTING_PARAMETER_SNAPSHOT_INVALID",
                f"{type(exc).__name__}: {str(exc)[:200]}",
            ],
            "broker_transport_available": False,
            "live_status": "LIVE_DISABLED",
        }
    valuation_source: dict[str, object] = {
        "forward_root": root,
    # ``events`` 来自上方已经冻结的前向账本校验器。
        # 成功 EVALUATED 的交易日构成日级估值连续性的权威运行日历；此处绝不猜测工作日
        # 或交易所假日。
        "forward_events": events,
    }
    if status_accounting_parameters is not None:
        valuation_source.update(
            paper_events=tuple(paper_ledger["events"]),
            accounting_parameters=status_accounting_parameters,
        )
    paper_valuation = audit_human_paper_valuation_evidence(**valuation_source)
    session_events = tuple(
        row for row in events if row["session"] == _session(args).isoformat()
    )
    sector_ledger_path = args.sector_ledger.resolve()
    sector_receipt_audit: dict[str, object]
    sector_capture_readiness: dict[str, object]
    if sector_ledger_path.is_file():
        try:
            sector_receipt_audit = audit_sector_capture_receipts(
                output=sector_ledger_path,
                required_capture_session=_required_sector_capture_session(
                    args,
                    observed_at=status_observed_at,
                    trading_session_evidence=trading_session_evidence,
                ),
            )
        except (OSError, TypeError, ValueError) as exc:
            sector_receipt_audit = {
                "schema": "chanlun-qmt-sector-receipt-audit",
                "status": "SECTOR_CAPTURE_LEDGER_INVALID",
                "ledger": str(sector_ledger_path),
                "entry_count": 0,
                "valid_receipt_count": 0,
                "error": f"{type(exc).__name__}: {str(exc)[:200]}",
                "historical_receipts_synthesized": False,
                "real_account_accessed": False,
                "real_order_transport_enabled": False,
                "live_status": "LIVE_DISABLED",
            }
        try:
            sector_capture_readiness = audit_forward_sector_capture_readiness(
                output=sector_ledger_path,
                session=_session(args),
                decision_time=datetime.combine(
                    _session(args),
                    time(15),
                    tzinfo=CN,
                ),
            )
        except (OSError, TypeError, ValueError) as exc:
            sector_capture_readiness = {
                "schema": "chanlun-forward-sector-capture-readiness",
                "ready": False,
                "status": "not_ready",
                "reason_code": "SECTOR_CAPTURE_LEDGER_INVALID",
                "session": _session(args).isoformat(),
                "receipt_proven": False,
                "error": f"{type(exc).__name__}: {str(exc)[:200]}",
                "real_account_accessed": False,
                "real_order_transport_enabled": False,
                "live_status": "LIVE_DISABLED",
            }
    else:
        sector_receipt_audit = {
            "schema": "chanlun-qmt-sector-receipt-audit",
            "status": "SECTOR_LEDGER_NOT_STARTED",
            "ledger": str(sector_ledger_path),
            "entry_count": 0,
            "valid_receipt_count": 0,
            "missing_entry_count": 0,
            "historical_receipts_synthesized": False,
            "real_account_accessed": False,
            "real_order_transport_enabled": False,
            "live_status": "LIVE_DISABLED",
        }
        sector_capture_readiness = {
            "schema": "chanlun-forward-sector-capture-readiness",
            "ready": False,
            "status": "not_ready",
            "reason_code": "SECTOR_LEDGER_NOT_STARTED",
            "session": _session(args).isoformat(),
            "receipt_proven": False,
            "real_account_accessed": False,
            "real_order_transport_enabled": False,
            "live_status": "LIVE_DISABLED",
        }
    session_delivery = audit_forward_paper_session_delivery(
        events,
        session=_session(args),
        observed_at=status_observed_at,
        sector_capture_readiness=sector_capture_readiness,
        trading_session_evidence=trading_session_evidence,
        forward_root=root,
    )
    lineage_qualification = _forward_review_session_qualification(
        args=args,
        through_session=_session(args),
    )
    lineage_eligible_sessions = _qualified_forward_review_session_dates(
        lineage_qualification,
        through_session=_session(args),
    )
    forward_warmup_structure_lineage = (
        build_forward_warmup_structure_lineage_rollup(
            _promoted_forward_warmup_lineage_sources(
                args=args,
                through_session=_session(args),
                eligible_sessions=lineage_eligible_sessions,
            ),
            through_session=_session(args),
            source_session_qualification_sha256=str(
                lineage_qualification["content_sha256"]
            ),
        )
    )
    _print(
        {
            "status": contract.operational_status,
            "session": _session(args),
            "ledger": str(ledger_path),
            "ledger_sha256": sha256_file(ledger_path),
            "contract_id": contract.contract_id,
            "strategy_parameter_set_id": contract.strategy_parameter_set_id,
            "event_count": len(events),
            "session_events": session_events,
            "session_delivery": session_delivery,
            "implementation_continuity_preflight": implementation_continuity,
            "trading_session_evidence": trading_session_evidence,
            "latest_event": None if not events else events[-1],
            "sector_capture_receipts": sector_receipt_audit,
            "sector_capture_readiness": sector_capture_readiness,
            "human_paper_ledger": str(args.human_paper_ledger.resolve()),
            "human_paper_ledger_content_sha256": paper_ledger["content_sha256"],
            "paper_execution_evidence": paper_execution_evidence,
            "paper_entry_boundary_attestation": (
                paper_entry_boundary_attestation
            ),
            "paper_entry_selection_settlement_gate": (
                paper_entry_selection_settlement_gate
            ),
            "paper_execution_rejection_evidence": (
                paper_execution_rejection_evidence
            ),
            "paper_operations_cancellation_evidence": (
                paper_operations_cancellation_evidence
            ),
            "paper_portfolio_rejection_evidence": (
                paper_portfolio_rejection_evidence
            ),
            "paper_portfolio_decision_audit": (
                paper_portfolio_decision_audit
            ),
            "paper_portfolio_fill_decision_audit": (
                paper_portfolio_fill_decision_audit
            ),
            "paper_pending_continuity": paper_pending_continuity,
            "forward_warmup_structure_lineage": (
                forward_warmup_structure_lineage
            ),
            "paper_accounting": paper_accounting,
            "paper_valuation": paper_valuation,
            "paper_capital_controls": {
                "cash_and_slot_pretrade_enforced": True,
                "portfolio_rejected_intent_count": len(
                    human_paper_portfolio_rejected_intent_ids(
                        tuple(paper_ledger["events"])
                    )
                ),
                "slot_fraction_notional_gate_evaluable": True,
                "account_exposure_notional_gate_evaluable": True,
                "synchronous_open_position_one_minute_marks_required": True,
                "unresolved_position_marks_block_new_buys": True,
                "exact_qmt_sector_admission_required_before_virtual_buy_fill": True,
                "entry_selection_blocked_buy_intent_count": (
                    paper_entry_selection_settlement_gate[
                        "blocked_pending_buy_intent_count"
                    ]
                ),
                "entry_selection_pending_buys_fully_verified": (
                    paper_entry_selection_settlement_gate["status"]
                    in {"READY", "NO_PENDING_BUYS"}
                ),
                "portfolio_approved_fill_ledger_prefix_recomputed": True,
                "one_security_one_strategic_slot_enforced": True,
                "terminal_signal_lifecycle_one_shot_enforced": True,
                "fixed_one_lot_tactical_review_only": True,
                "fixed_one_lot_diagnostic": True,
                "strategic_buy_confirmation_bar_price_cap_enforced": True,
                "strategic_buy_entire_bar_strict_cross_enforced": True,
                "strategic_buy_five_percent_bar_volume_cap_enforced": True,
                "persistent_sell_five_percent_bar_volume_cap_enforced": True,
                "adverse_observed_bar_extreme_fill_price_enforced": True,
                "completed_bar_close_fill_timestamp_enforced": True,
                "strategic_buy_one_locator_bar_ttl_enforced": True,
                "strategic_buy_causal_full_1m_window_prechecked": True,
                "full_session_240_bar_grid_required": True,
                "opening_auction_event_merged_into_0931": True,
                "optional_buy_data_fault_cancelled": True,
                "optional_buy_security_gate_cancelled": True,
                "execution_fact_incomplete_optional_buy_cancelled": True,
                "operations_cancellation_exact_evidence_audited": True,
                "persistent_exit_independent_symbol_continues": True,
                "persistent_exit_security_blocked_remains_pending": True,
                "persistent_exit_fact_incomplete_remains_pending": True,
                "fill_and_rejection_full_session_grid_audited": (
                    paper_execution_evidence.get("status")
                    in {"COMPLETE", "NO_FILLS"}
                    and paper_execution_rejection_evidence.get("status")
                    in {"COMPLETE", "NO_REJECTIONS"}
                    and paper_portfolio_rejection_evidence.get("status")
                    in {"COMPLETE", "NO_REJECTIONS"}
                    and paper_operations_cancellation_evidence.get("status")
                    in {"COMPLETE", "NO_CANCELLATIONS"}
                ),
                "pending_continuity_requires_gap_free_240_bar_grid": True,
                "current_pending_continuity_proven": (
                    paper_pending_continuity.get("status")
                    in {"COMPLETE", "NO_PENDING_INTENTS"}
                ),
                "raw_1m_entry_boundary_self_contained": (
                    paper_entry_boundary_attestation.get("status")
                    in {"COMPLETE", "NO_BOUNDARY_INTENTS"}
                ),
                "structure_anchor_never_used_as_execution_cap": True,
                "execution_rejection_exact_1m_evidence_audited": True,
                "strategic_exit_persistent_until_fill": True,
                "broker_transport_available": False,
                "live_status": "LIVE_DISABLED",
            },
            "real_account_access": False,
            "real_order_transport": False,
            "live_status": "LIVE_DISABLED",
        }
    )
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    args = parser().parse_args(argv)
    handlers = {
        "start": _start,
        "capture": _capture,
        "evaluate": _evaluate,
        "status": _status,
    }
    if args.command != "status":
        # 采集或评估读取任何市场事实前先冻结可执行程序/来源身份。长命令运行期间修改文件时，
        # 终态事件不能声称进程从未执行的代码。每次命令行调用都是新进程；状态命令保持只读，
        # 无需来源预检。
        _implementation_provenance()
    return handlers[args.command](args)


if __name__ == "__main__":
    raise SystemExit(main())
