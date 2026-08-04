"""Crash-isolated process boundary for native QMT screening reads.

The xtquant client contains native code which can terminate the interpreter
without raising a Python exception.  The live screening worker therefore runs
all of its QMT/structure reads in a persistent child process.  This module owns
the authenticated loopback IPC transport and exposes the same read-only gateway
protocol used by :mod:`trading_screening`.

No account object, trader session or order transport is accepted by this
boundary.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
import hashlib
import json
from multiprocessing.connection import Connection, Listener
import os
from pathlib import Path
from queue import Empty, Queue
import secrets
import subprocess
import sys
from threading import Lock, RLock, Thread
import time
from uuid import uuid4
from zoneinfo import ZoneInfo

from chanlun.decision_support.fingerprints import normalize_datetime, sha256_json
from chanlun.decision_support.trading_system.engine import SymbolStructureBundle
from chanlun.decision_support.trading_system.incremental_scan import BarKey
from chanlun.decision_support.trading_system.decision_source_provenance import (
    is_content_addressed_application_source_revision,
)
from chanlun.decision_support.trading_system.sector_strength import (
    sector_strength_batch_from_evidence_document,
)
from chanlun.decision_support.trading_system.models import (
    SectorAssessment,
    TimeframeContext,
)
from chanlun.decision_support.trading_system.v3_trading_session import (
    build_trading_session_evidence,
    validate_trading_session_evidence,
)
from cl_app.services.trading_screening_gateway import (
    SectorAnalysisExclusion,
    SectorAnalysisFailure,
    SectorAssessmentBatch,
    _KNOWN_SCREENING_INSTRUMENT_TYPES,
    _stock_codes,
)


IPC_SCHEMA = "chanlun-trading-screening-native-ipc/v1"
IPC_AUTHKEY_ENV = "CHANLUN_SCREENING_WORKER_AUTHKEY"
_CN = ZoneInfo("Asia/Shanghai")
_PROJECT_ROOT = Path(__file__).resolve().parents[4]
_DEFAULT_WORKER = Path(__file__).with_name("trading_screening_native_worker.py")
_SECTOR_CACHE_SCHEMA = "chanlun-native-sector-snapshot-cache/v2"
_SECTOR_CACHE_PAYLOAD_SCHEMA = "chanlun-native-sector-snapshot-cache-payload/v2"
_SECTOR_SNAPSHOT_PRODUCER_SCHEMA = (
    "chanlun-native-sector-snapshot-producer/v1"
)
_SECTOR_SNAPSHOT_WEB_PRODUCERS = (
    "web/chanlun_chart/cl_app/services/trading_screening_gateway.py",
    "web/chanlun_chart/cl_app/services/trading_screening_native_worker.py",
    "web/chanlun_chart/cl_app/services/trading_screening_process.py",
)


def _sector_cache_decision_epoch(value: datetime) -> tuple[date, str, int]:
    """Map a wall-clock request to its causal A-share 5m data epoch.

    A sector snapshot is expensive but its completed market-data prefix does
    not change every wall-clock minute.  Exact timestamp matching made a cache
    written at 20:58 immediately unusable at 20:59 and trapped the background
    scanner in a permanent sector-rebuild loop.  Reuse is safe only inside a
    window in which no new completed 5m bar can appear.  Weekday holidays are
    intentionally treated like trading days: this may recompute unnecessarily
    but can never carry a snapshot across a possible new bar.
    """

    local = normalize_datetime(value, "sector cache decision time").astimezone(
        _CN
    )
    minute = local.hour * 60 + local.minute
    if local.weekday() >= 5:
        return local.date(), "NON_TRADING_WEEKEND", 0
    if minute < 9 * 60 + 30:
        return local.date(), "PREOPEN", 0
    if minute < 11 * 60 + 30:
        return local.date(), "MORNING", (minute - (9 * 60 + 30)) // 5
    if minute < 13 * 60:
        return local.date(), "LUNCH", 0
    if minute < 15 * 60:
        return local.date(), "AFTERNOON", (minute - 13 * 60) // 5
    return local.date(), "POSTCLOSE", 0


def native_sector_snapshot_producer_revision(
    *,
    project_root: Path | str | None = None,
) -> str:
    """Return the complete, UI-independent native-sector producer identity.

    The persisted native snapshot contains QMT catalog, composite structure,
    constituent-strength and cache-codec output.  Its identity therefore
    covers every runtime file under ``src`` plus the three Web service modules
    that assemble, transport and authenticate that output.  Templates,
    JavaScript, CSS, unrelated Web routes and deployment scripts cannot change
    those facts and deliberately do not invalidate an expensive sector replay.

    This is intentionally broader than a hand-maintained Python import list:
    bundled QMT binaries/configuration and newly introduced decision helpers
    are picked up automatically.  Runtime bytecode/cache directories are not
    source and are excluded.
    """

    root = (
        _PROJECT_ROOT
        if project_root is None
        else Path(project_root).resolve()
    )
    source_root = root / "src"
    required = tuple(root / value for value in _SECTOR_SNAPSHOT_WEB_PRODUCERS)
    if not source_root.is_dir() or any(not value.is_file() for value in required):
        raise RuntimeError("native sector snapshot producer source is incomplete")
    ignored_directories = frozenset(
        {"__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache"}
    )
    paths = {
        value.resolve()
        for value in source_root.rglob("*")
        if value.is_file()
        and not any(part in ignored_directories for part in value.parts)
        and value.suffix.lower() not in {".pyc", ".pyo"}
    }
    paths.update(value.resolve() for value in required)
    manifest = tuple(
        {
            "path": value.relative_to(root).as_posix(),
            "sha256": "sha256:"
            + hashlib.sha256(value.read_bytes()).hexdigest(),
        }
        for value in sorted(paths, key=lambda item: item.relative_to(root).as_posix())
    )
    return sha256_json(
        {
            "schema": _SECTOR_SNAPSHOT_PRODUCER_SCHEMA,
            "files": manifest,
            "minimum_market_data_frequency": "1m",
            "tick_data_used": False,
            "real_account_access": False,
            "real_order_transport": False,
        }
    )


def native_sector_snapshot_cache_revision(
    build_revision: str,
    *,
    project_root: Path | str | None = None,
) -> str | None:
    """Enable cache for a legacy official run or exact content-addressed tree."""

    if not isinstance(build_revision, str):
        raise TypeError("build_revision must be a string")
    runtime_revision = build_revision.strip()
    if (
        ".run." not in runtime_revision
        and not is_content_addressed_application_source_revision(runtime_revision)
    ):
        return None
    return native_sector_snapshot_producer_revision(project_root=project_root)


class NativeScreeningWorkerError(RuntimeError):
    """Base class for the isolated native screening boundary."""


class NativeScreeningWorkerUnavailable(NativeScreeningWorkerError):
    """The child process is dead or still inside its restart backoff."""


class NativeScreeningWorkerTimeout(NativeScreeningWorkerError):
    """No progress arrived before the native-call idle deadline."""


class NativeScreeningWorkerProtocolError(NativeScreeningWorkerError):
    """The authenticated child returned an invalid protocol message."""


class NativeScreeningWorkerRemoteError(NativeScreeningWorkerError):
    """A normal Python exception was raised inside the healthy child."""

    def __init__(
        self,
        *,
        method: str,
        remote_error_type: str,
        remote_message: str,
    ) -> None:
        self.method = method
        self.remote_error_type = remote_error_type
        self.remote_message = remote_message
        super().__init__(
            f"native worker {method} failed: "
            f"{remote_error_type}: {remote_message}"
        )


@dataclass(frozen=True, slots=True)
class NativeWorkerProcessConfig:
    startup_timeout_seconds: float = 45.0
    native_idle_timeout_seconds: float = 210.0
    restart_backoff_seconds: float = 30.0

    def __post_init__(self) -> None:
        if any(
            isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0
            for value in (
                self.startup_timeout_seconds,
                self.native_idle_timeout_seconds,
                self.restart_backoff_seconds,
            )
        ):
            raise ValueError("native worker timeouts must be positive numbers")


def _now() -> datetime:
    return datetime.now(_CN)


def _iso(value: datetime | None) -> str | None:
    return None if value is None else value.isoformat()


@dataclass(frozen=True, slots=True)
class _SectorSnapshotComponents:
    batch: SectorAssessmentBatch
    members: dict[str, tuple[str, ...]]
    changed_bars: tuple[BarKey, ...]
    symbol_names: dict[str, str]


class _SectorSnapshotCacheError(ValueError):
    def __init__(self, reason_code: str, detail: str) -> None:
        self.reason_code = reason_code
        super().__init__(detail)


def _cache_mapping(value: object, field_name: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping) or any(
        not isinstance(key, str) for key in value
    ):
        raise ValueError(f"{field_name} must be a string-keyed mapping")
    return value


def _cache_sequence(value: object, field_name: str) -> Sequence[object]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise ValueError(f"{field_name} must be a sequence")
    return value


def _cache_string(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{field_name} must be a non-empty string")
    return value


def _cache_optional_string(value: object, field_name: str) -> str | None:
    if value is None:
        return None
    return _cache_string(value, field_name)


def _cache_bool(value: object, field_name: str) -> bool:
    if type(value) is not bool:
        raise ValueError(f"{field_name} must be a boolean")
    return value


def _cache_int(value: object, field_name: str, *, minimum: int = 0) -> int:
    if type(value) is not int or value < minimum:
        raise ValueError(f"{field_name} must be an integer >= {minimum}")
    return value


def _cache_datetime(value: object, field_name: str) -> datetime:
    raw = _cache_string(value, field_name)
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError as exc:
        raise ValueError(f"{field_name} must be an ISO datetime") from exc
    return normalize_datetime(parsed, field_name)


def _cache_date(value: object, field_name: str) -> date:
    raw = _cache_string(value, field_name)
    try:
        return date.fromisoformat(raw)
    except ValueError as exc:
        raise ValueError(f"{field_name} must be an ISO date") from exc


def _cache_strings(value: object, field_name: str) -> tuple[str, ...]:
    values = _cache_sequence(value, field_name)
    result = tuple(
        _cache_string(item, f"{field_name}[]") for item in values
    )
    if len(result) != len(set(result)):
        raise ValueError(f"{field_name} values must be unique")
    return result


def _context_cache_document(value: TimeframeContext | None) -> object:
    if value is None:
        return None
    return {
        "frequency": value.frequency,
        "direction": value.direction,
        "disposition": value.disposition,
        "hard_block": value.hard_block,
        "dominant_point_id": value.dominant_point_id,
        "dominant_point_type": value.dominant_point_type,
        "reason_codes": list(value.reason_codes),
        "observed_at": value.observed_at.isoformat(),
    }


def _context_from_cache(value: object, field_name: str) -> TimeframeContext | None:
    if value is None:
        return None
    row = _cache_mapping(value, field_name)
    direction = _cache_string(row.get("direction"), f"{field_name}.direction")
    disposition = _cache_string(
        row.get("disposition"), f"{field_name}.disposition"
    )
    point_type = _cache_optional_string(
        row.get("dominant_point_type"), f"{field_name}.dominant_point_type"
    )
    if direction not in {"up", "down", "neutral"}:
        raise ValueError(f"{field_name}.direction is unsupported")
    if disposition not in {"supportive", "neutral", "hostile"}:
        raise ValueError(f"{field_name}.disposition is unsupported")
    if point_type not in {None, "1buy", "2buy", "3buy", "1sell", "2sell", "3sell"}:
        raise ValueError(f"{field_name}.dominant_point_type is unsupported")
    return TimeframeContext(
        frequency=_cache_string(
            row.get("frequency"), f"{field_name}.frequency"
        ),
        direction=direction,  # type: ignore[arg-type]
        disposition=disposition,  # type: ignore[arg-type]
        hard_block=_cache_bool(
            row.get("hard_block"), f"{field_name}.hard_block"
        ),
        dominant_point_id=_cache_optional_string(
            row.get("dominant_point_id"), f"{field_name}.dominant_point_id"
        ),
        dominant_point_type=point_type,  # type: ignore[arg-type]
        reason_codes=_cache_strings(
            row.get("reason_codes"), f"{field_name}.reason_codes"
        ),
        observed_at=_cache_datetime(
            row.get("observed_at"), f"{field_name}.observed_at"
        ),
    )


def _assessment_cache_document(value: SectorAssessment) -> dict[str, object]:
    return {
        "sector_id": value.sector_id,
        "sector_name": value.sector_name,
        "eligible": value.eligible,
        "hard_block": value.hard_block,
        "regime": value.regime,
        "rank_components": [list(item) for item in value.rank_components],
        "reason_codes": list(value.reason_codes),
        "thirty_context": _context_cache_document(value.thirty_context),
        "five_context": _context_cache_document(value.five_context),
        "one_context": _context_cache_document(value.one_context),
        "horizontal_strength": (
            None
            if value.horizontal_strength is None
            else str(value.horizontal_strength)
        ),
        "horizontal_rank": value.horizontal_rank,
        "strength_anchor_session": (
            None
            if value.strength_anchor_session is None
            else value.strength_anchor_session.isoformat()
        ),
        "strength_member_count": value.strength_member_count,
        "strength_source_revision": value.strength_source_revision,
        "strength_reason_codes": list(value.strength_reason_codes),
    }


def _rank_components_from_cache(
    value: object,
    field_name: str,
) -> tuple[tuple[str, int], ...]:
    result: list[tuple[str, int]] = []
    for index, item in enumerate(_cache_sequence(value, field_name)):
        pair = _cache_sequence(item, f"{field_name}[{index}]")
        if len(pair) != 2:
            raise ValueError(f"{field_name}[{index}] must have two values")
        result.append(
            (
                _cache_string(pair[0], f"{field_name}[{index}].name"),
                _cache_int(pair[1], f"{field_name}[{index}].value", minimum=-1000000),
            )
        )
    return tuple(result)


def _assessment_from_cache(value: object, field_name: str) -> SectorAssessment:
    row = _cache_mapping(value, field_name)
    regime = _cache_string(row.get("regime"), f"{field_name}.regime")
    if regime not in {"supportive", "neutral", "hostile"}:
        raise ValueError(f"{field_name}.regime is unsupported")
    raw_strength = row.get("horizontal_strength")
    try:
        strength = None if raw_strength is None else Decimal(
            _cache_string(raw_strength, f"{field_name}.horizontal_strength")
        )
    except ArithmeticError as exc:
        raise ValueError(f"{field_name}.horizontal_strength is invalid") from exc
    raw_rank = row.get("horizontal_rank")
    rank = None if raw_rank is None else _cache_int(
        raw_rank, f"{field_name}.horizontal_rank", minimum=1
    )
    raw_session = row.get("strength_anchor_session")
    anchor_session = None if raw_session is None else _cache_date(
        raw_session, f"{field_name}.strength_anchor_session"
    )
    return SectorAssessment(
        sector_id=_cache_string(row.get("sector_id"), f"{field_name}.sector_id"),
        sector_name=_cache_string(
            row.get("sector_name"), f"{field_name}.sector_name"
        ),
        eligible=_cache_bool(row.get("eligible"), f"{field_name}.eligible"),
        hard_block=_cache_bool(
            row.get("hard_block"), f"{field_name}.hard_block"
        ),
        regime=regime,  # type: ignore[arg-type]
        rank_components=_rank_components_from_cache(
            row.get("rank_components"), f"{field_name}.rank_components"
        ),
        reason_codes=_cache_strings(
            row.get("reason_codes"), f"{field_name}.reason_codes"
        ),
        thirty_context=_context_from_cache(
            row.get("thirty_context"), f"{field_name}.thirty_context"
        ),
        five_context=_context_from_cache(
            row.get("five_context"), f"{field_name}.five_context"
        ),
        one_context=_context_from_cache(
            row.get("one_context"), f"{field_name}.one_context"
        ),
        horizontal_strength=strength,
        horizontal_rank=rank,
        strength_anchor_session=anchor_session,
        strength_member_count=_cache_int(
            row.get("strength_member_count"),
            f"{field_name}.strength_member_count",
        ),
        strength_source_revision=_cache_optional_string(
            row.get("strength_source_revision"),
            f"{field_name}.strength_source_revision",
        ),
        strength_reason_codes=_cache_strings(
            row.get("strength_reason_codes"),
            f"{field_name}.strength_reason_codes",
        ),
    )


def _failure_cache_document(value: SectorAnalysisFailure) -> dict[str, object]:
    return {
        "sector_id": value.sector_id,
        "code": value.code,
        "error_type": value.error_type,
        "reason": value.reason,
        "detail_code": value.detail_code,
        "catalog_member_count": value.catalog_member_count,
        "universe_member_count": value.universe_member_count,
    }


def _failure_from_cache(value: object, field_name: str) -> SectorAnalysisFailure:
    row = _cache_mapping(value, field_name)

    def optional_count(name: str) -> int | None:
        raw = row.get(name)
        return None if raw is None else _cache_int(raw, f"{field_name}.{name}")

    return SectorAnalysisFailure(
        sector_id=_cache_string(row.get("sector_id"), f"{field_name}.sector_id"),
        code=_cache_string(row.get("code"), f"{field_name}.code"),
        error_type=_cache_string(
            row.get("error_type"), f"{field_name}.error_type"
        ),
        reason=_cache_string(row.get("reason"), f"{field_name}.reason"),
        detail_code=_cache_optional_string(
            row.get("detail_code"), f"{field_name}.detail_code"
        ),
        catalog_member_count=optional_count("catalog_member_count"),
        universe_member_count=optional_count("universe_member_count"),
    )


def _exclusion_cache_document(
    value: SectorAnalysisExclusion,
) -> dict[str, object]:
    return {
        "sector_id": value.sector_id,
        "code": value.code,
        "reason_code": value.reason_code,
        "reason": value.reason,
        "detail_code": value.detail_code,
        "catalog_member_count": value.catalog_member_count,
        "universe_member_count": value.universe_member_count,
        "required_member_count": value.required_member_count,
    }


def _exclusion_from_cache(
    value: object,
    field_name: str,
) -> SectorAnalysisExclusion:
    row = _cache_mapping(value, field_name)
    return SectorAnalysisExclusion(
        sector_id=_cache_string(row.get("sector_id"), f"{field_name}.sector_id"),
        code=_cache_string(row.get("code"), f"{field_name}.code"),
        reason_code=_cache_string(
            row.get("reason_code"), f"{field_name}.reason_code"
        ),
        reason=_cache_string(row.get("reason"), f"{field_name}.reason"),
        detail_code=_cache_string(
            row.get("detail_code"), f"{field_name}.detail_code"
        ),
        catalog_member_count=_cache_int(
            row.get("catalog_member_count"),
            f"{field_name}.catalog_member_count",
        ),
        universe_member_count=_cache_int(
            row.get("universe_member_count"),
            f"{field_name}.universe_member_count",
        ),
        required_member_count=_cache_int(
            row.get("required_member_count"),
            f"{field_name}.required_member_count",
            minimum=1,
        ),
    )


def _batch_cache_document(value: SectorAssessmentBatch) -> dict[str, object]:
    return {
        "assessments": [
            _assessment_cache_document(item) for item in value.assessments
        ],
        "discovered_count": value.discovered_count,
        "completed_count": value.completed_count,
        "failure_counts": [list(item) for item in value.failure_counts],
        "errors": [_failure_cache_document(item) for item in value.errors],
        "exclusion_counts": [list(item) for item in value.exclusion_counts],
        "exclusions": [
            _exclusion_cache_document(item) for item in value.exclusions
        ],
        # This is the independently recomputed QMT catalog identity carried by
        # the native gateway.  Dropping it on cache round-trip makes an ordinary
        # same-build Web restart silently replace it with the service's weaker
        # fallback membership hash and therefore changes the coverage epoch.
        "catalog_revision": value.catalog_revision,
        "strength_evidence": (
            None
            if value.strength_evidence is None
            else value.strength_evidence.evidence_document()
        ),
        "strength_evidence_revision": (
            None
            if value.strength_evidence is None
            else value.strength_evidence.evidence_revision
        ),
    }


def _batch_from_cache(value: object) -> SectorAssessmentBatch:
    row = _cache_mapping(value, "payload.snapshot.assessments")
    raw_strength_evidence = row.get("strength_evidence")
    if raw_strength_evidence is None:
        strength_evidence = None
        if row.get("strength_evidence_revision") is not None:
            raise ValueError("sector strength evidence revision has no document")
    else:
        strength_evidence = sector_strength_batch_from_evidence_document(
            raw_strength_evidence
        )
        if row.get("strength_evidence_revision") != strength_evidence.evidence_revision:
            raise ValueError("sector strength evidence revision is inconsistent")
    raw_counts = _cache_sequence(
        row.get("failure_counts"), "payload.snapshot.assessments.failure_counts"
    )
    failure_counts: list[tuple[str, int]] = []
    for index, item in enumerate(raw_counts):
        pair = _cache_sequence(
            item, f"payload.snapshot.assessments.failure_counts[{index}]"
        )
        if len(pair) != 2:
            raise ValueError("sector failure-count rows must have two values")
        failure_counts.append(
            (
                _cache_string(pair[0], "sector failure-count code"),
                _cache_int(pair[1], "sector failure-count value", minimum=1),
            )
        )
    raw_exclusion_counts = _cache_sequence(
        row.get("exclusion_counts"),
        "payload.snapshot.assessments.exclusion_counts",
    )
    exclusion_counts: list[tuple[str, int]] = []
    for index, item in enumerate(raw_exclusion_counts):
        pair = _cache_sequence(
            item,
            f"payload.snapshot.assessments.exclusion_counts[{index}]",
        )
        if len(pair) != 2:
            raise ValueError("sector exclusion-count rows must have two values")
        exclusion_counts.append(
            (
                _cache_string(pair[0], "sector exclusion-count code"),
                _cache_int(
                    pair[1], "sector exclusion-count value", minimum=1
                ),
            )
        )
    return SectorAssessmentBatch(
        assessments=tuple(
            _assessment_from_cache(item, f"assessment[{index}]")
            for index, item in enumerate(
                _cache_sequence(row.get("assessments"), "sector assessments")
            )
        ),
        discovered_count=_cache_int(
            row.get("discovered_count"), "sector discovered_count"
        ),
        completed_count=_cache_int(
            row.get("completed_count"), "sector completed_count"
        ),
        failure_counts=tuple(failure_counts),
        errors=tuple(
            _failure_from_cache(item, f"sector error[{index}]")
            for index, item in enumerate(
                _cache_sequence(row.get("errors"), "sector errors")
            )
        ),
        exclusion_counts=tuple(exclusion_counts),
        exclusions=tuple(
            _exclusion_from_cache(item, f"sector exclusion[{index}]")
            for index, item in enumerate(
                _cache_sequence(row.get("exclusions"), "sector exclusions")
            )
        ),
        catalog_revision=_cache_optional_string(
            row.get("catalog_revision"),
            "payload.snapshot.assessments.catalog_revision",
        ),
        strength_evidence=strength_evidence,
    )


def _bar_cache_document(value: BarKey) -> dict[str, object]:
    return {
        "code": value.code,
        "frequency": value.frequency,
        "closed_at": value.closed_at.isoformat(),
    }


def _bar_from_cache(value: object, field_name: str) -> BarKey:
    row = _cache_mapping(value, field_name)
    return BarKey(
        code=_cache_string(row.get("code"), f"{field_name}.code"),
        frequency=_cache_string(  # type: ignore[arg-type]
            row.get("frequency"), f"{field_name}.frequency"
        ),
        closed_at=_cache_datetime(
            row.get("closed_at"), f"{field_name}.closed_at"
        ),
    )


class NativeWorkerProcessTransport:
    """Persistent authenticated IPC client with idle timeout and crash recovery."""

    def __init__(
        self,
        *,
        log_path: Path,
        config: NativeWorkerProcessConfig = NativeWorkerProcessConfig(),
        worker_command: Sequence[str] | None = None,
        environment: Mapping[str, str] | None = None,
        progress_callback: Callable[[], None] = lambda: None,
    ) -> None:
        if worker_command is not None and (
            isinstance(worker_command, (str, bytes)) or not worker_command
        ):
            raise ValueError("worker_command must be a non-empty argument sequence")
        if not callable(progress_callback):
            raise TypeError("progress_callback must be callable")
        self._log_path = log_path.resolve()
        self._config = config
        self._worker_command = (
            None if worker_command is None else tuple(str(value) for value in worker_command)
        )
        self._environment = None if environment is None else dict(environment)
        self._progress_callback = progress_callback
        self._request_lock = Lock()
        self._state_lock = RLock()
        self._process: subprocess.Popen[bytes] | None = None
        self._connection: Connection | None = None
        self._worker_pid: int | None = None
        self._started_at: datetime | None = None
        self._request_started_at: datetime | None = None
        self._last_progress_at: datetime | None = None
        self._last_response_at: datetime | None = None
        self._last_method: str | None = None
        self._last_error: str | None = None
        self._last_remote_error: str | None = None
        self._last_failure_monotonic: float | None = None
        self._restart_count = 0
        self._failure_count = 0
        self._in_flight_request_id: str | None = None

    def set_progress_callback(self, callback: Callable[[], None]) -> None:
        if not callable(callback):
            raise TypeError("progress callback must be callable")
        with self._state_lock:
            self._progress_callback = callback

    def _base_command(self) -> tuple[str, ...]:
        if self._worker_command is not None:
            return self._worker_command
        return (sys.executable, str(_DEFAULT_WORKER))

    def _accept_connection(
        self,
        listener: Listener,
        output: Queue[tuple[Connection | None, BaseException | None]],
    ) -> None:
        try:
            output.put((listener.accept(), None))
        except BaseException as exc:  # pragma: no cover - platform shutdown race
            output.put((None, exc))

    def _spawn(self) -> None:
        with self._state_lock:
            process = self._process
            connection = self._connection
            if (
                process is not None
                and process.poll() is None
                and connection is not None
                and not connection.closed
            ):
                return
            last_failure = self._last_failure_monotonic
            if last_failure is not None:
                remaining = (
                    self._config.restart_backoff_seconds
                    - (time.monotonic() - last_failure)
                )
                if remaining > 0:
                    raise NativeScreeningWorkerUnavailable(
                        f"native worker restart backoff active ({remaining:.1f}s)"
                    )

        authkey = secrets.token_bytes(32)
        listener = Listener(("127.0.0.1", 0), authkey=authkey)
        host, port = listener.address
        accepted: Queue[tuple[Connection | None, BaseException | None]] = Queue(maxsize=1)
        Thread(
            target=self._accept_connection,
            args=(listener, accepted),
            name="TradingScreeningNativeAccept",
            daemon=True,
        ).start()
        self._log_path.parent.mkdir(parents=True, exist_ok=True)
        command = (
            *self._base_command(),
            "--host",
            str(host),
            "--port",
            str(port),
        )
        environment = os.environ.copy()
        if self._environment is not None:
            environment.update(self._environment)
        environment[IPC_AUTHKEY_ENV] = authkey.hex()
        log_handle = self._log_path.open("ab", buffering=0)
        try:
            process = subprocess.Popen(
                command,
                cwd=_PROJECT_ROOT,
                env=environment,
                stdin=subprocess.DEVNULL,
                stdout=log_handle,
                stderr=subprocess.STDOUT,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except BaseException:
            listener.close()
            log_handle.close()
            raise
        finally:
            # Popen duplicates the handle for the child on Windows.
            log_handle.close()

        deadline = time.monotonic() + self._config.startup_timeout_seconds
        connection: Connection | None = None
        failure: BaseException | None = None
        try:
            while time.monotonic() < deadline:
                try:
                    connection, failure = accepted.get(timeout=0.1)
                    break
                except Empty:
                    if process.poll() is not None:
                        failure = RuntimeError(
                            f"worker exited during startup with code {process.returncode}"
                        )
                        break
            if connection is None:
                raise NativeScreeningWorkerUnavailable(
                    f"native worker failed to connect: {failure or 'startup timeout'}"
                )
            while time.monotonic() < deadline and not connection.poll(0.1):
                if process.poll() is not None:
                    raise NativeScreeningWorkerUnavailable(
                        f"native worker exited before handshake ({process.returncode})"
                    )
            if not connection.poll(0):
                raise NativeScreeningWorkerUnavailable("native worker handshake timeout")
            handshake = connection.recv()
            if not isinstance(handshake, Mapping) or (
                handshake.get("schema") != IPC_SCHEMA
                or handshake.get("type") != "ready"
                or type(handshake.get("pid")) is not int
                or handshake.get("real_account_access") is not False
                or handshake.get("real_order_transport") is not False
            ):
                raise NativeScreeningWorkerProtocolError(
                    "native worker returned an invalid safety handshake"
                )
        except BaseException as exc:
            try:
                if connection is not None:
                    connection.close()
            finally:
                self._stop_process(process)
                self._record_failure(f"{type(exc).__name__}: {exc}")
            raise
        finally:
            listener.close()

        started = _now()
        with self._state_lock:
            self._process = process
            self._connection = connection
            self._worker_pid = int(handshake["pid"])
            self._started_at = started
            self._last_progress_at = started
            self._last_response_at = started
            self._last_error = None
            self._last_remote_error = None
            self._last_failure_monotonic = None
            self._restart_count += 1

    @staticmethod
    def _stop_process(process: subprocess.Popen[bytes] | None) -> None:
        if process is None or process.poll() is not None:
            return
        process.terminate()
        try:
            process.wait(timeout=1.0)
        except subprocess.TimeoutExpired:
            process.kill()
            try:
                process.wait(timeout=1.0)
            except subprocess.TimeoutExpired:  # pragma: no cover - OS failure
                pass

    def _record_failure(self, message: str) -> None:
        with self._state_lock:
            self._last_error = message[:500]
            self._last_failure_monotonic = time.monotonic()
            self._failure_count += 1

    def _discard_worker(self, message: str, *, record_failure: bool = True) -> None:
        with self._state_lock:
            connection = self._connection
            process = self._process
            self._connection = None
            self._process = None
            self._worker_pid = None
            self._in_flight_request_id = None
            self._request_started_at = None
        try:
            if connection is not None:
                connection.close()
        finally:
            self._stop_process(process)
        if record_failure:
            self._record_failure(message)

    def _notify_progress(self) -> None:
        callback = self._progress_callback
        try:
            callback()
        except Exception:
            # Operational telemetry must not corrupt a valid market-data reply.
            pass

    def request(self, method: str, **kwargs: object) -> object:
        if not isinstance(method, str) or not method:
            raise ValueError("native worker method is required")
        with self._request_lock:
            self._spawn()
            request_id = "sha256:" + uuid4().hex + uuid4().hex
            started = _now()
            with self._state_lock:
                process = self._process
                connection = self._connection
                self._in_flight_request_id = request_id
                self._request_started_at = started
                self._last_progress_at = started
                self._last_method = method
            if process is None or connection is None:
                raise NativeScreeningWorkerUnavailable("native worker is unavailable")
            try:
                connection.send(
                    {
                        "schema": IPC_SCHEMA,
                        "type": "request",
                        "request_id": request_id,
                        "method": method,
                        "kwargs": dict(kwargs),
                    }
                )
                idle_deadline = (
                    time.monotonic() + self._config.native_idle_timeout_seconds
                )
                while True:
                    if process.poll() is not None:
                        raise NativeScreeningWorkerUnavailable(
                            "native worker exited during request "
                            f"{method} with code {process.returncode}"
                        )
                    remaining = idle_deadline - time.monotonic()
                    if remaining <= 0:
                        raise NativeScreeningWorkerTimeout(
                            f"native worker made no progress for "
                            f"{self._config.native_idle_timeout_seconds:g}s in {method}"
                        )
                    if not connection.poll(min(0.2, remaining)):
                        continue
                    response = connection.recv()
                    if not isinstance(response, Mapping) or (
                        response.get("schema") != IPC_SCHEMA
                        or response.get("request_id") != request_id
                    ):
                        raise NativeScreeningWorkerProtocolError(
                            "native worker response identity is invalid"
                        )
                    response_type = response.get("type")
                    if response_type == "progress":
                        progressed = _now()
                        with self._state_lock:
                            self._last_progress_at = progressed
                        idle_deadline = (
                            time.monotonic()
                            + self._config.native_idle_timeout_seconds
                        )
                        self._notify_progress()
                        continue
                    if response_type == "error":
                        name = str(response.get("error_type") or "RemoteError")
                        message = str(response.get("message") or "")[:400]
                        with self._state_lock:
                            self._last_response_at = _now()
                            self._last_remote_error = f"{name}: {message}"
                        raise NativeScreeningWorkerRemoteError(
                            method=method,
                            remote_error_type=name,
                            remote_message=message,
                        )
                    if response_type != "result" or "value" not in response:
                        raise NativeScreeningWorkerProtocolError(
                            "native worker returned an unsupported response"
                        )
                    with self._state_lock:
                        self._last_response_at = _now()
                        self._last_remote_error = None
                    return response["value"]
            except NativeScreeningWorkerRemoteError:
                raise
            except (EOFError, BrokenPipeError, OSError) as exc:
                message = f"native worker transport failed in {method}: {exc}"
                self._discard_worker(message)
                raise NativeScreeningWorkerUnavailable(message) from exc
            except NativeScreeningWorkerError as exc:
                self._discard_worker(f"{type(exc).__name__}: {exc}")
                raise
            finally:
                with self._state_lock:
                    if self._in_flight_request_id == request_id:
                        self._in_flight_request_id = None
                        self._request_started_at = None

    def shutdown(self) -> None:
        acquired = self._request_lock.acquire(timeout=1.0)
        try:
            with self._state_lock:
                connection = self._connection
                request_id = self._in_flight_request_id
            if acquired and connection is not None and request_id is None:
                try:
                    connection.send(
                        {
                            "schema": IPC_SCHEMA,
                            "type": "shutdown",
                            "request_id": "shutdown:" + uuid4().hex,
                        }
                    )
                except (EOFError, BrokenPipeError, OSError):
                    pass
            self._discard_worker("native worker stopped", record_failure=False)
            with self._state_lock:
                self._last_failure_monotonic = None
                self._last_error = None
        finally:
            if acquired:
                self._request_lock.release()

    close = shutdown

    def health_snapshot(self) -> dict[str, object]:
        with self._state_lock:
            process = self._process
            alive = process is not None and process.poll() is None
            backoff_remaining = 0.0
            if self._last_failure_monotonic is not None:
                backoff_remaining = max(
                    0.0,
                    self._config.restart_backoff_seconds
                    - (time.monotonic() - self._last_failure_monotonic),
                )
            ready = alive and self._connection is not None and self._last_error is None
            reasons: list[str] = []
            if not alive:
                reasons.append("native_screening_worker_not_running")
            if self._last_error is not None:
                reasons.append("native_screening_worker_failed")
            return {
                "schema": "chanlun-trading-screening-native-health/v1",
                "required": True,
                "ready": ready,
                "status": "ready" if ready else "not_ready",
                "isolated_process": True,
                "loopback_authenticated": True,
                "worker_pid": self._worker_pid,
                "worker_alive": alive,
                "started_at": _iso(self._started_at),
                "in_flight": self._in_flight_request_id is not None,
                "request_started_at": _iso(self._request_started_at),
                "last_method": self._last_method,
                "last_progress_at": _iso(self._last_progress_at),
                "last_response_at": _iso(self._last_response_at),
                "restart_count": self._restart_count,
                "failure_count": self._failure_count,
                "restart_backoff_remaining_seconds": round(backoff_remaining, 3),
                "last_error": self._last_error,
                "last_remote_error": self._last_remote_error,
                "minimum_market_data_frequency": "1m",
                "tick_data_used": False,
                "real_account_access": False,
                "real_order_transport": False,
                "reasons": reasons,
            }


class NativeTradingDataGatewayProcessProxy:
    """Typed read-only gateway backed by :class:`NativeWorkerProcessTransport`."""

    def __init__(
        self,
        *,
        watchlist_provider: Callable[[], object] = lambda: (),
        holdings_provider: Callable[[], object] = lambda: (),
        transport: NativeWorkerProcessTransport | None = None,
        log_path: Path | None = None,
        process_config: NativeWorkerProcessConfig = NativeWorkerProcessConfig(),
        sector_cache_path: Path | None = None,
        sector_cache_revision: str | None = None,
        worker_environment: Mapping[str, str] | None = None,
        structure_worker_count: int = 1,
    ) -> None:
        if not callable(watchlist_provider) or not callable(holdings_provider):
            raise TypeError("watchlist and holdings providers must be callable")
        if transport is None and log_path is None:
            raise ValueError("log_path is required when no transport is supplied")
        if (sector_cache_path is None) != (sector_cache_revision is None):
            raise ValueError(
                "sector_cache_path and sector_cache_revision must be supplied together"
            )
        if sector_cache_revision is not None and not sector_cache_revision.strip():
            raise ValueError("sector_cache_revision must be a non-empty string")
        if type(structure_worker_count) is not int or structure_worker_count <= 0:
            raise ValueError("structure_worker_count must be a positive integer")
        if transport is not None and structure_worker_count != 1:
            raise ValueError(
                "custom transport supports exactly one structure worker"
            )
        self._watchlist_provider = watchlist_provider
        self._holdings_provider = holdings_provider
        self._transport = transport or NativeWorkerProcessTransport(
            log_path=log_path,  # type: ignore[arg-type]
            config=process_config,
            environment=worker_environment,
        )
        structure_transports: list[NativeWorkerProcessTransport] = [
            self._transport
        ]
        if transport is None and structure_worker_count > 1:
            assert log_path is not None
            for index in range(1, structure_worker_count):
                worker_log = log_path.with_name(
                    f"{log_path.stem}.worker-{index + 1}{log_path.suffix}"
                )
                structure_transports.append(
                    NativeWorkerProcessTransport(
                        log_path=worker_log,
                        config=process_config,
                        environment=worker_environment,
                    )
                )
        self._structure_transports = tuple(structure_transports)
        self._cache_lock = RLock()
        self._sector_cache_path = sector_cache_path
        self._sector_cache_revision = sector_cache_revision
        self._sector_cache_state = (
            "disabled" if sector_cache_path is None else "not_checked"
        )
        self._sector_cache_reason: str | None = None
        self._sector_cache_requested_as_of: datetime | None = None
        self._sector_cache_content_sha256: str | None = None
        self._sector_members: dict[str, tuple[str, ...]] | None = None
        self._changed_bars: tuple[BarKey, ...] = ()
        self._emitted_bar_ids: set[tuple[str, str, datetime]] = set()
        self._symbol_names: dict[str, str] = {}
        self._trading_session_cache: dict[date, dict[str, object]] = {}

    def set_progress_callback(self, callback: Callable[[], None]) -> None:
        for transport in self._structure_transports:
            transport.set_progress_callback(callback)

    def _structure_transport(self, code: str) -> NativeWorkerProcessTransport:
        """Keep one symbol on one worker so its in-memory analysis cache survives."""

        digest = hashlib.sha256(code.encode("ascii", errors="strict")).digest()
        index = int.from_bytes(digest[:8], "big") % len(self._structure_transports)
        return self._structure_transports[index]

    def native_sector_assessments(self, *, as_of: datetime) -> SectorAssessmentBatch:
        observed_at = normalize_datetime(as_of, "as_of")
        cached = self._load_sector_snapshot_cache(observed_at)
        if cached is not None:
            self._install_sector_snapshot(cached)
            return cached.batch

        value = self._transport.request("sector_snapshot", as_of=as_of)
        components = self._validated_atomic_snapshot(value, observed_at)
        self._install_sector_snapshot(components)
        self._persist_sector_snapshot_cache(components, observed_at)
        return components.batch

    def _validated_atomic_snapshot(
        self,
        value: object,
        as_of: datetime,
    ) -> _SectorSnapshotComponents:
        if not isinstance(value, Mapping) or (
            value.get("schema") != "chanlun-native-sector-snapshot/v1"
        ):
            raise NativeScreeningWorkerProtocolError("invalid atomic sector snapshot")
        if (
            value.get("real_account_access") is not False
            or value.get("real_order_transport") is not False
            or value.get("tick_data_used") is not False
            or value.get("minimum_market_data_frequency") != "1m"
        ):
            raise NativeScreeningWorkerProtocolError(
                "atomic sector snapshot crossed the read-only safety boundary"
            )
        batch = value.get("assessments")
        members = self._validated_members(value.get("members"))
        bars = value.get("changed_bars")
        names = value.get("symbol_names")
        if not isinstance(batch, SectorAssessmentBatch):
            raise NativeScreeningWorkerProtocolError("invalid sector assessment batch")
        if not isinstance(bars, tuple) or any(
            not isinstance(item, BarKey) for item in bars
        ):
            raise NativeScreeningWorkerProtocolError("invalid sector changed bars")
        if not isinstance(names, Mapping) or any(
            not isinstance(code, str) or not isinstance(name, str)
            for code, name in names.items()
        ):
            raise NativeScreeningWorkerProtocolError("invalid sector symbol names")
        components = _SectorSnapshotComponents(
            batch=batch,
            members=members,
            changed_bars=bars,
            symbol_names=dict(names),
        )
        try:
            self._validate_sector_snapshot_causality(components, as_of)
        except ValueError as exc:
            raise NativeScreeningWorkerProtocolError(
                f"atomic sector snapshot violates causality: {exc}"
            ) from exc
        return components

    def _install_sector_snapshot(self, value: _SectorSnapshotComponents) -> None:
        with self._cache_lock:
            self._sector_members = dict(value.members)
            self._changed_bars = tuple(value.changed_bars)
            self._symbol_names = dict(value.symbol_names)

    @staticmethod
    def _validate_sector_snapshot_causality(
        value: _SectorSnapshotComponents,
        as_of: datetime,
    ) -> None:
        batch = value.batch
        assessment_ids = tuple(item.sector_id for item in batch.assessments)
        if assessment_ids != tuple(sorted(assessment_ids)):
            raise ValueError("sector assessments must be sorted")
        if len(assessment_ids) != len(set(assessment_ids)):
            raise ValueError("sector assessments must be unique")
        if len(assessment_ids) != batch.discovered_count:
            raise ValueError("sector assessment count must match discovered_count")
        if (
            len(batch.errors) + len(batch.exclusions)
            != batch.discovered_count - batch.completed_count
        ):
            raise ValueError(
                "sector errors and exclusions must explain every incomplete assessment"
            )
        error_ids = tuple(item.sector_id for item in batch.errors)
        if error_ids != tuple(sorted(error_ids)):
            raise ValueError("sector errors must be sorted")
        exclusion_ids = tuple(item.sector_id for item in batch.exclusions)
        if exclusion_ids != tuple(sorted(exclusion_ids)):
            raise ValueError("sector exclusions must be sorted")
        if (
            set(error_ids) & set(exclusion_ids)
            or not set(error_ids).issubset(assessment_ids)
            or not set(exclusion_ids).issubset(assessment_ids)
        ):
            raise ValueError("sector dispositions do not match assessments")
        if set(assessment_ids) - set(value.members):
            raise ValueError("every sector assessment must retain its member snapshot")

        for assessment in batch.assessments:
            contexts = (
                assessment.thirty_context,
                assessment.five_context,
                assessment.one_context,
            )
            if any(
                context is not None and context.observed_at > as_of
                for context in contexts
            ):
                raise ValueError("sector context is later than the decision time")
            if (
                assessment.strength_anchor_session is not None
                and assessment.strength_anchor_session > as_of.date()
            ):
                raise ValueError("sector strength anchor is later than the decision date")

        bar_ids = tuple(
            (item.code, item.frequency, item.closed_at)
            for item in value.changed_bars
        )
        if bar_ids != tuple(
            sorted(bar_ids, key=lambda item: (item[2], item[0], item[1]))
        ):
            raise ValueError("sector changed bars must be sorted")
        if len(bar_ids) != len(set(bar_ids)):
            raise ValueError("sector changed bars must be unique")
        if any(item.closed_at > as_of for item in value.changed_bars):
            raise ValueError("sector changed bar is later than the decision time")
        if any(item.code not in value.members for item in value.changed_bars):
            raise ValueError("sector changed bar has no membership snapshot")

    def _sector_snapshot_cache_document(
        self,
        value: _SectorSnapshotComponents,
        as_of: datetime,
    ) -> dict[str, object]:
        if self._sector_cache_revision is None:
            raise ValueError("sector snapshot cache is disabled")
        snapshot = {
            "schema": "chanlun-native-sector-snapshot/v1",
            "assessments": _batch_cache_document(value.batch),
            "members": {
                key: list(items) for key, items in sorted(value.members.items())
            },
            "changed_bars": [
                _bar_cache_document(item) for item in value.changed_bars
            ],
            "symbol_names": dict(sorted(value.symbol_names.items())),
            "minimum_market_data_frequency": "1m",
            "tick_data_used": False,
            "real_account_access": False,
            "real_order_transport": False,
        }
        payload: dict[str, object] = {
            "schema": _SECTOR_CACHE_PAYLOAD_SCHEMA,
            "source_revision": self._sector_cache_revision,
            "requested_as_of": as_of.isoformat(),
            "captured_at": _now().isoformat(),
            "snapshot": snapshot,
        }
        return {
            "schema": _SECTOR_CACHE_SCHEMA,
            "content_sha256": sha256_json(payload),
            "payload": payload,
        }

    def _components_from_cache_document(
        self,
        document: object,
        as_of: datetime,
    ) -> tuple[_SectorSnapshotComponents, str]:
        outer = _cache_mapping(document, "sector cache document")
        if outer.get("schema") != _SECTOR_CACHE_SCHEMA:
            raise _SectorSnapshotCacheError(
                "CACHE_SCHEMA_MISMATCH", "sector cache schema is unsupported"
            )
        payload = _cache_mapping(outer.get("payload"), "sector cache payload")
        expected_hash = _cache_string(
            outer.get("content_sha256"), "sector cache content_sha256"
        )
        if sha256_json(payload) != expected_hash:
            raise _SectorSnapshotCacheError(
                "CACHE_CONTENT_HASH_MISMATCH",
                "sector cache content hash does not match its payload",
            )
        if payload.get("schema") != _SECTOR_CACHE_PAYLOAD_SCHEMA:
            raise _SectorSnapshotCacheError(
                "CACHE_PAYLOAD_SCHEMA_MISMATCH",
                "sector cache payload schema is unsupported",
            )
        if payload.get("source_revision") != self._sector_cache_revision:
            raise _SectorSnapshotCacheError(
                "CACHE_SOURCE_REVISION_MISMATCH",
                "sector cache was produced by a different source revision",
            )
        cached_as_of = _cache_datetime(
            payload.get("requested_as_of"), "sector cache requested_as_of"
        )
        if _sector_cache_decision_epoch(cached_as_of) != (
            _sector_cache_decision_epoch(as_of)
        ):
            raise _SectorSnapshotCacheError(
                "CACHE_DECISION_TIME_MISMATCH",
                "sector cache belongs to a different causal market-data epoch",
            )
        _cache_datetime(payload.get("captured_at"), "sector cache captured_at")

        snapshot = _cache_mapping(payload.get("snapshot"), "sector cache snapshot")
        if snapshot.get("schema") != "chanlun-native-sector-snapshot/v1":
            raise _SectorSnapshotCacheError(
                "CACHE_ATOMIC_SCHEMA_MISMATCH",
                "cached atomic sector snapshot schema is unsupported",
            )
        if (
            snapshot.get("real_account_access") is not False
            or snapshot.get("real_order_transport") is not False
            or snapshot.get("tick_data_used") is not False
            or snapshot.get("minimum_market_data_frequency") != "1m"
        ):
            raise _SectorSnapshotCacheError(
                "CACHE_SAFETY_BOUNDARY_VIOLATION",
                "cached sector snapshot crossed the read-only safety boundary",
            )
        try:
            members = self._validated_members(snapshot.get("members"))
            raw_names = _cache_mapping(
                snapshot.get("symbol_names"), "cached sector symbol names"
            )
            names = {
                _cache_string(code, "cached sector symbol code"): _cache_string(
                    name, f"cached sector symbol name[{code}]"
                )
                for code, name in raw_names.items()
            }
            components = _SectorSnapshotComponents(
                batch=_batch_from_cache(snapshot.get("assessments")),
                members=members,
                changed_bars=tuple(
                    _bar_from_cache(item, f"cached changed bar[{index}]")
                    for index, item in enumerate(
                        _cache_sequence(
                            snapshot.get("changed_bars"),
                            "cached sector changed bars",
                        )
                    )
                ),
                symbol_names=names,
            )
            self._validate_sector_snapshot_causality(components, as_of)
        except _SectorSnapshotCacheError:
            raise
        except (NativeScreeningWorkerProtocolError, TypeError, ValueError) as exc:
            raise _SectorSnapshotCacheError(
                "CACHE_DOCUMENT_INVALID", str(exc)
            ) from exc
        return components, expected_hash

    def _set_sector_cache_status(
        self,
        *,
        state: str,
        reason: str | None,
        as_of: datetime | None,
        content_sha256: str | None,
    ) -> None:
        with self._cache_lock:
            self._sector_cache_state = state
            self._sector_cache_reason = reason
            self._sector_cache_requested_as_of = as_of
            self._sector_cache_content_sha256 = content_sha256

    def _load_sector_snapshot_cache(
        self,
        as_of: datetime,
    ) -> _SectorSnapshotComponents | None:
        path = self._sector_cache_path
        if path is None:
            return None
        try:
            document = json.loads(path.read_text(encoding="utf-8"))
            components, content_sha256 = self._components_from_cache_document(
                document, as_of
            )
        except FileNotFoundError:
            self._set_sector_cache_status(
                state="miss",
                reason="CACHE_FILE_MISSING",
                as_of=as_of,
                content_sha256=None,
            )
            return None
        except _SectorSnapshotCacheError as exc:
            self._set_sector_cache_status(
                state="rejected",
                reason=exc.reason_code,
                as_of=as_of,
                content_sha256=None,
            )
            return None
        except (OSError, TypeError, ValueError) as exc:
            self._set_sector_cache_status(
                state="rejected",
                reason=f"CACHE_READ_INVALID:{type(exc).__name__}",
                as_of=as_of,
                content_sha256=None,
            )
            return None
        self._set_sector_cache_status(
            state="hit",
            reason=None,
            as_of=as_of,
            content_sha256=content_sha256,
        )
        return components

    def _persist_sector_snapshot_cache(
        self,
        value: _SectorSnapshotComponents,
        as_of: datetime,
    ) -> None:
        path = self._sector_cache_path
        if path is None:
            return
        temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
        try:
            document = self._sector_snapshot_cache_document(value, as_of)
            path.parent.mkdir(parents=True, exist_ok=True)
            temporary.write_text(
                json.dumps(
                    document,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                    allow_nan=False,
                ),
                encoding="utf-8",
            )
            os.replace(temporary, path)
        except (OSError, TypeError, ValueError) as exc:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass
            self._set_sector_cache_status(
                state="write_failed",
                reason=f"CACHE_WRITE_FAILED:{type(exc).__name__}",
                as_of=as_of,
                content_sha256=None,
            )
            return
        self._set_sector_cache_status(
            state="refreshed",
            reason=None,
            as_of=as_of,
            content_sha256=_cache_string(
                document.get("content_sha256"), "sector cache content_sha256"
            ),
        )

    @staticmethod
    def _validated_members(value: object) -> dict[str, tuple[str, ...]]:
        if not isinstance(value, Mapping) or any(
            not isinstance(key, str)
            or isinstance(items, (str, bytes))
            or not isinstance(items, Sequence)
            or any(not isinstance(item, str) for item in items)
            for key, items in value.items()
        ):
            raise NativeScreeningWorkerProtocolError(
                "invalid sector membership result"
            )
        return {str(key): tuple(items) for key, items in value.items()}

    def members(self) -> Mapping[str, tuple[str, ...]]:
        with self._cache_lock:
            if self._sector_members is None:
                raise NativeScreeningWorkerUnavailable(
                    "atomic sector snapshot has not been captured"
                )
            return dict(self._sector_members)

    def restore_authenticated_sector_members(
        self,
        *,
        members: Mapping[str, tuple[str, ...]],
        as_of: datetime,
        catalog_revision: str,
    ) -> None:
        """Prime process-local routing from an authenticated app snapshot.

        The complete typed sector batch remains owned and validated by the
        screening service.  Only the member routing required by
        ``structure_bundle`` is restored here; no market fact is recomputed,
        no changed-bar cursor is advanced, and no disk cache is overwritten.
        """

        observed_at = normalize_datetime(as_of, "restored sector members as_of")
        if not isinstance(catalog_revision, str) or not catalog_revision:
            raise ValueError("catalog_revision must be a non-empty string")
        validated = self._validated_members(members)
        if any(
            not sector_id
            or values != tuple(sorted(set(values)))
            for sector_id, values in validated.items()
        ):
            raise NativeScreeningWorkerProtocolError(
                "restored sector membership must be canonical"
            )
        attestation = sha256_json(
            {
                "schema": "chanlun-restored-sector-member-routing/v1",
                "as_of": observed_at.isoformat(),
                "catalog_revision": catalog_revision,
                "members": {
                    key: list(values) for key, values in sorted(validated.items())
                },
            }
        )
        with self._cache_lock:
            self._sector_members = dict(validated)
            self._sector_cache_state = "restored_from_screening_snapshot"
            self._sector_cache_reason = None
            self._sector_cache_requested_as_of = observed_at
            self._sector_cache_content_sha256 = attestation

    def changed_bars(self, since: datetime | None) -> tuple[BarKey, ...]:
        cutoff = None if since is None else normalize_datetime(since, "changed bars cutoff")
        with self._cache_lock:
            changed = tuple(
                item
                for item in self._changed_bars
                if (item.code, item.frequency, item.closed_at)
                not in self._emitted_bar_ids
                and (cutoff is None or item.closed_at > cutoff)
            )
            self._emitted_bar_ids.update(
                (item.code, item.frequency, item.closed_at) for item in changed
            )
        return tuple(
            sorted(
                changed,
                key=lambda item: (item.closed_at, item.code, item.frequency),
            )
        )

    def active_watchlist(self) -> tuple[str, ...]:
        return self.active_watchlist_scope()[0]

    def active_watchlist_scope(
        self,
    ) -> tuple[tuple[str, ...], tuple[str, ...]]:
        requested = _stock_codes(self._watchlist_provider())
        eligible = self.tradable_instrument_codes(requested)
        return eligible, tuple(
            code for code in requested if code not in eligible
        )

    def holdings(self) -> tuple[str, ...]:
        return self.holdings_scope()[0]

    def holdings_scope(
        self,
    ) -> tuple[tuple[str, ...], tuple[str, ...]]:
        requested = _stock_codes(self._holdings_provider())
        eligible = self.tradable_instrument_codes(requested)
        return eligible, tuple(
            code for code in requested if code not in eligible
        )

    def tradable_instrument_codes(
        self,
        codes: tuple[str, ...],
    ) -> tuple[str, ...]:
        """Classify monitor supplements inside the isolated QMT worker."""

        normalized = _stock_codes(codes)
        if not normalized:
            return ()
        value = self._transport.request(
            "tradable_instrument_codes",
            codes=normalized,
        )
        if (
            type(value) is not tuple
            or any(type(code) is not str for code in value)
            or len(value) != len(set(value))
            or tuple(sorted(value)) != value
            or any(code not in normalized for code in value)
        ):
            raise NativeScreeningWorkerProtocolError(
                "invalid tradable instrument scope result"
            )
        return value

    def screening_instrument_types(
        self,
        codes: tuple[str, ...],
    ) -> Mapping[str, str]:
        """Return exact native types without collapsing unresolved results."""

        normalized = _stock_codes(codes)
        if not normalized:
            return {}
        value = self._transport.request(
            "screening_instrument_types",
            codes=normalized,
        )
        if (
            not isinstance(value, Mapping)
            or set(value) != set(normalized)
            or any(
                type(code) is not str
                or type(kind) is not str
                or kind not in _KNOWN_SCREENING_INSTRUMENT_TYPES
                for code, kind in value.items()
            )
        ):
            raise NativeScreeningWorkerProtocolError(
                "invalid instrument type disposition result"
            )
        return {code: str(value[code]) for code in normalized}

    def symbol_name(self, code: str) -> str | None:
        with self._cache_lock:
            cached = self._symbol_names.get(code)
        if cached is not None:
            return cached
        value = self._transport.request("symbol_name", code=code)
        if value is not None and not isinstance(value, str):
            raise NativeScreeningWorkerProtocolError("invalid symbol name result")
        return value

    def trading_session_evidence(
        self,
        *,
        session: date,
        observed_at: datetime,
    ) -> Mapping[str, object]:
        """Read and validate QMT calendar evidence in the native worker."""

        if isinstance(session, datetime) or not isinstance(session, date):
            raise TypeError("session must be a date")
        observed = normalize_datetime(observed_at, "observed_at")
        with self._cache_lock:
            cached = self._trading_session_cache.get(session)
        if cached is not None:
            return validate_trading_session_evidence(
                cached,
                session=session,
                observed_at=observed,
            )
        # Screening structure reads are intentionally serialized through one
        # isolated native worker.  A readiness probe must not queue behind a
        # potentially long ``structure_bundle`` call: doing so can make the
        # Web deployment health gate time out even though both processes are
        # healthy.  Busy means calendar provenance is temporarily unavailable,
        # never that the target is a weekday/trading session.
        worker_health = self._transport.health_snapshot()
        if worker_health.get("in_flight") is True:
            return build_trading_session_evidence(
                session=session,
                observed_at=observed,
                query_attempted=False,
                query_succeeded=False,
            )
        value = self._transport.request(
            "trading_session_evidence",
            session=session,
            observed_at=observed,
        )
        if not isinstance(value, Mapping):
            raise NativeScreeningWorkerProtocolError(
                "invalid trading session evidence"
            )
        try:
            validated = validate_trading_session_evidence(
                value,
                session=session,
                observed_at=observed,
            )
        except (TypeError, ValueError) as exc:
            raise NativeScreeningWorkerProtocolError(
                "invalid trading session evidence"
            ) from exc
        if validated["classification"] != "UNRESOLVED":
            with self._cache_lock:
                self._trading_session_cache[session] = validated
        return validated

    def structure_bundle(
        self,
        code: str,
        *,
        as_of: datetime,
        sector: SectorAssessment,
        frequencies: tuple[str, ...],
    ) -> SymbolStructureBundle:
        return self._structure_bundle(
            code,
            as_of=as_of,
            sector=sector,
            frequencies=frequencies,
            higher_timeframe_as_of=None,
        )

    def structure_bundle_with_risk_cutoff(
        self,
        code: str,
        *,
        as_of: datetime,
        sector: SectorAssessment,
        frequencies: tuple[str, ...],
        risk_evidence_cutoff: datetime,
    ) -> SymbolStructureBundle:
        """Keep current 1m precision while freezing M/W/D evidence earlier."""

        return self._structure_bundle(
            code,
            as_of=as_of,
            sector=sector,
            frequencies=frequencies,
            higher_timeframe_as_of=normalize_datetime(
                risk_evidence_cutoff,
                "risk_evidence_cutoff",
            ),
        )

    def _structure_bundle(
        self,
        code: str,
        *,
        as_of: datetime,
        sector: SectorAssessment,
        frequencies: tuple[str, ...],
        higher_timeframe_as_of: datetime | None,
    ) -> SymbolStructureBundle:
        with self._cache_lock:
            members = self._sector_members
            sector_members = (
                None
                if members is None
                else tuple(members.get(sector.sector_id, ()))
            )
        if sector_members is None:
            raise NativeScreeningWorkerUnavailable(
                "atomic sector snapshot has not been captured"
            )
        value = self._structure_transport(code).request(
            "structure_bundle",
            code=code,
            as_of=as_of,
            sector=sector,
            sector_members=sector_members,
            frequencies=frequencies,
            higher_timeframe_as_of=higher_timeframe_as_of,
        )
        if not isinstance(value, SymbolStructureBundle):
            raise NativeScreeningWorkerProtocolError("invalid structure bundle result")
        return value

    def health_snapshot(self) -> dict[str, object]:
        result = self._transport.health_snapshot()
        worker_health = tuple(
            transport.health_snapshot()
            for transport in self._structure_transports
        )
        result["structure_worker_pool"] = {
            "configured_worker_count": len(worker_health),
            "running_worker_count": sum(
                value.get("worker_alive") is True for value in worker_health
            ),
            "ready_worker_count": sum(
                value.get("ready") is True for value in worker_health
            ),
            "in_flight_worker_count": sum(
                value.get("in_flight") is True for value in worker_health
            ),
            "worker_pids": [
                value.get("worker_pid")
                for value in worker_health
                if type(value.get("worker_pid")) is int
            ],
            "workers": list(worker_health),
        }
        with self._cache_lock:
            result["sector_snapshot_cache"] = {
                "schema": _SECTOR_CACHE_SCHEMA,
                "enabled": self._sector_cache_path is not None,
                "state": self._sector_cache_state,
                "reason": self._sector_cache_reason,
                "source_revision": self._sector_cache_revision,
                "requested_as_of": _iso(self._sector_cache_requested_as_of),
                "content_sha256": self._sector_cache_content_sha256,
            }
        return result

    def close(self) -> None:
        for transport in self._structure_transports:
            transport.shutdown()


__all__ = (
    "IPC_SCHEMA",
    "IPC_AUTHKEY_ENV",
    "NativeScreeningWorkerError",
    "NativeScreeningWorkerProtocolError",
    "NativeScreeningWorkerRemoteError",
    "NativeScreeningWorkerTimeout",
    "NativeScreeningWorkerUnavailable",
    "NativeTradingDataGatewayProcessProxy",
    "NativeWorkerProcessConfig",
    "NativeWorkerProcessTransport",
)
