"""Restart-safe persistence for analysis-only exit evaluations."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime
import hashlib
import json
from pathlib import Path
import re
import sqlite3
from threading import RLock
from types import MappingProxyType

from .fingerprints import normalize_datetime, sha256_json
from .exit_evidence_policy import ExitEvidencePolicy
from .exit_runtime import (
    EntryLedgerResolver,
    ExitEvaluationRequest,
    ExitRecommendation,
    ExitRuntimeError,
    evaluate_tracked_position,
)
from .mutation_fence import MutationLeaseGuard, mutation_fenced


_FINGERPRINT_RE = re.compile(r"sha256:[0-9a-f]{64}")
_SNAPSHOT_PREFIX = "exit-evaluation:"
LIVE_ORDER_CAPABILITY = False
_SNAPSHOT_FIELDS = frozenset(
    {
        "schema_version",
        "snapshot_id",
        "payload_fingerprint",
        "entry_event_id",
        "evaluation_cycle_id",
        "entry_provenance_fingerprint",
        "exit_evidence_policy_fingerprint",
        "certified_corpus_manifest_fingerprint",
        "source_pdf_fingerprint",
        "bar_structure_payload_fingerprint",
        "risk_context_payload_fingerprint",
        "quote_payload_fingerprint",
        "algorithm_version",
        "evaluation_version",
        "recommendation_payload",
        "evaluated_at",
    }
)


class ExitEvaluationConflictError(RuntimeError):
    """An immutable identity or compare-and-swap revision conflicted."""


class ExitEvaluationIntegrityError(RuntimeError):
    """Persisted exit-evaluation evidence failed integrity validation."""


def _require_text(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip() or value != value.strip():
        raise ValueError(f"{field_name} must be a non-empty trimmed string")
    return value


def _require_fingerprint(value: object, field_name: str) -> str:
    if not isinstance(value, str) or _FINGERPRINT_RE.fullmatch(value) is None:
        raise ValueError(f"{field_name} must use sha256:<64 lowercase hex>")
    return value


def _require_positive_int(value: object, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{field_name} must be a positive integer")
    return value


def _freeze_json(value: object, field_name: str) -> object:
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if value != value or value in (float("inf"), float("-inf")):
            raise ValueError(f"{field_name} must contain finite JSON values")
        return value
    if isinstance(value, Mapping):
        if not all(isinstance(key, str) for key in value):
            raise ValueError(f"{field_name} must contain string object keys")
        return MappingProxyType(
            {
                key: _freeze_json(item, field_name)
                for key, item in value.items()
            }
        )
    if isinstance(value, (tuple, list)):
        return tuple(_freeze_json(item, field_name) for item in value)
    raise ValueError(f"{field_name} must be JSON-compatible")


def _thaw_json(value: object) -> object:
    if isinstance(value, Mapping):
        return {key: _thaw_json(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw_json(item) for item in value]
    return value


def _snapshot_identity(entry_event_id: str, evaluation_cycle_id: str) -> str:
    fingerprint = sha256_json(
        {
            "entry_event_id": entry_event_id,
            "evaluation_cycle_id": evaluation_cycle_id,
        }
    )
    return _SNAPSHOT_PREFIX + fingerprint[7:]


@dataclass(frozen=True, slots=True)
class ExitEvaluationSnapshot:
    entry_event_id: str
    evaluation_cycle_id: str
    entry_provenance_fingerprint: str
    exit_evidence_policy_fingerprint: str
    certified_corpus_manifest_fingerprint: str
    source_pdf_fingerprint: str
    bar_structure_payload_fingerprint: str
    risk_context_payload_fingerprint: str
    quote_payload_fingerprint: str
    algorithm_version: str
    evaluation_version: int
    recommendation_payload: Mapping[str, object]
    evaluated_at: datetime

    def __post_init__(self) -> None:
        _require_text(self.entry_event_id, "entry_event_id")
        for field_name in (
            "evaluation_cycle_id",
            "entry_provenance_fingerprint",
            "exit_evidence_policy_fingerprint",
            "certified_corpus_manifest_fingerprint",
            "source_pdf_fingerprint",
            "bar_structure_payload_fingerprint",
            "risk_context_payload_fingerprint",
            "quote_payload_fingerprint",
        ):
            _require_fingerprint(getattr(self, field_name), field_name)
        _require_text(self.algorithm_version, "algorithm_version")
        _require_positive_int(self.evaluation_version, "evaluation_version")
        if not isinstance(self.recommendation_payload, Mapping):
            raise TypeError("recommendation_payload must be a mapping")
        frozen_payload = _freeze_json(
            self.recommendation_payload,
            "recommendation_payload",
        )
        if not isinstance(frozen_payload, Mapping):
            raise TypeError("recommendation_payload must freeze to a mapping")
        object.__setattr__(self, "recommendation_payload", frozen_payload)
        object.__setattr__(
            self,
            "evaluated_at",
            normalize_datetime(self.evaluated_at, "evaluated_at"),
        )

    @property
    def snapshot_id(self) -> str:
        return _snapshot_identity(
            self.entry_event_id,
            self.evaluation_cycle_id,
        )

    @property
    def payload_fingerprint(self) -> str:
        return sha256_json(self._payload())

    def _payload(self) -> dict[str, object]:
        return {
            "entry_event_id": self.entry_event_id,
            "evaluation_cycle_id": self.evaluation_cycle_id,
            "entry_provenance_fingerprint": self.entry_provenance_fingerprint,
            "exit_evidence_policy_fingerprint": (
                self.exit_evidence_policy_fingerprint
            ),
            "certified_corpus_manifest_fingerprint": (
                self.certified_corpus_manifest_fingerprint
            ),
            "source_pdf_fingerprint": self.source_pdf_fingerprint,
            "bar_structure_payload_fingerprint": (
                self.bar_structure_payload_fingerprint
            ),
            "risk_context_payload_fingerprint": (
                self.risk_context_payload_fingerprint
            ),
            "quote_payload_fingerprint": self.quote_payload_fingerprint,
            "algorithm_version": self.algorithm_version,
            "evaluation_version": self.evaluation_version,
            "recommendation_payload": self.recommendation_payload,
            "evaluated_at": self.evaluated_at,
        }

    def to_dict(self) -> dict[str, object]:
        payload = self._payload()
        return {
            "schema_version": 1,
            "snapshot_id": self.snapshot_id,
            "payload_fingerprint": self.payload_fingerprint,
            **{
                key: (
                    value.isoformat()
                    if isinstance(value, datetime)
                    else _thaw_json(value)
                )
                for key, value in payload.items()
            },
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, object]) -> ExitEvaluationSnapshot:
        if not isinstance(payload, Mapping) or set(payload) != _SNAPSHOT_FIELDS:
            raise ExitEvaluationIntegrityError("snapshot fields mismatch")
        if payload["schema_version"] != 1:
            raise ExitEvaluationIntegrityError("snapshot schema mismatch")
        raw_time = payload["evaluated_at"]
        if not isinstance(raw_time, str):
            raise ExitEvaluationIntegrityError("snapshot evaluated_at invalid")
        try:
            evaluated_at = datetime.fromisoformat(raw_time)
            snapshot = cls(
                entry_event_id=payload["entry_event_id"],
                evaluation_cycle_id=payload["evaluation_cycle_id"],
                entry_provenance_fingerprint=(
                    payload["entry_provenance_fingerprint"]
                ),
                exit_evidence_policy_fingerprint=(
                    payload["exit_evidence_policy_fingerprint"]
                ),
                certified_corpus_manifest_fingerprint=(
                    payload["certified_corpus_manifest_fingerprint"]
                ),
                source_pdf_fingerprint=payload["source_pdf_fingerprint"],
                bar_structure_payload_fingerprint=(
                    payload["bar_structure_payload_fingerprint"]
                ),
                risk_context_payload_fingerprint=(
                    payload["risk_context_payload_fingerprint"]
                ),
                quote_payload_fingerprint=payload["quote_payload_fingerprint"],
                algorithm_version=payload["algorithm_version"],
                evaluation_version=payload["evaluation_version"],
                recommendation_payload=payload["recommendation_payload"],
                evaluated_at=evaluated_at,
            )
        except (TypeError, ValueError) as exc:
            raise ExitEvaluationIntegrityError("snapshot payload invalid") from exc
        if payload["snapshot_id"] != snapshot.snapshot_id:
            raise ExitEvaluationIntegrityError("snapshot identity mismatch")
        if payload["payload_fingerprint"] != snapshot.payload_fingerprint:
            raise ExitEvaluationIntegrityError("snapshot payload fingerprint mismatch")
        return snapshot


@dataclass(frozen=True, slots=True, order=True)
class ExitEvaluationCommitment:
    """Exact immutable identity committed by one trusted paper-bar cycle."""

    snapshot_id: str
    payload_fingerprint: str
    entry_event_id: str
    evaluation_cycle_id: str
    evaluated_at: datetime

    def __post_init__(self) -> None:
        _require_text(self.entry_event_id, "entry_event_id")
        _require_fingerprint(
            self.payload_fingerprint,
            "payload_fingerprint",
        )
        _require_fingerprint(
            self.evaluation_cycle_id,
            "evaluation_cycle_id",
        )
        object.__setattr__(
            self,
            "evaluated_at",
            normalize_datetime(self.evaluated_at, "evaluated_at"),
        )
        expected_snapshot_id = _snapshot_identity(
            self.entry_event_id,
            self.evaluation_cycle_id,
        )
        if self.snapshot_id != expected_snapshot_id:
            raise ValueError("snapshot_id does not match exit evaluation identity")

    @classmethod
    def from_snapshot(
        cls,
        snapshot: ExitEvaluationSnapshot,
    ) -> ExitEvaluationCommitment:
        if not isinstance(snapshot, ExitEvaluationSnapshot):
            raise TypeError("snapshot must be ExitEvaluationSnapshot")
        return cls(
            snapshot_id=snapshot.snapshot_id,
            payload_fingerprint=snapshot.payload_fingerprint,
            entry_event_id=snapshot.entry_event_id,
            evaluation_cycle_id=snapshot.evaluation_cycle_id,
            evaluated_at=snapshot.evaluated_at,
        )

    def to_dict(self) -> dict[str, str]:
        return {
            "snapshot_id": self.snapshot_id,
            "payload_fingerprint": self.payload_fingerprint,
            "entry_event_id": self.entry_event_id,
            "evaluation_cycle_id": self.evaluation_cycle_id,
            "evaluated_at": self.evaluated_at.isoformat(),
        }

    @classmethod
    def from_dict(
        cls,
        payload: Mapping[str, object],
    ) -> ExitEvaluationCommitment:
        if not isinstance(payload, Mapping) or set(payload) != {
            "snapshot_id",
            "payload_fingerprint",
            "entry_event_id",
            "evaluation_cycle_id",
            "evaluated_at",
        }:
            raise ExitEvaluationIntegrityError(
                "exit evaluation commitment fields mismatch"
            )
        try:
            raw_evaluated_at = payload["evaluated_at"]
            if not isinstance(raw_evaluated_at, str):
                raise ValueError("evaluated_at must be ISO datetime text")
            return cls(
                snapshot_id=payload["snapshot_id"],
                payload_fingerprint=payload["payload_fingerprint"],
                entry_event_id=payload["entry_event_id"],
                evaluation_cycle_id=payload["evaluation_cycle_id"],
                evaluated_at=datetime.fromisoformat(raw_evaluated_at),
            )
        except (TypeError, ValueError) as exc:
            raise ExitEvaluationIntegrityError(
                "exit evaluation commitment invalid"
            ) from exc


def _snapshot_json(snapshot: ExitEvaluationSnapshot) -> str:
    return json.dumps(
        snapshot.to_dict(),
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _checksum(value: str) -> str:
    return "sha256:" + hashlib.sha256(value.encode("utf-8")).hexdigest()


class SQLiteExitEvaluationStore:
    """Append-only SQLite store with identity idempotence and global CAS."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = RLock()
        self._mutation_fence = MutationLeaseGuard()
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS exit_evaluation_meta (
                    singleton_id INTEGER PRIMARY KEY CHECK (singleton_id = 1),
                    revision INTEGER NOT NULL CHECK (revision >= 0)
                );
                CREATE TABLE IF NOT EXISTS exit_evaluations (
                    entry_event_id TEXT NOT NULL,
                    evaluation_cycle_id TEXT NOT NULL,
                    snapshot_id TEXT NOT NULL UNIQUE,
                    payload_fingerprint TEXT NOT NULL,
                    snapshot_json TEXT NOT NULL,
                    snapshot_checksum TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY (entry_event_id, evaluation_cycle_id)
                );
                INSERT OR IGNORE INTO exit_evaluation_meta (
                    singleton_id, revision
                ) VALUES (1, 0);
                """
            )

    def bind_strategy_run(self, strategy_run: object) -> None:
        bindings = getattr(strategy_run, "store_bindings", {})
        binding = bindings.get("exit") if isinstance(bindings, Mapping) else None
        self._mutation_fence.bind(
            strategy_run,
            expected_store_role="exit",
            expected_store_path=self.path,
            expected_store_instance_id=getattr(
                binding,
                "store_instance_id",
                None,
            ),
        )

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=30)
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    @staticmethod
    def _revision(connection: sqlite3.Connection) -> int:
        row = connection.execute(
            "SELECT revision FROM exit_evaluation_meta WHERE singleton_id = 1"
        ).fetchone()
        if row is None or isinstance(row[0], bool) or not isinstance(row[0], int):
            raise ExitEvaluationIntegrityError("store revision is invalid")
        if row[0] < 0:
            raise ExitEvaluationIntegrityError("store revision is invalid")
        return row[0]

    @property
    def revision(self) -> int:
        with self._lock, self._connect() as connection:
            return self._revision(connection)

    @staticmethod
    def _snapshot_from_row(row: tuple[object, ...]) -> ExitEvaluationSnapshot:
        (
            stored_entry_event_id,
            stored_cycle_id,
            stored_snapshot_id,
            stored_payload_fingerprint,
            snapshot_json,
            stored_checksum,
            stored_created_at,
        ) = row
        if not isinstance(snapshot_json, str) or not isinstance(
            stored_checksum,
            str,
        ):
            raise ExitEvaluationIntegrityError("stored snapshot is invalid")
        if _checksum(snapshot_json) != stored_checksum:
            raise ExitEvaluationIntegrityError("snapshot checksum mismatch")
        try:
            payload = json.loads(snapshot_json)
        except json.JSONDecodeError as exc:
            raise ExitEvaluationIntegrityError("snapshot JSON invalid") from exc
        snapshot = ExitEvaluationSnapshot.from_dict(payload)
        if snapshot.payload_fingerprint != stored_payload_fingerprint:
            raise ExitEvaluationIntegrityError(
                "stored payload fingerprint mismatch"
            )
        if (
            stored_entry_event_id != snapshot.entry_event_id
            or stored_cycle_id != snapshot.evaluation_cycle_id
            or stored_snapshot_id != snapshot.snapshot_id
            or stored_created_at != snapshot.evaluated_at.isoformat()
        ):
            raise ExitEvaluationIntegrityError("snapshot row identity mismatch")
        return snapshot

    def get(
        self,
        entry_event_id: str,
        evaluation_cycle_id: str,
    ) -> ExitEvaluationSnapshot | None:
        _require_text(entry_event_id, "entry_event_id")
        _require_fingerprint(evaluation_cycle_id, "evaluation_cycle_id")
        with self._lock, self._connect() as connection:
            row = connection.execute(
                """
                SELECT entry_event_id, evaluation_cycle_id, snapshot_id,
                       payload_fingerprint, snapshot_json, snapshot_checksum,
                       created_at
                FROM exit_evaluations
                WHERE entry_event_id = ? AND evaluation_cycle_id = ?
                """,
                (entry_event_id, evaluation_cycle_id),
            ).fetchone()
        if row is None:
            return None
        return self._snapshot_from_row(row)

    def list_snapshots(self) -> tuple[ExitEvaluationSnapshot, ...]:
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                """
                SELECT entry_event_id, evaluation_cycle_id, snapshot_id,
                       payload_fingerprint, snapshot_json, snapshot_checksum,
                       created_at
                FROM exit_evaluations
                ORDER BY created_at DESC, snapshot_id DESC
                """
            ).fetchall()
        return tuple(self._snapshot_from_row(row) for row in rows)

    @mutation_fenced("exit_evaluation_store.persist")
    def persist(
        self,
        snapshot: ExitEvaluationSnapshot,
        *,
        expected_revision: int,
    ) -> ExitEvaluationSnapshot:
        self._mutation_fence.require()
        if not isinstance(snapshot, ExitEvaluationSnapshot):
            raise TypeError("snapshot must be ExitEvaluationSnapshot")
        if (
            isinstance(expected_revision, bool)
            or not isinstance(expected_revision, int)
            or expected_revision < 0
        ):
            raise ValueError("expected_revision must be non-negative")
        snapshot_json = _snapshot_json(snapshot)
        checksum = _checksum(snapshot_json)
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                row = connection.execute(
                    """
                    SELECT entry_event_id, evaluation_cycle_id, snapshot_id,
                           payload_fingerprint, snapshot_json,
                           snapshot_checksum, created_at
                    FROM exit_evaluations
                    WHERE entry_event_id = ? AND evaluation_cycle_id = ?
                    """,
                    (snapshot.entry_event_id, snapshot.evaluation_cycle_id),
                ).fetchone()
                if row is not None:
                    existing = self._snapshot_from_row(row)
                    if existing.payload_fingerprint != snapshot.payload_fingerprint:
                        raise ExitEvaluationConflictError(
                            "exit evaluation payload conflict"
                        )
                    connection.commit()
                    return existing
                current_revision = self._revision(connection)
                if current_revision != expected_revision:
                    raise ExitEvaluationConflictError(
                        "exit evaluation revision conflict"
                    )
                connection.execute(
                    """
                    INSERT INTO exit_evaluations (
                        entry_event_id,
                        evaluation_cycle_id,
                        snapshot_id,
                        payload_fingerprint,
                        snapshot_json,
                        snapshot_checksum,
                        created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        snapshot.entry_event_id,
                        snapshot.evaluation_cycle_id,
                        snapshot.snapshot_id,
                        snapshot.payload_fingerprint,
                        snapshot_json,
                        checksum,
                        snapshot.evaluated_at.isoformat(),
                    ),
                )
                changed = connection.execute(
                    """
                    UPDATE exit_evaluation_meta
                    SET revision = ?
                    WHERE singleton_id = 1 AND revision = ?
                    """,
                    (current_revision + 1, current_revision),
                ).rowcount
                if changed != 1:
                    raise ExitEvaluationConflictError(
                        "exit evaluation revision conflict"
                    )
                connection.commit()
            except BaseException:
                connection.rollback()
                raise
        return snapshot


@dataclass(frozen=True, slots=True)
class ExitEvaluationFailure:
    entry_event_id: str
    evaluation_cycle_id: str
    code: str
    reason: str
    detail: str

    def __post_init__(self) -> None:
        for field_name in (
            "entry_event_id",
            "evaluation_cycle_id",
            "code",
            "reason",
            "detail",
        ):
            _require_text(getattr(self, field_name), field_name)


@dataclass(frozen=True, slots=True)
class ExitEvaluationBatchResult:
    snapshots: tuple[ExitEvaluationSnapshot, ...]
    failures: tuple[ExitEvaluationFailure, ...]

    def __post_init__(self) -> None:
        snapshots = tuple(self.snapshots)
        failures = tuple(self.failures)
        if not all(isinstance(item, ExitEvaluationSnapshot) for item in snapshots):
            raise TypeError("snapshots must contain ExitEvaluationSnapshot")
        if not all(isinstance(item, ExitEvaluationFailure) for item in failures):
            raise TypeError("failures must contain ExitEvaluationFailure")
        object.__setattr__(self, "snapshots", snapshots)
        object.__setattr__(self, "failures", failures)


class ExitEvaluationService:
    """Evaluate and append immutable analysis snapshots; never create intents."""

    def __init__(
        self,
        store: SQLiteExitEvaluationStore,
        *,
        evidence_policy: ExitEvidencePolicy,
        entry_ledger_resolver: EntryLedgerResolver,
    ) -> None:
        if not isinstance(store, SQLiteExitEvaluationStore):
            raise TypeError("store must be SQLiteExitEvaluationStore")
        if not isinstance(evidence_policy, ExitEvidencePolicy):
            raise TypeError("evidence_policy must be ExitEvidencePolicy")
        if not callable(entry_ledger_resolver):
            raise TypeError("entry_ledger_resolver must be callable")
        self.store = store
        self.evidence_policy = evidence_policy
        self.entry_ledger_resolver = entry_ledger_resolver
        self._mutation_fence = MutationLeaseGuard()

    def bind_strategy_run(self, strategy_run: object) -> None:
        self._mutation_fence.bind(strategy_run)

    @staticmethod
    def _snapshot(
        request: ExitEvaluationRequest,
        recommendation: ExitRecommendation,
    ) -> ExitEvaluationSnapshot:
        payload = recommendation.to_dict()
        return ExitEvaluationSnapshot(
            entry_event_id=recommendation.entry_event_id,
            evaluation_cycle_id=recommendation.evaluation_cycle_id,
            entry_provenance_fingerprint=(
                recommendation.entry_provenance_fingerprint
            ),
            exit_evidence_policy_fingerprint=(
                recommendation.exit_evidence_policy_fingerprint
            ),
            certified_corpus_manifest_fingerprint=(
                recommendation.certified_corpus_manifest_fingerprint
            ),
            source_pdf_fingerprint=recommendation.source_pdf_fingerprint,
            bar_structure_payload_fingerprint=(
                recommendation.bar_structure_payload_fingerprint
            ),
            risk_context_payload_fingerprint=(
                recommendation.risk_context_payload_fingerprint
            ),
            quote_payload_fingerprint=(
                recommendation.quote_payload_fingerprint
            ),
            algorithm_version=recommendation.algorithm_version,
            evaluation_version=recommendation.evaluation_version,
            recommendation_payload=payload,
            evaluated_at=request.bar_closed_at,
        )

    @mutation_fenced("exit_evaluation_service.evaluate_and_persist")
    def evaluate_and_persist(
        self,
        request: ExitEvaluationRequest,
    ) -> ExitEvaluationSnapshot:
        self._mutation_fence.require()
        if not isinstance(request, ExitEvaluationRequest):
            raise TypeError("request must be ExitEvaluationRequest")
        recommendation = evaluate_tracked_position(
            request,
            evidence_resolver=self.evidence_policy,
            entry_ledger_resolver=self.entry_ledger_resolver,
        )
        snapshot = self._snapshot(request, recommendation)
        return self.store.persist(
            snapshot,
            expected_revision=self.store.revision,
        )

    @mutation_fenced("exit_evaluation_service.evaluate_and_persist_many")
    def evaluate_and_persist_many(
        self,
        requests: Iterable[ExitEvaluationRequest],
    ) -> ExitEvaluationBatchResult:
        self._mutation_fence.require()
        if isinstance(requests, (str, bytes)):
            raise TypeError("requests must be an iterable")
        try:
            frozen_requests = tuple(requests)
        except TypeError as exc:
            raise TypeError("requests must be an iterable") from exc
        snapshots: list[ExitEvaluationSnapshot] = []
        failures: list[ExitEvaluationFailure] = []
        seen: set[tuple[str, str]] = set()
        for index, request in enumerate(frozen_requests):
            if isinstance(request, ExitEvaluationRequest):
                entry_event_id = request.position.entry_event_id
                evaluation_cycle_id = request.evaluation_cycle_id
                code = request.position.holding.code
            else:
                entry_event_id = f"invalid-request-{index}"
                evaluation_cycle_id = "invalid-cycle"
                code = "unknown"
            try:
                snapshot = self.evaluate_and_persist(request)
            except Exception as exc:
                reason = (
                    exc.reason
                    if isinstance(exc, ExitRuntimeError)
                    else type(exc).__name__
                )
                failures.append(
                    ExitEvaluationFailure(
                        entry_event_id=entry_event_id,
                        evaluation_cycle_id=evaluation_cycle_id,
                        code=code,
                        reason=reason,
                        detail=str(exc) or reason,
                    )
                )
                continue
            key = (snapshot.entry_event_id, snapshot.evaluation_cycle_id)
            if key not in seen:
                snapshots.append(snapshot)
                seen.add(key)
        return ExitEvaluationBatchResult(tuple(snapshots), tuple(failures))

__all__ = (
    "ExitEvaluationCommitment",
    "ExitEvaluationConflictError",
    "ExitEvaluationFailure",
    "ExitEvaluationIntegrityError",
    "ExitEvaluationBatchResult",
    "ExitEvaluationService",
    "ExitEvaluationSnapshot",
    "SQLiteExitEvaluationStore",
    "LIVE_ORDER_CAPABILITY",
)
