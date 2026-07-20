"""Fail-closed strategy-run identity and SQLite epoch isolation.

One strategy epoch owns one ledger/bar/risk/exit store set.  Historical stores
are never relabelled with a newer strategy identity.
"""

from __future__ import annotations

from asyncio import current_task
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal, InvalidOperation
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
from threading import Event, Lock, RLock, get_ident
import time
from types import MappingProxyType
from typing import Callable, Iterator, Mapping
from uuid import uuid4

from .fingerprints import normalize_datetime, sha256_json
from .monitor import MonitorConfig
from .universe import UniversePolicy


STRATEGY_RUN_STORE_ROLES = ("ledger", "bar", "risk", "exit")
STRATEGY_RUN_SWITCH_CAPABILITY = "cold_stop_drain_required"
STRATEGY_RUN_MUTATION_LEASE_PROTOCOL = "durable_registry_v1"
_FINGERPRINT_RE = re.compile(r"sha256:[0-9a-f]{64}")
_RUN_STATUSES = frozenset({"initializing", "active", "closed", "blocked"})
_BOOTSTRAP_PROCESS_LOCKS_GUARD = RLock()
_BOOTSTRAP_PROCESS_LOCKS: dict[Path, Lock] = {}


class StrategyRunIntegrityError(RuntimeError):
    """The persisted run identity or its store set is not trustworthy."""


def _required_fingerprint(value: object, field_name: str) -> str:
    if not isinstance(value, str) or _FINGERPRINT_RE.fullmatch(value) is None:
        raise ValueError(f"{field_name} must use sha256:<64 lowercase hex>")
    return value


def _required_text(value: object, field_name: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or not value.isprintable()
        or len(value) > 255
    ):
        raise ValueError(f"{field_name} must be bounded printable text")
    return value


def _required_epoch(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError("requested_epoch must be a positive integer")
    return value


def _bootstrap_lock_path(path: Path) -> Path:
    return path.with_name(path.name + ".strategy-bootstrap.lock")


@contextmanager
def _exclusive_bootstrap_lock_set(
    paths: tuple[Path, ...],
    *,
    timeout: float,
) -> Iterator[None]:
    if (
        isinstance(timeout, bool)
        or not isinstance(timeout, (int, float))
        or timeout <= 0
    ):
        raise ValueError("bootstrap lock timeout must be positive")
    lock_paths = tuple(
        sorted(
            {_bootstrap_lock_path(path.expanduser().absolute()) for path in paths},
            key=lambda item: os.path.normcase(str(item)),
        )
    )
    deadline = time.monotonic() + float(timeout)
    process_locks: list[Lock] = []
    streams: list[object] = []
    try:
        for lock_path in lock_paths:
            with _BOOTSTRAP_PROCESS_LOCKS_GUARD:
                process_lock = _BOOTSTRAP_PROCESS_LOCKS.setdefault(
                    lock_path,
                    Lock(),
                )
            remaining = deadline - time.monotonic()
            if remaining <= 0 or not process_lock.acquire(timeout=remaining):
                raise StrategyRunIntegrityError(
                    "strategy_run_bootstrap_claim_unavailable"
                )
            process_locks.append(process_lock)

        for lock_path in lock_paths:
            lock_path.parent.mkdir(parents=True, exist_ok=True)
            stream = open(lock_path, "a+b")
            streams.append(stream)
            stream.seek(0, os.SEEK_END)
            if stream.tell() == 0:
                stream.write(b"\0")
                stream.flush()
                os.fsync(stream.fileno())
            while True:
                try:
                    stream.seek(0)
                    if os.name == "nt":
                        import msvcrt

                        msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
                    else:
                        import fcntl

                        fcntl.flock(
                            stream.fileno(),
                            fcntl.LOCK_EX | fcntl.LOCK_NB,
                        )
                    break
                except OSError as exc:
                    if time.monotonic() >= deadline:
                        raise StrategyRunIntegrityError(
                            "strategy_run_bootstrap_claim_unavailable"
                        ) from exc
                    time.sleep(0.01)
        yield
    finally:
        for stream in reversed(streams):
            try:
                stream.seek(0)
                if os.name == "nt":
                    import msvcrt

                    msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
            except OSError:
                pass
            finally:
                stream.close()
        for process_lock in reversed(process_locks):
            process_lock.release()


def _canonical_json(value: Mapping[str, object]) -> str:
    return json.dumps(
        dict(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _text_sha256(value: str) -> str:
    return "sha256:" + hashlib.sha256(value.encode("utf-8")).hexdigest()


def _strategy_run_id(
    *,
    epoch: int,
    strategy_run_fingerprint: str,
    predecessor_run_id: str | None,
) -> str:
    return "paper-run-" + sha256_json(
        {
            "schema_version": 1,
            "epoch": _required_epoch(epoch),
            "strategy_run_fingerprint": _required_fingerprint(
                strategy_run_fingerprint,
                "strategy_run_fingerprint",
            ),
            "predecessor_run_id": predecessor_run_id,
        }
    )[7:]


def build_rule_algorithm_fingerprint(rule_set: object) -> str:
    rule_set_fingerprint = _required_fingerprint(
        getattr(rule_set, "fingerprint", None),
        "rule_set.fingerprint",
    )
    cards = getattr(rule_set, "cards", None)
    if not isinstance(cards, (tuple, list)) or not cards:
        raise ValueError("rule_set.cards must be a non-empty sequence")
    payload: list[dict[str, object]] = []
    for card in cards:
        rule_id = _required_text(getattr(card, "rule_id", None), "rule_id")
        version = getattr(card, "version", None)
        if isinstance(version, bool) or not isinstance(version, int) or version <= 0:
            raise ValueError("rule version must be a positive integer")
        payload.append(
            {
                "rule_id": rule_id,
                "version": version,
                "algorithm_version": _required_text(
                    getattr(card, "algorithm_version", None),
                    "algorithm_version",
                ),
                "rule_card_fingerprint": _required_fingerprint(
                    getattr(card, "fingerprint", None),
                    "rule_card_fingerprint",
                ),
            }
        )
    return sha256_json(
        {
            "schema_version": 1,
            "rule_set_fingerprint": rule_set_fingerprint,
            "cards": sorted(payload, key=lambda item: (item["rule_id"], item["version"])),
        }
    )


def build_review_policy_fingerprint(
    *,
    provider: str,
    model: str,
    prompt_version: str,
    response_schema: Mapping[str, object],
) -> str:
    if not isinstance(response_schema, Mapping) or not response_schema:
        raise ValueError("response_schema must be a non-empty mapping")
    return sha256_json(
        {
            "schema_version": 1,
            "provider": _required_text(provider, "provider"),
            "model": _required_text(model, "model"),
            "prompt_version": _required_text(
                prompt_version,
                "prompt_version",
            ),
            "response_schema": dict(response_schema),
        }
    )


def build_review_runtime_policy_fingerprint(
    *,
    max_evidence_units: int,
    timeout: tuple[float, float],
) -> str:
    if (
        isinstance(max_evidence_units, bool)
        or not isinstance(max_evidence_units, int)
        or max_evidence_units <= 0
    ):
        raise ValueError("max_evidence_units must be a positive integer")
    if not isinstance(timeout, tuple) or len(timeout) != 2:
        raise TypeError("timeout must be a connect/read tuple")
    timeout_values: list[str] = []
    for value, field_name in zip(
        timeout,
        ("connect_timeout", "read_timeout"),
    ):
        if isinstance(value, bool) or not isinstance(
            value,
            (int, float, Decimal),
        ):
            raise TypeError(f"{field_name} must be numeric")
        decimal_value = Decimal(str(value))
        if not decimal_value.is_finite() or decimal_value <= 0:
            raise ValueError(f"{field_name} must be finite and positive")
        timeout_values.append(format(decimal_value.normalize(), "f"))
    return sha256_json(
        {
            "schema_version": 1,
            "max_evidence_units": max_evidence_units,
            "timeout": {
                "connect_seconds": timeout_values[0],
                "read_seconds": timeout_values[1],
            },
        }
    )


def build_universe_policy_fingerprint(policy: UniversePolicy) -> str:
    if type(policy) is not UniversePolicy:
        raise TypeError("policy must be UniversePolicy")
    minimum_turnover = Decimal(str(policy.min_avg_turnover_20d))
    if not minimum_turnover.is_finite() or minimum_turnover < 0:
        raise ValueError("policy minimum turnover must be finite and non-negative")
    return sha256_json(
        {
            "schema_version": 1,
            "market": "a",
            "min_listed_days": policy.min_listed_days,
            "min_avg_turnover_20d": format(minimum_turnover.normalize(), "f"),
            "exclude_st": policy.exclude_st,
            "exclude_delisting": policy.exclude_delisting,
            "require_complete_metadata": policy.require_complete_metadata,
        }
    )


def build_monitor_policy_fingerprint(
    *,
    config: MonitorConfig,
    max_completed_bars: int,
    max_market_age_seconds: int,
    processed_bar_limit: int,
    universe_policy_fingerprint: str,
) -> str:
    if type(config) is not MonitorConfig:
        raise TypeError("config must be MonitorConfig")
    for value, field_name in (
        (max_completed_bars, "max_completed_bars"),
        (max_market_age_seconds, "max_market_age_seconds"),
        (processed_bar_limit, "processed_bar_limit"),
    ):
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise ValueError(f"{field_name} must be a positive integer")
    return sha256_json(
        {
            "schema_version": 1,
            "monitor_config": {
                "enabled": config.enabled,
                "markets": config.markets,
                "scan_interval_seconds": config.scan_interval_seconds,
                "review_workers": config.review_workers,
                "review_queue_limit": config.review_queue_limit,
                "max_llm_reviews_per_day": config.max_llm_reviews_per_day,
                "review_mode": config.review_mode,
                "paper_enabled": config.paper_enabled,
                "auto_order_enabled": config.auto_order_enabled,
            },
            "scanner_runtime": {
                "closed_bar_frequency": "5m",
                "max_market_age_seconds": max_market_age_seconds,
                "processed_bar_limit": processed_bar_limit,
            },
            "structure_runtime": {
                "max_completed_bars": max_completed_bars,
            },
            "universe_policy_fingerprint": _required_fingerprint(
                universe_policy_fingerprint,
                "universe_policy_fingerprint",
            ),
        }
    )


def trusted_bar_schema_fingerprint() -> str:
    return sha256_json(
        {
            "schema_version": 9,
            "fields": (
                "bar_id",
                "code",
                "opened_at",
                "closed_at",
                "open_price",
                "close_price",
                "previous_close",
                "suspended",
                "limit_up_locked",
                "limit_down_locked",
                "max_fill_shares",
            ),
            "decimal_encoding": "canonical-finite-decimal-text",
            "identity": "full-payload-sha256",
            "observation_schema": {
                "tables": (
                    "trusted_signal_observation_log_state",
                    "trusted_signal_observation_cycle",
                    "trusted_signal_first_observation",
                    "trusted_signal_segment_observation",
                ),
                "states": (
                    "trusted_first_seen",
                    "baseline_not_fresh",
                    "quarantined_unknown",
                ),
                "scope": "required_codes_sell_signals_only",
                "authority": "current_required_segment_only",
                "quarantine": (
                    "sticky_until_new_segment_or_explicit_rebaseline"
                ),
                "attempt_generation": "monotonic_per_cycle_start",
                "retry_ambiguity": (
                    "generation_gt_1_quarantines_new_signals"
                ),
                "closed_at_lookup": "indexed_exact_closed_at",
                "atomic_commit": (
                    "manifest+global_first_seen+segment_state+cycle+attempt"
                ),
            },
            "calendar_preflight_recovery_schema": {
                "tables": (
                    "trusted_paper_bar_calendar_preflight",
                    "trusted_paper_bar_calendar_preflight_resolution",
                    "trusted_paper_bar_calendar_preflight_watermark",
                ),
                "watermark": "append_only_committed_cycle_observed_at",
                "atomic_commit": "watermark+cycle+attempt",
                "replay_cleanup": "committed_watermark_only",
            },
            "segment_ledger_schema": {
                "tables": (
                    "trusted_paper_bar_segment",
                    "trusted_paper_bar_segment_member",
                ),
                "cycle_payload_schema_version": 3,
                "bar_binding": "bar_id+payload_sha256+segment_id",
                "legacy_replay": "v2_exact_checksum_only",
                "validation": "full_ledger_at_every_trust_boundary",
                "tail_lookup": "indexed_segment_id+closed_at_desc",
            },
            "exit_manifest_schema": {
                "tables": (
                    "trusted_paper_exit_manifest_log_state",
                    "trusted_paper_exit_manifest",
                ),
                "commitment_fields": (
                    "snapshot_id",
                    "payload_fingerprint",
                    "entry_event_id",
                    "evaluation_cycle_id",
                    "evaluated_at",
                ),
                "cardinality": (
                    "exactly_one_row_per_completed_cycle_including_empty"
                ),
                "atomic_commit": (
                    "exit_manifest+signal_observation+cycle+attempt"
                ),
                "publication": "exact_snapshot_commitment_membership",
                "replay": "exact_commitment_tuple_only",
                "history": "append_only_sha256_chain",
                "external_anchor": (
                    "content_addressed_manifest_history_head"
                ),
                "hot_append_validation": (
                    "cached_full_prefix+exact_log_state+tail_row+tail_anchor"
                ),
                "full_validation_boundaries": (
                    "startup+bind+health+read+bulk+cache_rebase"
                ),
            },
        }
    )


@dataclass(frozen=True, slots=True)
class StrategyRunIdentity:
    rule_set_fingerprint: str
    corpus_manifest_fingerprint: str
    source_pdf_fingerprint: str
    rule_algorithm_fingerprint: str
    strategy_engine_build_fingerprint: str
    scanner_algorithm_fingerprint: str
    structure_algorithm_fingerprint: str
    universe_policy_fingerprint: str
    monitor_policy_fingerprint: str
    review_provider: str
    review_model: str
    review_prompt_version: str
    review_schema_fingerprint: str
    review_runtime_policy_fingerprint: str
    execution_policy_fingerprint: str
    fee_schedule_fingerprint: str
    initial_cash: Decimal | str
    account_algorithm_fingerprint: str
    risk_policy_fingerprint: str
    exit_policy_fingerprint: str
    exit_algorithm_fingerprint: str
    calendar_fingerprint: str
    bar_provider_fingerprint: str
    bar_schema_fingerprint: str
    schema_version: int = 1

    def __post_init__(self) -> None:
        if self.schema_version != 1 or isinstance(self.schema_version, bool):
            raise ValueError("strategy run schema_version must be 1")
        for field_name in (
            "rule_set_fingerprint",
            "corpus_manifest_fingerprint",
            "source_pdf_fingerprint",
            "rule_algorithm_fingerprint",
            "strategy_engine_build_fingerprint",
            "scanner_algorithm_fingerprint",
            "structure_algorithm_fingerprint",
            "universe_policy_fingerprint",
            "monitor_policy_fingerprint",
            "review_schema_fingerprint",
            "review_runtime_policy_fingerprint",
            "execution_policy_fingerprint",
            "fee_schedule_fingerprint",
            "account_algorithm_fingerprint",
            "risk_policy_fingerprint",
            "exit_policy_fingerprint",
            "exit_algorithm_fingerprint",
            "calendar_fingerprint",
            "bar_provider_fingerprint",
            "bar_schema_fingerprint",
        ):
            _required_fingerprint(getattr(self, field_name), field_name)
        for field_name in (
            "review_provider",
            "review_model",
            "review_prompt_version",
        ):
            _required_text(getattr(self, field_name), field_name)
        value = self.initial_cash
        if isinstance(value, str):
            try:
                value = Decimal(value)
            except InvalidOperation as exc:
                raise ValueError("initial_cash must be a positive Decimal") from exc
        if (
            not isinstance(value, Decimal)
            or not value.is_finite()
            or value <= 0
        ):
            raise ValueError("initial_cash must be a positive Decimal")
        object.__setattr__(self, "initial_cash", value)

    def to_payload(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "rule_set_fingerprint": self.rule_set_fingerprint,
            "corpus_manifest_fingerprint": self.corpus_manifest_fingerprint,
            "source_pdf_fingerprint": self.source_pdf_fingerprint,
            "rule_algorithm_fingerprint": self.rule_algorithm_fingerprint,
            "strategy_engine_build_fingerprint": (
                self.strategy_engine_build_fingerprint
            ),
            "scanner_algorithm_fingerprint": (
                self.scanner_algorithm_fingerprint
            ),
            "structure_algorithm_fingerprint": (
                self.structure_algorithm_fingerprint
            ),
            "universe_policy_fingerprint": self.universe_policy_fingerprint,
            "monitor_policy_fingerprint": self.monitor_policy_fingerprint,
            "review_provider": self.review_provider,
            "review_model": self.review_model,
            "review_prompt_version": self.review_prompt_version,
            "review_schema_fingerprint": self.review_schema_fingerprint,
            "review_runtime_policy_fingerprint": (
                self.review_runtime_policy_fingerprint
            ),
            "execution_policy_fingerprint": self.execution_policy_fingerprint,
            "fee_schedule_fingerprint": self.fee_schedule_fingerprint,
            "initial_cash": format(self.initial_cash, "f"),
            "account_algorithm_fingerprint": (
                self.account_algorithm_fingerprint
            ),
            "risk_policy_fingerprint": self.risk_policy_fingerprint,
            "exit_policy_fingerprint": self.exit_policy_fingerprint,
            "exit_algorithm_fingerprint": self.exit_algorithm_fingerprint,
            "calendar_fingerprint": self.calendar_fingerprint,
            "bar_provider_fingerprint": self.bar_provider_fingerprint,
            "bar_schema_fingerprint": self.bar_schema_fingerprint,
        }

    @property
    def fingerprint(self) -> str:
        return sha256_json(self.to_payload())


@dataclass(frozen=True, slots=True)
class StrategyRunStoreBinding:
    run_id: str
    epoch: int
    strategy_run_fingerprint: str
    identity_sha256: str
    store_role: str
    store_instance_id: str
    bound_at: datetime

    def __post_init__(self) -> None:
        _required_text(self.run_id, "run_id")
        _required_epoch(self.epoch)
        _required_fingerprint(
            self.strategy_run_fingerprint,
            "strategy_run_fingerprint",
        )
        _required_fingerprint(self.identity_sha256, "identity_sha256")
        if self.store_role not in STRATEGY_RUN_STORE_ROLES:
            raise ValueError("store_role is invalid")
        _required_text(self.store_instance_id, "store_instance_id")
        object.__setattr__(
            self,
            "bound_at",
            normalize_datetime(self.bound_at, "bound_at"),
        )

    def checksum_payload(self) -> dict[str, object]:
        return {
            "schema_version": 1,
            "run_id": self.run_id,
            "epoch": self.epoch,
            "strategy_run_fingerprint": self.strategy_run_fingerprint,
            "identity_sha256": self.identity_sha256,
            "store_role": self.store_role,
            "store_instance_id": self.store_instance_id,
            "bound_at": self.bound_at.isoformat(),
        }


@dataclass(frozen=True, slots=True)
class StrategyRunEpochRecord:
    epoch: int
    run_id: str
    strategy_run_fingerprint: str
    identity: StrategyRunIdentity
    identity_sha256: str
    status: str
    predecessor_run_id: str | None
    started_at: datetime
    activated_at: datetime | None
    ended_at: datetime | None


@dataclass(frozen=True, slots=True)
class StrategyRunMutationLease:
    lease_id: str
    run_id: str
    epoch: int
    strategy_run_fingerprint: str
    operation: str
    owner_token: str = field(repr=False, compare=False)
    acquired_at: datetime

    def __post_init__(self) -> None:
        _required_text(self.lease_id, "lease_id")
        _required_text(self.run_id, "run_id")
        _required_epoch(self.epoch)
        _required_fingerprint(
            self.strategy_run_fingerprint,
            "strategy_run_fingerprint",
        )
        _required_text(self.operation, "operation")
        _required_text(self.owner_token, "owner_token")
        object.__setattr__(
            self,
            "acquired_at",
            normalize_datetime(self.acquired_at, "acquired_at"),
        )

    def to_payload(self) -> dict[str, object]:
        return {
            "lease_id": self.lease_id,
            "run_id": self.run_id,
            "epoch": self.epoch,
            "strategy_run_fingerprint": self.strategy_run_fingerprint,
            "operation": self.operation,
            "acquired_at": self.acquired_at.isoformat(),
        }


@dataclass(frozen=True, slots=True)
class _PersistedStrategyRunMutationLease:
    lease_id: str
    run_id: str
    epoch: int
    strategy_run_fingerprint: str
    operation: str
    owner_token_sha256: str
    acquired_at: datetime

    def __post_init__(self) -> None:
        _required_text(self.lease_id, "lease_id")
        _required_text(self.run_id, "run_id")
        _required_epoch(self.epoch)
        _required_fingerprint(
            self.strategy_run_fingerprint,
            "strategy_run_fingerprint",
        )
        _required_text(self.operation, "operation")
        _required_fingerprint(
            self.owner_token_sha256,
            "owner_token_sha256",
        )
        object.__setattr__(
            self,
            "acquired_at",
            normalize_datetime(self.acquired_at, "acquired_at"),
        )

    def to_payload(self) -> dict[str, object]:
        return {
            "lease_id": self.lease_id,
            "run_id": self.run_id,
            "epoch": self.epoch,
            "strategy_run_fingerprint": self.strategy_run_fingerprint,
            "operation": self.operation,
            "acquired_at": self.acquired_at.isoformat(),
        }


@dataclass(frozen=True, slots=True)
class _MutationLeaseContextEntry:
    registry_path: Path
    run_id: str
    strategy_run_fingerprint: str
    lease: StrategyRunMutationLease
    execution_thread_id: int
    execution_task: object | None
    closing: Event


@dataclass(frozen=True, slots=True)
class _MutationLeaseValidationCache:
    max_event_sequence: int
    history_head_sha256: str
    active_leases: tuple[_PersistedStrategyRunMutationLease, ...]
    file_signature: tuple[int, int, int, int, int] | None


_MUTATION_LEASE_CONTEXT: ContextVar[
    tuple[_MutationLeaseContextEntry, ...]
] = ContextVar("paper_strategy_run_mutation_lease_context", default=())


def _mutation_execution_context() -> tuple[int, object | None]:
    try:
        task = current_task()
    except RuntimeError:
        task = None
    return get_ident(), task


@dataclass(frozen=True, slots=True)
class ActiveStrategyRun:
    epoch: int
    run_id: str
    strategy_run_fingerprint: str
    identity: StrategyRunIdentity
    started_at: datetime
    store_bindings: Mapping[str, StrategyRunStoreBinding]
    registry_path: Path
    store_paths: Mapping[str, Path]
    registry_file_identity: tuple[int, int]
    store_file_identities: Mapping[str, tuple[int, int]]
    _registry: SQLiteStrategyRunRegistry = field(repr=False, compare=False)
    evidence_scope: str = "current_epoch_only"

    def __post_init__(self) -> None:
        bindings = dict(self.store_bindings)
        if set(bindings) != set(STRATEGY_RUN_STORE_ROLES):
            raise ValueError("all strategy-run store bindings are required")
        if any(
            binding.store_role != role
            or binding.run_id != self.run_id
            or binding.epoch != self.epoch
            or binding.strategy_run_fingerprint
            != self.strategy_run_fingerprint
            for role, binding in bindings.items()
        ):
            raise ValueError("strategy-run store bindings are inconsistent")
        _required_epoch(self.epoch)
        _required_text(self.run_id, "run_id")
        _required_fingerprint(
            self.strategy_run_fingerprint,
            "strategy_run_fingerprint",
        )
        if self.strategy_run_fingerprint != self.identity.fingerprint:
            raise ValueError("strategy-run identity fingerprint is inconsistent")
        registry_path = Path(self.registry_path).expanduser().absolute()
        paths = {
            role: Path(path).expanduser().absolute()
            for role, path in dict(self.store_paths).items()
        }
        file_identities = dict(self.store_file_identities)
        if (
            set(paths) != set(STRATEGY_RUN_STORE_ROLES)
            or set(file_identities) != set(STRATEGY_RUN_STORE_ROLES)
            or any(
                not isinstance(identity, tuple)
                or len(identity) != 2
                or any(
                    isinstance(value, bool) or not isinstance(value, int)
                    for value in identity
                )
                for identity in file_identities.values()
            )
        ):
            raise ValueError("strategy-run file identities are incomplete")
        if (
            not isinstance(self.registry_file_identity, tuple)
            or len(self.registry_file_identity) != 2
            or any(
                isinstance(value, bool) or not isinstance(value, int)
                for value in self.registry_file_identity
            )
        ):
            raise ValueError("strategy-run registry file identity is invalid")
        object.__setattr__(self, "store_bindings", MappingProxyType(bindings))
        object.__setattr__(self, "registry_path", registry_path)
        object.__setattr__(self, "store_paths", MappingProxyType(paths))
        object.__setattr__(
            self,
            "store_file_identities",
            MappingProxyType(file_identities),
        )
        object.__setattr__(
            self,
            "started_at",
            normalize_datetime(self.started_at, "started_at"),
        )
        if self.evidence_scope != "current_epoch_only":
            raise ValueError("unsupported evidence_scope")
        if self._registry.path != registry_path:
            raise ValueError("strategy-run registry instance is inconsistent")

    def status_payload(self) -> dict[str, object]:
        active_leases = _revalidate_active_strategy_run(self)
        return {
            "run_id": self.run_id,
            "epoch": self.epoch,
            "fingerprint": self.strategy_run_fingerprint,
            "state": "active",
            "started_at": self.started_at.isoformat(),
            "evidence_scope": self.evidence_scope,
            "store_bindings_complete": True,
            "switch_capability": STRATEGY_RUN_SWITCH_CAPABILITY,
            "rolling_switch_supported": False,
            "mutation_lease_protocol": (
                STRATEGY_RUN_MUTATION_LEASE_PROTOCOL
            ),
            "inflight_mutation_count": len(active_leases),
            "mutations_drained": not active_leases,
            "identity": self.identity.to_payload(),
        }

    def require_current_mutation_lease(self) -> None:
        """Require this execution context to hold this run's physical lease."""

        current = _MUTATION_LEASE_CONTEXT.get()
        if not current:
            raise StrategyRunIntegrityError(
                "strategy_run_mutation_lease_required"
            )
        entry = current[-1]
        execution_thread_id, execution_task = _mutation_execution_context()
        lease = entry.lease
        if (
            entry.registry_path != self.registry_path
            or entry.run_id != self.run_id
            or entry.strategy_run_fingerprint
            != self.strategy_run_fingerprint
            or entry.execution_thread_id != execution_thread_id
            or entry.execution_task is not execution_task
            or entry.closing.is_set()
            or lease.run_id != self.run_id
            or lease.epoch != self.epoch
            or lease.strategy_run_fingerprint
            != self.strategy_run_fingerprint
        ):
            raise StrategyRunIntegrityError(
                "strategy_run_mutation_lease_required"
            )

    def acquire_mutation_lease(
        self,
        operation: str,
        *,
        now: datetime | None = None,
    ) -> StrategyRunMutationLease:
        return self._registry.acquire_mutation_lease(
            self,
            operation=operation,
            now=now,
        )

    def release_mutation_lease(
        self,
        lease: StrategyRunMutationLease,
        *,
        now: datetime | None = None,
    ) -> None:
        self._registry.release_mutation_lease(
            self,
            lease=lease,
            now=now,
        )

    @contextmanager
    def mutation_lease(
        self,
        operation: str,
    ) -> Iterator[StrategyRunMutationLease]:
        current = _MUTATION_LEASE_CONTEXT.get()
        execution_thread_id, execution_task = _mutation_execution_context()
        if current:
            outer = current[-1]
            if (
                outer.registry_path == self.registry_path
                and outer.run_id == self.run_id
                and outer.strategy_run_fingerprint
                == self.strategy_run_fingerprint
                and outer.execution_thread_id == execution_thread_id
                and outer.execution_task is execution_task
                and not outer.closing.is_set()
            ):
                token = _MUTATION_LEASE_CONTEXT.set(current + (outer,))
                try:
                    yield outer.lease
                finally:
                    _MUTATION_LEASE_CONTEXT.reset(token)
                return
        lease = self.acquire_mutation_lease(operation)
        entry = _MutationLeaseContextEntry(
            registry_path=self.registry_path,
            run_id=self.run_id,
            strategy_run_fingerprint=self.strategy_run_fingerprint,
            lease=lease,
            execution_thread_id=execution_thread_id,
            execution_task=execution_task,
            closing=Event(),
        )
        token = _MUTATION_LEASE_CONTEXT.set(current + (entry,))
        try:
            yield lease
        finally:
            _MUTATION_LEASE_CONTEXT.reset(token)
            entry.closing.set()
            self.release_mutation_lease(lease)

    def mutation_lease_diagnostics(self) -> dict[str, object]:
        return self._registry.mutation_lease_diagnostics(self.run_id)


@dataclass(slots=True)
class StrategyRunBootstrapReservation:
    """Exact durable ownership required before a strategy-store constructor."""

    record: StrategyRunEpochRecord
    store_bindings: Mapping[str, StrategyRunStoreBinding]
    store_paths: Mapping[str, Path]
    registry_path: Path
    _registry: SQLiteStrategyRunRegistry = field(repr=False)
    _active: ActiveStrategyRun | None = field(default=None, repr=False)
    _initialized_roles: set[str] = field(default_factory=set, repr=False)
    _closed: bool = field(default=False, repr=False)

    def __post_init__(self) -> None:
        bindings = dict(self.store_bindings)
        paths = {
            role: Path(path).expanduser().absolute()
            for role, path in dict(self.store_paths).items()
        }
        if set(bindings) != set(STRATEGY_RUN_STORE_ROLES) or set(paths) != set(
            STRATEGY_RUN_STORE_ROLES
        ):
            raise ValueError("bootstrap reservation store set is incomplete")
        if any(
            binding.run_id != self.record.run_id
            or binding.epoch != self.record.epoch
            or binding.strategy_run_fingerprint
            != self.record.strategy_run_fingerprint
            or binding.identity_sha256 != self.record.identity_sha256
            or binding.store_role != role
            for role, binding in bindings.items()
        ):
            raise ValueError("bootstrap reservation bindings are inconsistent")
        self.store_bindings = MappingProxyType(bindings)
        self.store_paths = MappingProxyType(paths)
        self.registry_path = Path(self.registry_path).expanduser().absolute()

    def initialize_store(
        self,
        role: str,
        path: str | Path,
        factory: Callable[[], object],
    ) -> object:
        if self._closed:
            raise StrategyRunIntegrityError(
                "strategy_run_bootstrap_reservation_closed"
            )
        if role not in STRATEGY_RUN_STORE_ROLES:
            raise ValueError("store_role is invalid")
        normalized = Path(path).expanduser().absolute()
        if normalized != self.store_paths[role]:
            raise StrategyRunIntegrityError(
                "strategy_run_store_binding_mismatch"
            )
        if not callable(factory):
            raise TypeError("store factory must be callable")
        expected = self.store_bindings[role]
        if read_strategy_run_binding(normalized) != expected:
            raise StrategyRunIntegrityError(
                "strategy_run_store_binding_mismatch"
            )
        store = factory()
        actual_path = getattr(store, "path", None)
        if (
            not isinstance(actual_path, (str, Path))
            or Path(actual_path).expanduser().absolute() != normalized
            or read_strategy_run_binding(normalized) != expected
        ):
            raise StrategyRunIntegrityError(
                "strategy_run_bootstrap_store_factory_mismatch"
            )
        if not _store_schema_initialized(normalized, role):
            raise StrategyRunIntegrityError(
                f"strategy_run_bootstrap_store_schema_invalid:{role}"
            )
        self._initialized_roles.add(role)
        return store

    def activate(self, *, now: datetime) -> ActiveStrategyRun:
        if self._closed:
            raise StrategyRunIntegrityError(
                "strategy_run_bootstrap_reservation_closed"
            )
        if self._initialized_roles != set(STRATEGY_RUN_STORE_ROLES):
            raise StrategyRunIntegrityError(
                "strategy_run_bootstrap_store_initialization_incomplete"
            )
        if self._active is None:
            self._active = self._registry.bind_and_activate(
                self.record,
                store_paths=self.store_paths,
                now=now,
            )
        return self._active

    def close(self) -> None:
        self._closed = True


def _file_identity(path: Path, *, reason: str) -> tuple[int, int]:
    try:
        stat = path.stat()
    except OSError as exc:
        raise StrategyRunIntegrityError(reason) from exc
    if not path.is_file():
        raise StrategyRunIntegrityError(reason)
    return (int(stat.st_dev), int(stat.st_ino))


def _database_file_signature(
    path: Path,
) -> tuple[int, int, int, int, int] | None:
    try:
        stat = path.stat()
    except OSError:
        return None
    if not path.is_file():
        return None
    return (
        int(stat.st_dev),
        int(stat.st_ino),
        int(stat.st_size),
        int(stat.st_mtime_ns),
        int(stat.st_ctime_ns),
    )


def _make_active_strategy_run(
    *,
    record: StrategyRunEpochRecord,
    bindings: Mapping[str, StrategyRunStoreBinding],
    registry_path: Path,
    store_paths: Mapping[str, Path],
    registry: SQLiteStrategyRunRegistry,
) -> ActiveStrategyRun:
    normalized_paths = {
        role: Path(store_paths[role]).expanduser().absolute()
        for role in STRATEGY_RUN_STORE_ROLES
    }
    return ActiveStrategyRun(
        epoch=record.epoch,
        run_id=record.run_id,
        strategy_run_fingerprint=record.strategy_run_fingerprint,
        identity=record.identity,
        started_at=record.started_at,
        store_bindings=bindings,
        registry_path=registry_path,
        store_paths=normalized_paths,
        registry_file_identity=_file_identity(
            registry_path,
            reason="strategy_run_registry_file_replaced",
        ),
        store_file_identities={
            role: _file_identity(
                path,
                reason="strategy_run_store_file_replaced",
            )
            for role, path in normalized_paths.items()
        },
        _registry=registry,
    )


def _connect(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(str(path), timeout=10)
    connection.execute("PRAGMA busy_timeout = 10000")
    return connection


def _table_exists(connection: sqlite3.Connection, name: str) -> bool:
    return (
        connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
            (name,),
        ).fetchone()
        is not None
    )


def _count_if_present(connection: sqlite3.Connection, table: str) -> int:
    if not _table_exists(connection, table):
        return 0
    quoted = '"' + table.replace('"', '""') + '"'
    return int(connection.execute(f"SELECT COUNT(*) FROM {quoted}").fetchone()[0])


def _user_tables(connection: sqlite3.Connection) -> frozenset[str]:
    return frozenset(
        row[0]
        for row in connection.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
        ).fetchall()
        if isinstance(row[0], str)
    )


def _ledger_state(connection: sqlite3.Connection) -> dict[str, object]:
    if not _table_exists(connection, "paper_ledger"):
        raise StrategyRunIntegrityError("strategy_run_store_schema_invalid:ledger")
    row = connection.execute(
        "SELECT revision, state_json FROM paper_ledger WHERE singleton_id = 1"
    ).fetchone()
    if row is None or isinstance(row[0], bool) or not isinstance(row[0], int):
        raise StrategyRunIntegrityError("strategy_run_store_schema_invalid:ledger")
    try:
        state = json.loads(row[1])
    except (TypeError, json.JSONDecodeError) as exc:
        raise StrategyRunIntegrityError(
            "strategy_run_store_schema_invalid:ledger"
        ) from exc
    if not isinstance(state, dict) or state.get("revision") != row[0]:
        raise StrategyRunIntegrityError("strategy_run_store_schema_invalid:ledger")
    return state


def _store_has_evidence_connection(
    connection: sqlite3.Connection,
    role: str,
) -> bool:
    if role == "ledger":
        state = _ledger_state(connection)
        for field_name in (
            "intents",
            "fills",
            "lots",
            "processed_bar_ids",
            "bar_cursors",
        ):
            value = state.get(field_name)
            if not isinstance(value, list):
                raise StrategyRunIntegrityError(
                    "strategy_run_store_schema_invalid:ledger"
                )
            if value:
                return True
        return bool(
            _count_if_present(connection, "paper_buying_power_reservation")
            or _count_if_present(connection, "paper_trusted_admission")
            or _count_if_present(connection, "paper_execution_policy")
        )
    if role == "bar":
        evidence_tables = (
            "trusted_paper_bar",
            "trusted_paper_bar_segment",
            "trusted_paper_bar_segment_member",
            "trusted_paper_bar_cycle",
            "trusted_paper_bar_attempt",
            "trusted_signal_observation_cycle",
            "trusted_signal_first_observation",
            "trusted_signal_segment_observation",
            "trusted_paper_bar_calendar_preflight_watermark",
            "trusted_paper_exit_manifest",
        )
        if any(
            _count_if_present(connection, table) for table in evidence_tables
        ):
            return True
        default_payload = {
            "schema_version": 1,
            "event_count": 0,
            "max_sequence": 0,
            "history_head_sha256": None,
        }
        if _table_exists(connection, "trusted_signal_observation_log_state"):
            rows = connection.execute(
                """
                SELECT singleton_id, event_count, max_sequence,
                       history_head_sha256, payload_sha256
                FROM trusted_signal_observation_log_state
                """
            ).fetchall()
            if len(rows) != 1 or rows[0] != (
                1,
                0,
                0,
                None,
                sha256_json(default_payload),
            ):
                raise StrategyRunIntegrityError(
                    "strategy_run_store_schema_invalid:bar"
                )
        if _table_exists(connection, "trusted_paper_exit_manifest_log_state"):
            rows = connection.execute(
                """
                SELECT singleton_id, event_count, max_sequence,
                       history_head_sha256, payload_sha256
                FROM trusted_paper_exit_manifest_log_state
                """
            ).fetchall()
            if len(rows) != 1:
                raise StrategyRunIntegrityError(
                    "strategy_run_store_schema_invalid:bar"
                )
            row = rows[0]
            if row == (
                1,
                0,
                0,
                None,
                sha256_json(default_payload),
            ):
                return False
            state_payload = {
                "schema_version": 1,
                "event_count": row[1],
                "max_sequence": row[2],
                "history_head_sha256": row[3],
            }
            if (
                row[0] != 1
                or isinstance(row[1], bool)
                or not isinstance(row[1], int)
                or row[1] <= 0
                or row[2] != row[1]
                or not isinstance(row[3], str)
                or _FINGERPRINT_RE.fullmatch(row[3]) is None
                or row[4] != sha256_json(state_payload)
            ):
                raise StrategyRunIntegrityError(
                    "strategy_run_store_schema_invalid:bar"
                )
            return True
        return False
    tables = {
        "risk": (
            "paper_risk_state",
            "paper_exit_coverage",
            "paper_risk_authority_binding",
        ),
        "exit": ("exit_evaluations",),
    }[role]
    return any(_count_if_present(connection, table) for table in tables)


def _store_has_evidence(path: Path, role: str) -> bool:
    if not path.is_file():
        raise StrategyRunIntegrityError(f"strategy_run_store_missing:{role}")
    with _connect(path) as connection:
        return _store_has_evidence_connection(connection, role)


def _unbound_store_has_evidence(path: Path, role: str) -> bool:
    if not path.is_file():
        return False
    with _connect(path) as connection:
        if not _user_tables(connection):
            return False
        return _store_has_evidence_connection(connection, role)


def _store_schema_initialized(path: Path, role: str) -> bool:
    if not path.is_file():
        return False
    required_tables = {
        "ledger": frozenset({"paper_ledger"}),
        "bar": frozenset(
            {
                "trusted_paper_bar",
                "trusted_paper_bar_cycle",
                "trusted_signal_observation_log_state",
            }
        ),
        "risk": frozenset({"paper_risk_state", "paper_exit_coverage"}),
        "exit": frozenset({"exit_evaluation_meta", "exit_evaluations"}),
    }[role]
    with _connect(path) as connection:
        return required_tables.issubset(_user_tables(connection))


def _previous_ledger_is_flat(path: Path) -> bool:
    if not path.is_file():
        return False
    with _connect(path) as connection:
        state = _ledger_state(connection)
        lots = state.get("lots")
        intents = state.get("intents")
        if not isinstance(lots, list) or not isinstance(intents, list):
            return False
        if lots:
            return False
        if any(
            not isinstance(intent, dict)
            or isinstance(intent.get("remaining_shares"), bool)
            or not isinstance(intent.get("remaining_shares"), int)
            or intent["remaining_shares"] > 0
            for intent in intents
        ):
            return False
        return not _count_if_present(
            connection,
            "paper_buying_power_reservation",
        )


def _binding_checksum(binding: StrategyRunStoreBinding) -> str:
    return sha256_json(binding.checksum_payload())


def _registry_store_checksum(
    *,
    run_id: str,
    store_role: str,
    store_path: Path,
    store_instance_id: str,
) -> str:
    return sha256_json(
        {
            "schema_version": 1,
            "run_id": run_id,
            "store_role": store_role,
            "store_path": str(store_path.absolute()),
            "store_instance_id": store_instance_id,
        }
    )


_EMPTY_HISTORY_HEAD = sha256_json(
    {"schema_version": 1, "history": "empty"}
)
_EMPTY_MUTATION_LEASE_HEAD = sha256_json(
    {"schema_version": 1, "mutation_lease_history": "empty"}
)
_MUTATION_LEASE_GUARD_SQL = {
    "paper_strategy_run_mutation_lease_delete_guard": """
        CREATE TRIGGER paper_strategy_run_mutation_lease_delete_guard
        AFTER DELETE ON paper_strategy_run_mutation_lease
        BEGIN
            UPDATE paper_strategy_run_mutation_lease_meta
            SET meta_sha256 = 'invalidated-by-lease-delete'
            WHERE singleton_id = 1;
        END
    """,
    "paper_strategy_run_mutation_lease_update_guard": """
        CREATE TRIGGER paper_strategy_run_mutation_lease_update_guard
        AFTER UPDATE ON paper_strategy_run_mutation_lease
        BEGIN
            UPDATE paper_strategy_run_mutation_lease_meta
            SET meta_sha256 = 'invalidated-by-lease-update'
            WHERE singleton_id = 1;
        END
    """,
    "paper_strategy_run_mutation_lease_insert_guard": """
        CREATE TRIGGER paper_strategy_run_mutation_lease_insert_guard
        BEFORE INSERT ON paper_strategy_run_mutation_lease
        WHEN NEW.event_sequence != COALESCE(
            (
                SELECT max_event_sequence + 1
                FROM paper_strategy_run_mutation_lease_meta
                WHERE singleton_id = 1
            ),
            -1
        )
        BEGIN
            SELECT RAISE(ABORT, 'mutation-lease-append-order-invalid');
        END
    """,
}


def _history_meta_checksum(max_epoch: int, history_head: str) -> str:
    return sha256_json(
        {
            "schema_version": 1,
            "max_epoch": max_epoch,
            "history_head_sha256": history_head,
        }
    )


def _mutation_lease_meta_checksum(
    max_event_sequence: int,
    history_head: str,
) -> str:
    return sha256_json(
        {
            "schema_version": 1,
            "max_event_sequence": max_event_sequence,
            "history_head_sha256": history_head,
        }
    )


def _mutation_lease_event_checksum(
    *,
    event_sequence: int,
    previous_event_sha256: str,
    lease: _PersistedStrategyRunMutationLease,
    event_type: str,
    occurred_at: datetime,
) -> str:
    return sha256_json(
        {
            "schema_version": 1,
            "event_sequence": event_sequence,
            "previous_event_sha256": previous_event_sha256,
            **lease.to_payload(),
            "owner_token_sha256": lease.owner_token_sha256,
            "event_type": event_type,
            "occurred_at": occurred_at.isoformat(),
        }
    )


def _next_history_head(
    previous_head: str,
    record: StrategyRunEpochRecord,
    stores: list[tuple[str, Path, str, str]],
) -> str:
    return sha256_json(
        {
            "schema_version": 1,
            "previous_head_sha256": previous_head,
            "epoch": record.epoch,
            "run_id": record.run_id,
            "strategy_run_fingerprint": record.strategy_run_fingerprint,
            "identity_sha256": record.identity_sha256,
            "predecessor_run_id": record.predecessor_run_id,
            "started_at": record.started_at.isoformat(),
            "stores": [
                {
                    "store_role": role,
                    "store_path": str(path.absolute()),
                    "store_instance_id": instance_id,
                    "registry_binding_sha256": registry_checksum,
                }
                for role, path, instance_id, registry_checksum in sorted(
                    stores,
                    key=lambda item: item[0],
                )
            ],
        }
    )


def read_strategy_run_binding(
    path: str | Path,
) -> StrategyRunStoreBinding | None:
    resolved = Path(path).expanduser().absolute()
    if not resolved.is_file():
        return None
    with _connect(resolved) as connection:
        if not _table_exists(connection, "paper_strategy_run_binding"):
            return None
        rows = connection.execute(
            """
            SELECT run_id, epoch, strategy_run_fingerprint,
                   identity_sha256, store_role, store_instance_id,
                   bound_at, binding_sha256
            FROM paper_strategy_run_binding
            """
        ).fetchall()
    if len(rows) != 1:
        raise StrategyRunIntegrityError("strategy_run_store_binding_invalid")
    row = rows[0]
    try:
        binding = StrategyRunStoreBinding(
            run_id=row[0],
            epoch=row[1],
            strategy_run_fingerprint=row[2],
            identity_sha256=row[3],
            store_role=row[4],
            store_instance_id=row[5],
            bound_at=datetime.fromisoformat(row[6]),
        )
    except (TypeError, ValueError) as exc:
        raise StrategyRunIntegrityError(
            "strategy_run_store_binding_invalid"
        ) from exc
    if row[7] != _binding_checksum(binding):
        raise StrategyRunIntegrityError(
            "strategy_run_store_binding_checksum_mismatch"
        )
    return binding


def _bind_store(
    path: Path,
    *,
    role: str,
    record: StrategyRunEpochRecord,
    store_instance_id: str,
) -> StrategyRunStoreBinding:
    binding = StrategyRunStoreBinding(
        run_id=record.run_id,
        epoch=record.epoch,
        strategy_run_fingerprint=record.strategy_run_fingerprint,
        identity_sha256=record.identity_sha256,
        store_role=role,
        store_instance_id=store_instance_id,
        bound_at=record.started_at,
    )
    existing = read_strategy_run_binding(path)
    if existing is not None:
        if existing != binding:
            raise StrategyRunIntegrityError(
                "strategy_run_store_binding_mismatch"
            )
        return existing
    path.parent.mkdir(parents=True, exist_ok=True)
    with _connect(path) as connection:
        connection.execute("BEGIN IMMEDIATE")
        try:
            if _table_exists(connection, "paper_strategy_run_binding"):
                raise StrategyRunIntegrityError(
                    "strategy_run_store_binding_mismatch"
                )
            if _user_tables(connection) and _store_has_evidence_connection(
                connection,
                role,
            ):
                raise StrategyRunIntegrityError(f"legacy_unbound:{role}")
            connection.execute(
                """
                CREATE TABLE paper_strategy_run_binding (
                    singleton_id INTEGER PRIMARY KEY CHECK (singleton_id = 1),
                    run_id TEXT NOT NULL,
                    epoch INTEGER NOT NULL CHECK (epoch > 0),
                    strategy_run_fingerprint TEXT NOT NULL,
                    identity_sha256 TEXT NOT NULL,
                    store_role TEXT NOT NULL,
                    store_instance_id TEXT NOT NULL,
                    bound_at TEXT NOT NULL,
                    binding_sha256 TEXT NOT NULL
                )
                """
            )
            connection.execute(
                """
                INSERT INTO paper_strategy_run_binding (
                    singleton_id, run_id, epoch,
                    strategy_run_fingerprint, identity_sha256,
                    store_role, store_instance_id, bound_at,
                    binding_sha256
                ) VALUES (1, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    binding.run_id,
                    binding.epoch,
                    binding.strategy_run_fingerprint,
                    binding.identity_sha256,
                    binding.store_role,
                    binding.store_instance_id,
                    binding.bound_at.isoformat(),
                    _binding_checksum(binding),
                ),
            )
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
    return binding


class SQLiteStrategyRunRegistry:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path).expanduser().absolute()
        if self.path.exists() and not self.path.is_file():
            raise ValueError("strategy run registry path must be a file")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = RLock()
        self._mutation_lease_cache: _MutationLeaseValidationCache | None = None
        self._initialize()
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN")
            self._validated_registry_state(
                connection,
                force_full_mutation=True,
            )

    def _connect(self) -> sqlite3.Connection:
        return _connect(self.path)

    def _initialize(self) -> None:
        with self._lock, self._connect() as connection:
            had_registry_schema = any(
                _table_exists(connection, table)
                for table in (
                    "paper_strategy_run_epoch",
                    "paper_strategy_run_store",
                    "paper_strategy_run_meta",
                )
            )
            mutation_schema = tuple(
                _table_exists(connection, table)
                for table in (
                    "paper_strategy_run_mutation_lease",
                    "paper_strategy_run_mutation_lease_meta",
                )
            )
            if (
                any(mutation_schema) != all(mutation_schema)
                or had_registry_schema != all(mutation_schema)
            ):
                raise StrategyRunIntegrityError(
                    "strategy_run_mutation_lease_schema_missing"
                )
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS paper_strategy_run_epoch (
                    epoch INTEGER PRIMARY KEY,
                    run_id TEXT NOT NULL UNIQUE,
                    strategy_run_fingerprint TEXT NOT NULL,
                    identity_json TEXT NOT NULL,
                    identity_sha256 TEXT NOT NULL,
                    status TEXT NOT NULL CHECK (
                        status IN ('initializing', 'active', 'closed', 'blocked')
                    ),
                    predecessor_run_id TEXT,
                    started_at TEXT NOT NULL,
                    activated_at TEXT,
                    ended_at TEXT
                );
                CREATE UNIQUE INDEX IF NOT EXISTS ux_strategy_run_active
                ON paper_strategy_run_epoch (status) WHERE status = 'active';
                CREATE TABLE IF NOT EXISTS paper_strategy_run_store (
                    run_id TEXT NOT NULL,
                    store_role TEXT NOT NULL,
                    store_path TEXT NOT NULL,
                    store_instance_id TEXT NOT NULL,
                    registry_binding_sha256 TEXT NOT NULL,
                    PRIMARY KEY (run_id, store_role),
                    UNIQUE (store_role, store_instance_id),
                    FOREIGN KEY (run_id) REFERENCES paper_strategy_run_epoch (run_id)
                );
                CREATE TABLE IF NOT EXISTS paper_strategy_run_meta (
                    singleton_id INTEGER PRIMARY KEY CHECK (singleton_id = 1),
                    max_epoch INTEGER NOT NULL CHECK (max_epoch >= 0),
                    history_head_sha256 TEXT NOT NULL,
                    meta_sha256 TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS paper_strategy_run_mutation_lease (
                    event_sequence INTEGER PRIMARY KEY CHECK (
                        event_sequence > 0
                    ),
                    lease_id TEXT NOT NULL,
                    run_id TEXT NOT NULL,
                    epoch INTEGER NOT NULL CHECK (epoch > 0),
                    strategy_run_fingerprint TEXT NOT NULL,
                    operation TEXT NOT NULL,
                    owner_token_sha256 TEXT NOT NULL,
                    acquired_at TEXT NOT NULL,
                    event_type TEXT NOT NULL CHECK (
                        event_type IN ('acquire', 'release')
                    ),
                    occurred_at TEXT NOT NULL,
                    previous_event_sha256 TEXT NOT NULL,
                    event_sha256 TEXT NOT NULL,
                    UNIQUE (lease_id, event_type),
                    FOREIGN KEY (run_id)
                    REFERENCES paper_strategy_run_epoch (run_id)
                );
                CREATE TABLE IF NOT EXISTS paper_strategy_run_mutation_lease_meta (
                    singleton_id INTEGER PRIMARY KEY CHECK (singleton_id = 1),
                    max_event_sequence INTEGER NOT NULL CHECK (
                        max_event_sequence >= 0
                    ),
                    history_head_sha256 TEXT NOT NULL,
                    meta_sha256 TEXT NOT NULL
                );
                CREATE TRIGGER IF NOT EXISTS
                    paper_strategy_run_mutation_lease_delete_guard
                AFTER DELETE ON paper_strategy_run_mutation_lease
                BEGIN
                    UPDATE paper_strategy_run_mutation_lease_meta
                    SET meta_sha256 = 'invalidated-by-lease-delete'
                    WHERE singleton_id = 1;
                END;
                CREATE TRIGGER IF NOT EXISTS
                    paper_strategy_run_mutation_lease_update_guard
                AFTER UPDATE ON paper_strategy_run_mutation_lease
                BEGIN
                    UPDATE paper_strategy_run_mutation_lease_meta
                    SET meta_sha256 = 'invalidated-by-lease-update'
                    WHERE singleton_id = 1;
                END;
                CREATE TRIGGER IF NOT EXISTS
                    paper_strategy_run_mutation_lease_insert_guard
                BEFORE INSERT ON paper_strategy_run_mutation_lease
                WHEN NEW.event_sequence != COALESCE(
                    (
                        SELECT max_event_sequence + 1
                        FROM paper_strategy_run_mutation_lease_meta
                        WHERE singleton_id = 1
                    ),
                    -1
                )
                BEGIN
                    SELECT RAISE(
                        ABORT,
                        'mutation-lease-append-order-invalid'
                    );
                END;
                """
            )
            meta_rows = connection.execute(
                """
                SELECT max_epoch, history_head_sha256, meta_sha256
                FROM paper_strategy_run_meta
                """
            ).fetchall()
            if not meta_rows:
                if had_registry_schema:
                    raise StrategyRunIntegrityError(
                        "strategy_run_history_invalid"
                    )
                connection.execute(
                    """
                    INSERT INTO paper_strategy_run_meta (
                        singleton_id, max_epoch,
                        history_head_sha256, meta_sha256
                    ) VALUES (1, 0, ?, ?)
                    """,
                    (
                        _EMPTY_HISTORY_HEAD,
                        _history_meta_checksum(0, _EMPTY_HISTORY_HEAD),
                    ),
                )
                meta_rows = [
                    (
                        0,
                        _EMPTY_HISTORY_HEAD,
                        _history_meta_checksum(0, _EMPTY_HISTORY_HEAD),
                    )
                ]
            if len(meta_rows) != 1:
                raise StrategyRunIntegrityError(
                    "strategy_run_history_invalid"
                )
            meta_epoch, meta_head, meta_checksum = meta_rows[0]
            if (
                isinstance(meta_epoch, bool)
                or not isinstance(meta_epoch, int)
                or meta_epoch < 0
                or not isinstance(meta_head, str)
                or _FINGERPRINT_RE.fullmatch(meta_head) is None
                or meta_checksum
                != _history_meta_checksum(meta_epoch, meta_head)
            ):
                raise StrategyRunIntegrityError(
                    "strategy_run_history_invalid"
                )
            mutation_meta_rows = connection.execute(
                """
                SELECT max_event_sequence, history_head_sha256, meta_sha256
                FROM paper_strategy_run_mutation_lease_meta
                """
            ).fetchall()
            if not mutation_meta_rows:
                if had_registry_schema:
                    raise StrategyRunIntegrityError(
                        "strategy_run_mutation_lease_history_invalid"
                    )
                checksum = _mutation_lease_meta_checksum(
                    0,
                    _EMPTY_MUTATION_LEASE_HEAD,
                )
                connection.execute(
                    """
                    INSERT INTO paper_strategy_run_mutation_lease_meta (
                        singleton_id, max_event_sequence,
                        history_head_sha256, meta_sha256
                    ) VALUES (1, 0, ?, ?)
                    """,
                    (_EMPTY_MUTATION_LEASE_HEAD, checksum),
                )
                mutation_meta_rows = [
                    (0, _EMPTY_MUTATION_LEASE_HEAD, checksum)
                ]
            if len(mutation_meta_rows) != 1:
                raise StrategyRunIntegrityError(
                    "strategy_run_mutation_lease_history_invalid"
                )
            mutation_sequence, mutation_head, mutation_checksum = (
                mutation_meta_rows[0]
            )
            if (
                isinstance(mutation_sequence, bool)
                or not isinstance(mutation_sequence, int)
                or mutation_sequence < 0
                or not isinstance(mutation_head, str)
                or _FINGERPRINT_RE.fullmatch(mutation_head) is None
                or mutation_checksum
                != _mutation_lease_meta_checksum(
                    mutation_sequence,
                    mutation_head,
                )
            ):
                raise StrategyRunIntegrityError(
                    "strategy_run_mutation_lease_history_invalid"
                )

    @staticmethod
    def _normalize_store_paths(
        store_paths: Mapping[str, str | Path],
    ) -> dict[str, Path]:
        if not isinstance(store_paths, Mapping) or set(store_paths) != set(
            STRATEGY_RUN_STORE_ROLES
        ):
            raise ValueError("exactly four strategy-run store paths are required")
        normalized = {
            role: Path(store_paths[role]).expanduser().absolute()
            for role in STRATEGY_RUN_STORE_ROLES
        }
        if len(set(normalized.values())) != len(normalized):
            raise ValueError("strategy-run store paths must be distinct")
        return normalized

    @staticmethod
    def _record(row: tuple[object, ...]) -> StrategyRunEpochRecord:
        (
            epoch,
            run_id,
            fingerprint,
            identity_json,
            identity_sha256,
            status,
            predecessor,
            started_at,
            activated_at,
            ended_at,
        ) = row
        if status not in _RUN_STATUSES:
            raise StrategyRunIntegrityError("strategy_run_registry_invalid")
        if not isinstance(identity_json, str) or _text_sha256(
            identity_json
        ) != identity_sha256:
            raise StrategyRunIntegrityError(
                "strategy_run_identity_checksum_mismatch"
            )
        try:
            payload = json.loads(identity_json)
            identity = StrategyRunIdentity(**payload)
            record = StrategyRunEpochRecord(
                epoch=epoch,
                run_id=run_id,
                strategy_run_fingerprint=fingerprint,
                identity=identity,
                identity_sha256=identity_sha256,
                status=status,
                predecessor_run_id=predecessor,
                started_at=datetime.fromisoformat(started_at),
                activated_at=(
                    None
                    if activated_at is None
                    else datetime.fromisoformat(activated_at)
                ),
                ended_at=(
                    None if ended_at is None else datetime.fromisoformat(ended_at)
                ),
            )
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise StrategyRunIntegrityError("strategy_run_registry_invalid") from exc
        if identity.fingerprint != fingerprint:
            raise StrategyRunIntegrityError(
                "strategy_run_identity_fingerprint_mismatch"
            )
        return record

    def list_epochs(self) -> tuple[StrategyRunEpochRecord, ...]:
        with self._lock, self._connect() as connection:
            records, _store_rows, _leases = self._validated_registry_state(
                connection
            )
        return records

    def _validated_registry_state(
        self,
        connection: sqlite3.Connection,
        *,
        force_full_mutation: bool = False,
    ) -> tuple[
        tuple[StrategyRunEpochRecord, ...],
        list[tuple[object, ...]],
        tuple[_PersistedStrategyRunMutationLease, ...],
    ]:
        self._require_mutation_lease_guards(connection)
        rows = connection.execute(
            """
            SELECT epoch, run_id, strategy_run_fingerprint,
                   identity_json, identity_sha256, status,
                   predecessor_run_id, started_at, activated_at, ended_at
            FROM paper_strategy_run_epoch ORDER BY epoch
            """
        ).fetchall()
        store_rows = connection.execute(
            """
            SELECT run_id, store_role, store_path, store_instance_id,
                   registry_binding_sha256
            FROM paper_strategy_run_store
            ORDER BY run_id, store_role
            """
        ).fetchall()
        meta_rows = connection.execute(
            """
            SELECT max_epoch, history_head_sha256, meta_sha256
            FROM paper_strategy_run_meta
            """
        ).fetchall()
        mutation_meta_rows = connection.execute(
            """
            SELECT max_event_sequence, history_head_sha256, meta_sha256
            FROM paper_strategy_run_mutation_lease_meta
            """
        ).fetchall()
        records = tuple(self._record(row) for row in rows)
        self._validate_history(records)
        self._validate_store_history(records, store_rows)
        self._validate_history_anchor(records, store_rows, meta_rows)
        leases = self._validated_mutation_lease_state(
            connection,
            records=records,
            meta_rows=mutation_meta_rows,
            force_full=force_full_mutation,
        )
        return records, store_rows, leases

    @staticmethod
    def _require_mutation_lease_guards(
        connection: sqlite3.Connection,
    ) -> None:
        rows = connection.execute(
            """
            SELECT name, sql
            FROM sqlite_master
            WHERE type = 'trigger'
              AND tbl_name = 'paper_strategy_run_mutation_lease'
            """
        ).fetchall()
        definitions = {
            row[0]: row[1]
            for row in rows
            if len(row) == 2
            and isinstance(row[0], str)
            and isinstance(row[1], str)
        }
        expected = {
            name: re.sub(r"\s+", "", sql).lower().removesuffix(";")
            for name, sql in _MUTATION_LEASE_GUARD_SQL.items()
        }
        actual = {
            name: re.sub(r"\s+", "", definitions.get(name, ""))
            .lower()
            .removesuffix(";")
            for name in expected
        }
        if actual != expected:
            raise StrategyRunIntegrityError(
                "strategy_run_mutation_lease_history_invalid"
            )

    def _validated_mutation_lease_state(
        self,
        connection: sqlite3.Connection,
        *,
        records: tuple[StrategyRunEpochRecord, ...],
        meta_rows: list[tuple[object, ...]],
        force_full: bool,
    ) -> tuple[_PersistedStrategyRunMutationLease, ...]:
        if len(meta_rows) != 1:
            raise StrategyRunIntegrityError(
                "strategy_run_mutation_lease_history_invalid"
            )
        max_sequence, history_head, meta_checksum = meta_rows[0]
        if (
            isinstance(max_sequence, bool)
            or not isinstance(max_sequence, int)
            or max_sequence < 0
            or not isinstance(history_head, str)
            or _FINGERPRINT_RE.fullmatch(history_head) is None
            or meta_checksum
            != _mutation_lease_meta_checksum(max_sequence, history_head)
        ):
            raise StrategyRunIntegrityError(
                "strategy_run_mutation_lease_history_invalid"
            )
        signature = _database_file_signature(self.path)
        cache = self._mutation_lease_cache
        if cache is not None and (
            max_sequence < cache.max_event_sequence
            or (
                max_sequence == cache.max_event_sequence
                and history_head != cache.history_head_sha256
            )
        ):
            raise StrategyRunIntegrityError(
                "strategy_run_mutation_lease_history_invalid"
            )
        same_sequence_file_changed = (
            cache is not None
            and max_sequence == cache.max_event_sequence
            and signature != cache.file_signature
        )
        if (
            not force_full
            and not same_sequence_file_changed
            and signature is not None
            and cache is not None
            and max_sequence >= cache.max_event_sequence
        ):
            anchor_valid = cache.max_event_sequence == 0
            if not anchor_valid:
                anchor = connection.execute(
                    """
                    SELECT event_sha256
                    FROM paper_strategy_run_mutation_lease
                    WHERE event_sequence = ?
                    """,
                    (cache.max_event_sequence,),
                ).fetchone()
                anchor_valid = anchor == (cache.history_head_sha256,)
            if not anchor_valid:
                raise StrategyRunIntegrityError(
                    "strategy_run_mutation_lease_history_invalid"
                )
            incremental_rows = connection.execute(
                """
                SELECT event_sequence, lease_id, run_id, epoch,
                       strategy_run_fingerprint, operation,
                       owner_token_sha256, acquired_at, event_type,
                       occurred_at, previous_event_sha256, event_sha256
                FROM paper_strategy_run_mutation_lease
                WHERE event_sequence > ?
                ORDER BY event_sequence
                """,
                (cache.max_event_sequence,),
            ).fetchall()
            leases = self._validate_incremental_mutation_lease_history(
                records=records,
                rows=incremental_rows,
                max_sequence=max_sequence,
                history_head=history_head,
                cache=cache,
            )
            self._mutation_lease_cache = _MutationLeaseValidationCache(
                max_event_sequence=max_sequence,
                history_head_sha256=history_head,
                active_leases=leases,
                file_signature=signature,
            )
            return leases

        mutation_rows = connection.execute(
            """
            SELECT event_sequence, lease_id, run_id, epoch,
                   strategy_run_fingerprint, operation, owner_token_sha256,
                   acquired_at, event_type, occurred_at,
                   previous_event_sha256, event_sha256
            FROM paper_strategy_run_mutation_lease
            ORDER BY event_sequence
            """
        ).fetchall()
        leases = self._validate_mutation_lease_history(
            records,
            mutation_rows,
            meta_rows,
        )
        self._mutation_lease_cache = _MutationLeaseValidationCache(
            max_event_sequence=max_sequence,
            history_head_sha256=history_head,
            active_leases=leases,
            file_signature=signature,
        )
        return leases

    @staticmethod
    def _validate_incremental_mutation_lease_history(
        *,
        records: tuple[StrategyRunEpochRecord, ...],
        rows: list[tuple[object, ...]],
        max_sequence: int,
        history_head: str,
        cache: _MutationLeaseValidationCache,
    ) -> tuple[_PersistedStrategyRunMutationLease, ...]:
        records_by_id = {record.run_id: record for record in records}
        active = {
            lease.lease_id: lease
            for lease in cache.active_leases
        }
        expected_head = cache.history_head_sha256
        expected_sequence = cache.max_event_sequence + 1
        try:
            for row in rows:
                (
                    event_sequence,
                    lease_id,
                    run_id,
                    epoch,
                    fingerprint,
                    operation,
                    owner_token_sha256,
                    acquired_at,
                    event_type,
                    occurred_at,
                    previous_event_sha256,
                    event_sha256,
                ) = row
                if event_sequence != expected_sequence:
                    raise ValueError("mutation lease sequence mismatch")
                lease = _PersistedStrategyRunMutationLease(
                    lease_id=lease_id,
                    run_id=run_id,
                    epoch=epoch,
                    strategy_run_fingerprint=fingerprint,
                    operation=operation,
                    owner_token_sha256=owner_token_sha256,
                    acquired_at=datetime.fromisoformat(acquired_at),
                )
                event_at = normalize_datetime(
                    datetime.fromisoformat(occurred_at),
                    "occurred_at",
                )
                record = records_by_id.get(lease.run_id)
                if (
                    record is None
                    or record.epoch != lease.epoch
                    or record.strategy_run_fingerprint
                    != lease.strategy_run_fingerprint
                    or event_type not in {"acquire", "release"}
                    or previous_event_sha256 != expected_head
                    or event_sha256
                    != _mutation_lease_event_checksum(
                        event_sequence=event_sequence,
                        previous_event_sha256=expected_head,
                        lease=lease,
                        event_type=event_type,
                        occurred_at=event_at,
                    )
                ):
                    raise ValueError("mutation lease event mismatch")
                if event_type == "acquire":
                    if lease.lease_id in active or event_at != lease.acquired_at:
                        raise ValueError("mutation lease acquisition mismatch")
                    active[lease.lease_id] = lease
                else:
                    if (
                        active.get(lease.lease_id) != lease
                        or event_at < lease.acquired_at
                    ):
                        raise ValueError("mutation lease release mismatch")
                    del active[lease.lease_id]
                expected_head = event_sha256
                expected_sequence += 1
        except (TypeError, ValueError) as exc:
            raise StrategyRunIntegrityError(
                "strategy_run_mutation_lease_history_invalid"
            ) from exc
        if (
            expected_sequence != max_sequence + 1
            or expected_head != history_head
            or any(
                records_by_id[lease.run_id].status != "active"
                for lease in active.values()
            )
        ):
            raise StrategyRunIntegrityError(
                "strategy_run_mutation_lease_history_invalid"
            )
        return tuple(active[lease_id] for lease_id in sorted(active))

    @staticmethod
    def _validate_history(
        records: tuple[StrategyRunEpochRecord, ...],
    ) -> None:
        if not records:
            return
        if tuple(record.epoch for record in records) != tuple(
            range(1, len(records) + 1)
        ):
            raise StrategyRunIntegrityError("strategy_run_history_invalid")
        predecessor: str | None = None
        previous_started_at: datetime | None = None
        for record in records:
            if (
                record.predecessor_run_id != predecessor
                or record.run_id
                != _strategy_run_id(
                    epoch=record.epoch,
                    strategy_run_fingerprint=(
                        record.strategy_run_fingerprint
                    ),
                    predecessor_run_id=predecessor,
                )
                or (
                    previous_started_at is not None
                    and record.started_at < previous_started_at
                )
            ):
                raise StrategyRunIntegrityError(
                    "strategy_run_history_invalid"
                )
            if record.status == "initializing":
                timestamps_valid = (
                    record.activated_at is None and record.ended_at is None
                )
            elif record.status == "active":
                timestamps_valid = (
                    record.activated_at is not None
                    and record.activated_at >= record.started_at
                    and record.ended_at is None
                )
            elif record.status == "closed":
                timestamps_valid = (
                    record.activated_at is not None
                    and record.ended_at is not None
                    and record.activated_at >= record.started_at
                    and record.ended_at >= record.activated_at
                )
            else:
                timestamps_valid = record.ended_at is not None
            if not timestamps_valid:
                raise StrategyRunIntegrityError(
                    "strategy_run_history_invalid"
                )
            predecessor = record.run_id
            previous_started_at = record.started_at

    @staticmethod
    def _validate_store_history(
        records: tuple[StrategyRunEpochRecord, ...],
        rows: list[tuple[object, ...]],
    ) -> None:
        expected_run_ids = {record.run_id for record in records}
        grouped: dict[str, dict[str, tuple[Path, str]]] = {}
        all_paths: set[Path] = set()
        try:
            for (
                raw_run_id,
                role,
                raw_path,
                instance_id,
                registry_checksum,
            ) in rows:
                run_id = _required_text(raw_run_id, "run_id")
                if run_id not in expected_run_ids:
                    raise StrategyRunIntegrityError(
                        "strategy_run_history_invalid"
                    )
                if role not in STRATEGY_RUN_STORE_ROLES:
                    raise StrategyRunIntegrityError(
                        "strategy_run_history_invalid"
                    )
                path = Path(raw_path).absolute()
                _required_text(instance_id, "store_instance_id")
                if registry_checksum != _registry_store_checksum(
                    run_id=run_id,
                    store_role=role,
                    store_path=path,
                    store_instance_id=instance_id,
                ):
                    raise StrategyRunIntegrityError(
                        "strategy_run_history_invalid"
                    )
                stores = grouped.setdefault(run_id, {})
                if role in stores or path in all_paths:
                    raise StrategyRunIntegrityError(
                        "strategy_run_history_invalid"
                    )
                stores[role] = (path, instance_id)
                all_paths.add(path)
        except (TypeError, ValueError) as exc:
            raise StrategyRunIntegrityError(
                "strategy_run_history_invalid"
            ) from exc
        if set(grouped) != expected_run_ids or any(
            set(stores) != set(STRATEGY_RUN_STORE_ROLES)
            for stores in grouped.values()
        ):
            raise StrategyRunIntegrityError("strategy_run_history_invalid")

    @staticmethod
    def _validate_history_anchor(
        records: tuple[StrategyRunEpochRecord, ...],
        store_rows: list[tuple[object, ...]],
        meta_rows: list[tuple[object, ...]],
    ) -> None:
        if len(meta_rows) != 1:
            raise StrategyRunIntegrityError("strategy_run_history_invalid")
        max_epoch, history_head, meta_checksum = meta_rows[0]
        if (
            isinstance(max_epoch, bool)
            or not isinstance(max_epoch, int)
            or max_epoch != len(records)
            or not isinstance(history_head, str)
            or _FINGERPRINT_RE.fullmatch(history_head) is None
            or meta_checksum
            != _history_meta_checksum(max_epoch, history_head)
        ):
            raise StrategyRunIntegrityError("strategy_run_history_invalid")
        grouped: dict[str, list[tuple[str, Path, str, str]]] = {}
        for (
            run_id,
            role,
            raw_path,
            instance_id,
            registry_checksum,
        ) in store_rows:
            grouped.setdefault(run_id, []).append(
                (
                    role,
                    Path(raw_path).absolute(),
                    instance_id,
                    registry_checksum,
                )
            )
        expected_head = _EMPTY_HISTORY_HEAD
        for record in records:
            expected_head = _next_history_head(
                expected_head,
                record,
                grouped[record.run_id],
            )
        if history_head != expected_head:
            raise StrategyRunIntegrityError("strategy_run_history_invalid")

    @staticmethod
    def _validate_mutation_lease_history(
        records: tuple[StrategyRunEpochRecord, ...],
        rows: list[tuple[object, ...]],
        meta_rows: list[tuple[object, ...]],
    ) -> tuple[_PersistedStrategyRunMutationLease, ...]:
        if len(meta_rows) != 1:
            raise StrategyRunIntegrityError(
                "strategy_run_mutation_lease_history_invalid"
            )
        max_sequence, history_head, meta_checksum = meta_rows[0]
        if (
            isinstance(max_sequence, bool)
            or not isinstance(max_sequence, int)
            or max_sequence != len(rows)
            or not isinstance(history_head, str)
            or _FINGERPRINT_RE.fullmatch(history_head) is None
            or meta_checksum
            != _mutation_lease_meta_checksum(max_sequence, history_head)
        ):
            raise StrategyRunIntegrityError(
                "strategy_run_mutation_lease_history_invalid"
            )
        records_by_id = {record.run_id: record for record in records}
        expected_head = _EMPTY_MUTATION_LEASE_HEAD
        acquired: dict[str, _PersistedStrategyRunMutationLease] = {}
        active: dict[str, _PersistedStrategyRunMutationLease] = {}
        try:
            for expected_sequence, row in enumerate(rows, start=1):
                (
                    event_sequence,
                    lease_id,
                    run_id,
                    epoch,
                    fingerprint,
                    operation,
                    owner_token_sha256,
                    acquired_at,
                    event_type,
                    occurred_at,
                    previous_event_sha256,
                    event_sha256,
                ) = row
                if event_sequence != expected_sequence:
                    raise ValueError("mutation lease sequence mismatch")
                lease = _PersistedStrategyRunMutationLease(
                    lease_id=lease_id,
                    run_id=run_id,
                    epoch=epoch,
                    strategy_run_fingerprint=fingerprint,
                    operation=operation,
                    owner_token_sha256=owner_token_sha256,
                    acquired_at=datetime.fromisoformat(acquired_at),
                )
                event_at = normalize_datetime(
                    datetime.fromisoformat(occurred_at),
                    "occurred_at",
                )
                record = records_by_id.get(lease.run_id)
                if (
                    record is None
                    or record.epoch != lease.epoch
                    or record.strategy_run_fingerprint
                    != lease.strategy_run_fingerprint
                    or event_type not in {"acquire", "release"}
                    or previous_event_sha256 != expected_head
                    or event_sha256
                    != _mutation_lease_event_checksum(
                        event_sequence=event_sequence,
                        previous_event_sha256=expected_head,
                        lease=lease,
                        event_type=event_type,
                        occurred_at=event_at,
                    )
                ):
                    raise ValueError("mutation lease event mismatch")
                if event_type == "acquire":
                    if lease.lease_id in acquired or event_at != lease.acquired_at:
                        raise ValueError("mutation lease acquisition mismatch")
                    acquired[lease.lease_id] = lease
                    active[lease.lease_id] = lease
                else:
                    if (
                        acquired.get(lease.lease_id) != lease
                        or active.get(lease.lease_id) != lease
                        or event_at < lease.acquired_at
                    ):
                        raise ValueError("mutation lease release mismatch")
                    del active[lease.lease_id]
                expected_head = event_sha256
        except (TypeError, ValueError) as exc:
            raise StrategyRunIntegrityError(
                "strategy_run_mutation_lease_history_invalid"
            ) from exc
        if history_head != expected_head:
            raise StrategyRunIntegrityError(
                "strategy_run_mutation_lease_history_invalid"
            )
        if any(
            records_by_id[lease.run_id].status != "active"
            for lease in active.values()
        ):
            raise StrategyRunIntegrityError(
                "strategy_run_mutation_lease_history_invalid"
            )
        return tuple(
            active[lease_id]
            for lease_id in sorted(active)
        )

    def active_epoch(self) -> StrategyRunEpochRecord | None:
        active = tuple(item for item in self.list_epochs() if item.status == "active")
        if len(active) > 1:
            raise StrategyRunIntegrityError("strategy_run_multiple_active_epochs")
        return None if not active else active[0]

    @staticmethod
    def _require_matching_active_record(
        active: ActiveStrategyRun,
        records: tuple[StrategyRunEpochRecord, ...],
    ) -> StrategyRunEpochRecord:
        matching = tuple(
            record
            for record in records
            if record.status == "active" and record.run_id == active.run_id
        )
        if len(matching) != 1:
            raise StrategyRunIntegrityError("strategy_run_not_active")
        record = matching[0]
        if (
            record.epoch != active.epoch
            or record.strategy_run_fingerprint
            != active.strategy_run_fingerprint
            or record.identity != active.identity
            or record.started_at != active.started_at
        ):
            raise StrategyRunIntegrityError("strategy_run_identity_mismatch")
        return record

    @staticmethod
    def _append_mutation_lease_event(
        connection: sqlite3.Connection,
        *,
        lease: _PersistedStrategyRunMutationLease,
        event_type: str,
        occurred_at: datetime,
    ) -> tuple[int, str]:
        if event_type not in {"acquire", "release"}:
            raise ValueError("mutation lease event type is invalid")
        meta = connection.execute(
            """
            SELECT max_event_sequence, history_head_sha256, meta_sha256
            FROM paper_strategy_run_mutation_lease_meta
            WHERE singleton_id = 1
            """
        ).fetchone()
        if (
            meta is None
            or meta[2] != _mutation_lease_meta_checksum(meta[0], meta[1])
        ):
            raise StrategyRunIntegrityError(
                "strategy_run_mutation_lease_history_invalid"
            )
        event_sequence = meta[0] + 1
        event_sha256 = _mutation_lease_event_checksum(
            event_sequence=event_sequence,
            previous_event_sha256=meta[1],
            lease=lease,
            event_type=event_type,
            occurred_at=occurred_at,
        )
        connection.execute(
            """
            INSERT INTO paper_strategy_run_mutation_lease (
                event_sequence, lease_id, run_id, epoch,
                strategy_run_fingerprint, operation, owner_token_sha256,
                acquired_at, event_type, occurred_at,
                previous_event_sha256, event_sha256
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                event_sequence,
                lease.lease_id,
                lease.run_id,
                lease.epoch,
                lease.strategy_run_fingerprint,
                lease.operation,
                lease.owner_token_sha256,
                lease.acquired_at.isoformat(),
                event_type,
                occurred_at.isoformat(),
                meta[1],
                event_sha256,
            ),
        )
        changed = connection.execute(
            """
            UPDATE paper_strategy_run_mutation_lease_meta
            SET max_event_sequence = ?, history_head_sha256 = ?,
                meta_sha256 = ?
            WHERE singleton_id = 1
              AND max_event_sequence = ?
              AND history_head_sha256 = ?
              AND meta_sha256 = ?
            """,
            (
                event_sequence,
                event_sha256,
                _mutation_lease_meta_checksum(
                    event_sequence,
                    event_sha256,
                ),
                meta[0],
                meta[1],
                meta[2],
            ),
        ).rowcount
        if changed != 1:
            raise StrategyRunIntegrityError(
                "strategy_run_mutation_lease_history_invalid"
            )
        return event_sequence, event_sha256

    def _cache_mutation_lease_state(
        self,
        *,
        connection: sqlite3.Connection,
        expected_data_version: int,
        max_event_sequence: int,
        history_head_sha256: str,
        active_leases: tuple[_PersistedStrategyRunMutationLease, ...],
    ) -> None:
        observed_data_version = self._connection_data_version(connection)
        if observed_data_version != expected_data_version:
            self._mutation_lease_cache = None
            return
        signature = _database_file_signature(self.path)
        if (
            signature is None
            or self._connection_data_version(connection)
            != observed_data_version
        ):
            self._mutation_lease_cache = None
            return
        self._mutation_lease_cache = _MutationLeaseValidationCache(
            max_event_sequence=max_event_sequence,
            history_head_sha256=history_head_sha256,
            active_leases=active_leases,
            file_signature=signature,
        )

    @staticmethod
    def _connection_data_version(connection: sqlite3.Connection) -> int:
        row = connection.execute("PRAGMA data_version").fetchone()
        if (
            row is None
            or len(row) != 1
            or isinstance(row[0], bool)
            or not isinstance(row[0], int)
            or row[0] < 0
        ):
            raise StrategyRunIntegrityError(
                "strategy_run_mutation_lease_history_invalid"
            )
        return row[0]

    def acquire_mutation_lease(
        self,
        active: ActiveStrategyRun,
        *,
        operation: str,
        now: datetime | None = None,
    ) -> StrategyRunMutationLease:
        if not isinstance(active, ActiveStrategyRun):
            raise TypeError("active must be ActiveStrategyRun")
        operation = _required_text(operation, "operation")
        occurred_at = normalize_datetime(
            datetime.now(active.started_at.tzinfo) if now is None else now,
            "now",
        )
        _revalidate_active_strategy_run(active)
        owner_token = (
            f"mutation-owner-token-{uuid4().hex}{uuid4().hex}"
        )
        lease = StrategyRunMutationLease(
            lease_id=f"mutation-lease-{uuid4().hex}",
            run_id=active.run_id,
            epoch=active.epoch,
            strategy_run_fingerprint=active.strategy_run_fingerprint,
            operation=operation,
            owner_token=owner_token,
            acquired_at=occurred_at,
        )
        persisted_lease = _PersistedStrategyRunMutationLease(
            lease_id=lease.lease_id,
            run_id=lease.run_id,
            epoch=lease.epoch,
            strategy_run_fingerprint=lease.strategy_run_fingerprint,
            operation=lease.operation,
            owner_token_sha256=_text_sha256(owner_token),
            acquired_at=lease.acquired_at,
        )
        with self._lock, self._connect() as connection:
            data_version = self._connection_data_version(connection)
            connection.execute("BEGIN IMMEDIATE")
            try:
                records, _stores, existing_leases = self._validated_registry_state(
                    connection
                )
                self._require_matching_active_record(active, records)
                sequence, history_head = self._append_mutation_lease_event(
                    connection,
                    lease=persisted_lease,
                    event_type="acquire",
                    occurred_at=occurred_at,
                )
                connection.commit()
            except BaseException:
                connection.rollback()
                raise
            self._cache_mutation_lease_state(
                connection=connection,
                expected_data_version=data_version,
                max_event_sequence=sequence,
                history_head_sha256=history_head,
                active_leases=tuple(
                    sorted(
                        existing_leases + (persisted_lease,),
                        key=lambda item: item.lease_id,
                    )
                ),
            )
        return lease

    def release_mutation_lease(
        self,
        active: ActiveStrategyRun,
        *,
        lease: StrategyRunMutationLease,
        now: datetime | None = None,
    ) -> None:
        if not isinstance(active, ActiveStrategyRun):
            raise TypeError("active must be ActiveStrategyRun")
        if not isinstance(lease, StrategyRunMutationLease):
            raise TypeError("lease must be StrategyRunMutationLease")
        occurred_at = normalize_datetime(
            datetime.now(active.started_at.tzinfo) if now is None else now,
            "now",
        )
        if occurred_at < lease.acquired_at:
            raise ValueError("mutation lease release cannot precede acquisition")
        with self._lock, self._connect() as connection:
            data_version = self._connection_data_version(connection)
            connection.execute("BEGIN IMMEDIATE")
            try:
                records, _stores, leases = self._validated_registry_state(
                    connection
                )
                self._require_matching_active_record(active, records)
                matching = tuple(
                    persisted
                    for persisted in leases
                    if persisted.lease_id == lease.lease_id
                )
                if (
                    lease.run_id != active.run_id
                    or lease.epoch != active.epoch
                    or lease.strategy_run_fingerprint
                    != active.strategy_run_fingerprint
                    or len(matching) != 1
                    or matching[0].to_payload() != lease.to_payload()
                    or matching[0].owner_token_sha256
                    != _text_sha256(lease.owner_token)
                ):
                    raise StrategyRunIntegrityError(
                        "strategy_run_mutation_lease_invalid"
                    )
                sequence, history_head = self._append_mutation_lease_event(
                    connection,
                    lease=matching[0],
                    event_type="release",
                    occurred_at=occurred_at,
                )
                connection.commit()
            except BaseException:
                connection.rollback()
                raise
            self._cache_mutation_lease_state(
                connection=connection,
                expected_data_version=data_version,
                max_event_sequence=sequence,
                history_head_sha256=history_head,
                active_leases=tuple(
                    persisted
                    for persisted in leases
                    if persisted.lease_id != lease.lease_id
                ),
            )

    def mutation_lease_diagnostics(self, run_id: str) -> dict[str, object]:
        run_id = _required_text(run_id, "run_id")
        with self._lock, self._connect() as connection:
            records, _stores, leases = self._validated_registry_state(
                connection
            )
        if run_id not in {record.run_id for record in records}:
            raise StrategyRunIntegrityError("strategy_run_registry_invalid")
        matching = tuple(lease for lease in leases if lease.run_id == run_id)
        return {
            "protocol": STRATEGY_RUN_MUTATION_LEASE_PROTOCOL,
            "active_count": len(matching),
            "leases": [lease.to_payload() for lease in matching],
        }

    def _store_rows(self, run_id: str) -> dict[str, tuple[Path, str]]:
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                """
                SELECT store_role, store_path, store_instance_id,
                       registry_binding_sha256
                FROM paper_strategy_run_store WHERE run_id = ?
                """,
                (run_id,),
            ).fetchall()
        result: dict[str, tuple[Path, str]] = {}
        for role, raw_path, instance_id, registry_checksum in rows:
            path = Path(raw_path).absolute()
            if (
                role not in STRATEGY_RUN_STORE_ROLES
                or registry_checksum
                != _registry_store_checksum(
                    run_id=run_id,
                    store_role=role,
                    store_path=path,
                    store_instance_id=instance_id,
                )
            ):
                raise StrategyRunIntegrityError(
                    "strategy_run_store_registry_invalid"
                )
            result[role] = (path, instance_id)
        if len(result) != len(rows):
            raise StrategyRunIntegrityError("strategy_run_store_registry_invalid")
        return result

    def _validate_active(
        self,
        record: StrategyRunEpochRecord,
        store_paths: Mapping[str, Path],
    ) -> ActiveStrategyRun:
        registered = self._store_rows(record.run_id)
        if set(registered) != set(STRATEGY_RUN_STORE_ROLES):
            raise StrategyRunIntegrityError("strategy_run_partial_binding")
        bindings: dict[str, StrategyRunStoreBinding] = {}
        for role in STRATEGY_RUN_STORE_ROLES:
            registered_path, instance_id = registered[role]
            if registered_path != store_paths[role]:
                raise StrategyRunIntegrityError(
                    "strategy_run_store_binding_mismatch"
                )
            binding = read_strategy_run_binding(store_paths[role])
            if (
                binding is None
                or binding.store_role != role
                or binding.store_instance_id != instance_id
                or binding.run_id != record.run_id
                or binding.epoch != record.epoch
                or binding.strategy_run_fingerprint
                != record.strategy_run_fingerprint
                or binding.identity_sha256 != record.identity_sha256
            ):
                raise StrategyRunIntegrityError(
                    "strategy_run_store_binding_mismatch"
                )
            bindings[role] = binding
        return _make_active_strategy_run(
            record=record,
            bindings=bindings,
            registry_path=self.path,
            store_paths=store_paths,
            registry=self,
        )

    def _validate_epoch_stores_and_flat(
        self,
        active: StrategyRunEpochRecord,
    ) -> None:
        registered = self._store_rows(active.run_id)
        if set(registered) != set(STRATEGY_RUN_STORE_ROLES):
            raise StrategyRunIntegrityError("strategy_run_partial_binding")
        paths = {role: value[0] for role, value in registered.items()}
        self._validate_active(active, paths)
        if not _previous_ledger_is_flat(paths["ledger"]):
            raise StrategyRunIntegrityError(
                "strategy_run_previous_epoch_not_flat"
            )

    @staticmethod
    def _validate_locked_predecessor_stores_and_flat(
        active: StrategyRunEpochRecord,
        store_rows: list[tuple[object, ...]],
    ) -> None:
        registered: dict[str, tuple[Path, str]] = {}
        try:
            for row in store_rows:
                run_id, role, raw_path, instance_id, _registry_checksum = row
                if run_id != active.run_id:
                    continue
                if (
                    role in registered
                    or role not in STRATEGY_RUN_STORE_ROLES
                    or not isinstance(instance_id, str)
                    or not instance_id
                ):
                    raise StrategyRunIntegrityError(
                        "strategy_run_store_registry_invalid"
                    )
                registered[role] = (Path(raw_path).absolute(), instance_id)
        except (TypeError, ValueError) as exc:
            raise StrategyRunIntegrityError(
                "strategy_run_store_registry_invalid"
            ) from exc
        if set(registered) != set(STRATEGY_RUN_STORE_ROLES):
            raise StrategyRunIntegrityError("strategy_run_partial_binding")
        for role, (path, instance_id) in registered.items():
            binding = read_strategy_run_binding(path)
            if (
                binding is None
                or binding.store_role != role
                or binding.store_instance_id != instance_id
                or binding.run_id != active.run_id
                or binding.epoch != active.epoch
                or binding.strategy_run_fingerprint
                != active.strategy_run_fingerprint
                or binding.identity_sha256 != active.identity_sha256
            ):
                raise StrategyRunIntegrityError(
                    "strategy_run_store_binding_mismatch"
                )
        if not _previous_ledger_is_flat(registered["ledger"][0]):
            raise StrategyRunIntegrityError(
                "strategy_run_previous_epoch_not_flat"
            )

    def _validate_predecessor(self, record: StrategyRunEpochRecord) -> None:
        predecessor = record.predecessor_run_id
        if predecessor is None:
            return
        matching = tuple(
            item
            for item in self.list_epochs()
            if item.run_id == predecessor and item.status == "active"
        )
        if len(matching) != 1:
            raise StrategyRunIntegrityError(
                "strategy_run_predecessor_changed"
            )
        self._validate_epoch_stores_and_flat(matching[0])

    def resume(
        self,
        *,
        requested_epoch: int,
        identity: StrategyRunIdentity,
        store_paths: Mapping[str, str | Path],
        allow_partial_initializing: bool = False,
    ) -> ActiveStrategyRun | None:
        requested_epoch = _required_epoch(requested_epoch)
        if not isinstance(identity, StrategyRunIdentity):
            raise TypeError("identity must be StrategyRunIdentity")
        if type(allow_partial_initializing) is not bool:
            raise TypeError("allow_partial_initializing must be boolean")
        normalized = self._normalize_store_paths(store_paths)
        epochs = self.list_epochs()
        initializing = tuple(
            item for item in epochs if item.status == "initializing"
        )
        if initializing:
            if len(initializing) != 1:
                raise StrategyRunIntegrityError(
                    "strategy_run_partial_binding"
                )
            pending = initializing[0]
            registered = self._store_rows(pending.run_id)
            existing_bindings = {
                role: read_strategy_run_binding(normalized[role])
                for role in STRATEGY_RUN_STORE_ROLES
            }
            if (
                pending.epoch != requested_epoch
                or pending.identity != identity
                or set(registered) != set(STRATEGY_RUN_STORE_ROLES)
                or any(
                    registered[role][0] != normalized[role]
                    for role in STRATEGY_RUN_STORE_ROLES
                )
            ):
                raise StrategyRunIntegrityError(
                    "strategy_run_partial_binding"
                )
            if any(
                binding is not None
                and (
                    binding.store_role != role
                    or binding.store_instance_id != registered[role][1]
                    or binding.run_id != pending.run_id
                    or binding.epoch != pending.epoch
                    or binding.strategy_run_fingerprint
                    != pending.strategy_run_fingerprint
                    or binding.identity_sha256 != pending.identity_sha256
                )
                for role, binding in existing_bindings.items()
            ):
                raise StrategyRunIntegrityError(
                    "strategy_run_partial_binding"
                )
            if not allow_partial_initializing and any(
                not _store_schema_initialized(normalized[role], role)
                for role in STRATEGY_RUN_STORE_ROLES
            ):
                raise StrategyRunIntegrityError(
                    "strategy_run_partial_binding"
                )
            return None
        active = self.active_epoch()
        if active is None or active.epoch != requested_epoch:
            return None
        if active.strategy_run_fingerprint != identity.fingerprint:
            raise StrategyRunIntegrityError("strategy_run_fingerprint_mismatch")
        return self._validate_active(active, normalized)

    def prepare_epoch(
        self,
        *,
        requested_epoch: int,
        identity: StrategyRunIdentity,
        store_paths: Mapping[str, str | Path],
        now: datetime,
        allow_uninitialized_stores: bool = False,
    ) -> StrategyRunEpochRecord:
        requested_epoch = _required_epoch(requested_epoch)
        if not isinstance(identity, StrategyRunIdentity):
            raise TypeError("identity must be StrategyRunIdentity")
        if type(allow_uninitialized_stores) is not bool:
            raise TypeError("allow_uninitialized_stores must be boolean")
        normalized = self._normalize_store_paths(store_paths)
        now = normalize_datetime(now, "now")
        epochs = self.list_epochs()
        initializing = tuple(
            item for item in epochs if item.status == "initializing"
        )
        if initializing:
            if len(initializing) != 1:
                raise StrategyRunIntegrityError(
                    "strategy_run_partial_binding"
                )
            pending = initializing[0]
            registered = self._store_rows(pending.run_id)
            if (
                pending.epoch != requested_epoch
                or pending.identity != identity
                or set(registered) != set(STRATEGY_RUN_STORE_ROLES)
                or any(
                    registered[role][0] != normalized[role]
                    for role in STRATEGY_RUN_STORE_ROLES
                )
            ):
                raise StrategyRunIntegrityError(
                    "strategy_run_partial_binding"
                )
            return pending
        active = self.active_epoch()
        if active is None:
            if epochs:
                raise StrategyRunIntegrityError(
                    "strategy_run_no_active_epoch"
                )
            expected_epoch = 1
        else:
            expected_epoch = active.epoch + 1
            self._validate_epoch_stores_and_flat(active)
        if requested_epoch != expected_epoch:
            if active is not None and requested_epoch == active.epoch:
                raise StrategyRunIntegrityError("strategy_run_fingerprint_mismatch")
            raise StrategyRunIntegrityError("strategy_run_epoch_sequence_invalid")
        for role, path in normalized.items():
            if not allow_uninitialized_stores and not path.is_file():
                raise StrategyRunIntegrityError(
                    f"strategy_run_store_missing:{role}"
                )
            if (
                not allow_uninitialized_stores
                and not _store_schema_initialized(path, role)
            ):
                raise StrategyRunIntegrityError(
                    f"strategy_run_store_schema_invalid:{role}"
                )
            binding = read_strategy_run_binding(path)
            if binding is not None:
                raise StrategyRunIntegrityError(
                    "strategy_run_store_binding_mismatch"
                )
            if _unbound_store_has_evidence(path, role):
                raise StrategyRunIntegrityError(f"legacy_unbound:{role}")
        identity_json = _canonical_json(identity.to_payload())
        identity_sha256 = _text_sha256(identity_json)
        predecessor = None if active is None else active.run_id
        run_id = _strategy_run_id(
            epoch=requested_epoch,
            strategy_run_fingerprint=identity.fingerprint,
            predecessor_run_id=predecessor,
        )
        record = StrategyRunEpochRecord(
            epoch=requested_epoch,
            run_id=run_id,
            strategy_run_fingerprint=identity.fingerprint,
            identity=identity,
            identity_sha256=identity_sha256,
            status="initializing",
            predecessor_run_id=predecessor,
            started_at=now,
            activated_at=None,
            ended_at=None,
        )
        prepared_store_rows: list[tuple[str, Path, str, str]] = []
        for role, path in normalized.items():
            instance_id = f"{role}-{uuid4().hex}"
            prepared_store_rows.append(
                (
                    role,
                    path,
                    instance_id,
                    _registry_store_checksum(
                        run_id=run_id,
                        store_role=role,
                        store_path=path,
                        store_instance_id=instance_id,
                    ),
                )
            )
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                meta_row = connection.execute(
                    """
                    SELECT max_epoch, history_head_sha256, meta_sha256
                    FROM paper_strategy_run_meta WHERE singleton_id = 1
                    """
                ).fetchone()
                if (
                    meta_row is None
                    or meta_row[0] != requested_epoch - 1
                    or meta_row[2]
                    != _history_meta_checksum(meta_row[0], meta_row[1])
                ):
                    raise StrategyRunIntegrityError(
                        "strategy_run_history_invalid"
                    )
                connection.execute(
                    """
                    INSERT INTO paper_strategy_run_epoch (
                        epoch, run_id, strategy_run_fingerprint,
                        identity_json, identity_sha256, status,
                        predecessor_run_id, started_at, activated_at, ended_at
                    ) VALUES (?, ?, ?, ?, ?, 'initializing', ?, ?, NULL, NULL)
                    """,
                    (
                        record.epoch,
                        record.run_id,
                        record.strategy_run_fingerprint,
                        identity_json,
                        identity_sha256,
                        predecessor,
                        now.isoformat(),
                    ),
                )
                for (
                    role,
                    path,
                    instance_id,
                    registry_checksum,
                ) in prepared_store_rows:
                    connection.execute(
                        """
                        INSERT INTO paper_strategy_run_store (
                            run_id, store_role, store_path, store_instance_id,
                            registry_binding_sha256
                        ) VALUES (?, ?, ?, ?, ?)
                        """,
                        (
                            run_id,
                            role,
                            str(path),
                            instance_id,
                            registry_checksum,
                        ),
                    )
                next_head = _next_history_head(
                    meta_row[1],
                    record,
                    prepared_store_rows,
                )
                changed = connection.execute(
                    """
                    UPDATE paper_strategy_run_meta
                    SET max_epoch = ?, history_head_sha256 = ?, meta_sha256 = ?
                    WHERE singleton_id = 1
                      AND max_epoch = ? AND history_head_sha256 = ?
                    """,
                    (
                        requested_epoch,
                        next_head,
                        _history_meta_checksum(requested_epoch, next_head),
                        meta_row[0],
                        meta_row[1],
                    ),
                ).rowcount
                if changed != 1:
                    raise StrategyRunIntegrityError(
                        "strategy_run_history_invalid"
                    )
                connection.commit()
            except BaseException:
                connection.rollback()
                raise
        return record

    def bind_and_activate(
        self,
        record: StrategyRunEpochRecord,
        *,
        store_paths: Mapping[str, str | Path],
        now: datetime,
    ) -> ActiveStrategyRun:
        normalized = self._normalize_store_paths(store_paths)
        self._validate_predecessor(record)
        registered = self._store_rows(record.run_id)
        if set(registered) != set(STRATEGY_RUN_STORE_ROLES):
            raise StrategyRunIntegrityError("strategy_run_partial_binding")
        existing_bindings = {
            role: read_strategy_run_binding(normalized[role])
            for role in STRATEGY_RUN_STORE_ROLES
        }
        bindings_preexisting = any(
            binding is not None for binding in existing_bindings.values()
        )
        if bindings_preexisting:
            if any(
                binding is None
                or binding.store_role != role
                or binding.store_instance_id != registered[role][1]
                or binding.run_id != record.run_id
                or binding.epoch != record.epoch
                or binding.strategy_run_fingerprint
                != record.strategy_run_fingerprint
                or binding.identity_sha256 != record.identity_sha256
                for role, binding in existing_bindings.items()
            ):
                raise StrategyRunIntegrityError(
                    "strategy_run_partial_binding"
                )
            bindings = {
                role: binding
                for role, binding in existing_bindings.items()
                if binding is not None
            }
        else:
            bindings = {}
        for role in STRATEGY_RUN_STORE_ROLES:
            registered_path, instance_id = registered[role]
            if registered_path != normalized[role]:
                raise StrategyRunIntegrityError(
                    "strategy_run_store_binding_mismatch"
                )
            if not bindings_preexisting:
                bindings[role] = _bind_store(
                    normalized[role],
                    role=role,
                    record=record,
                    store_instance_id=instance_id,
                )
        # Store writes are protected by durable registry mutation leases.  The
        # writer lock below serializes the final predecessor drain check with
        # any competing lease acquisition.
        self._validate_predecessor(record)
        now = normalize_datetime(now, "now")
        with self._lock, self._connect() as connection:
            data_version = self._connection_data_version(connection)
            connection.execute("BEGIN IMMEDIATE")
            try:
                records, store_rows, leases = self._validated_registry_state(
                    connection,
                    force_full_mutation=True,
                )
                current = connection.execute(
                    "SELECT status FROM paper_strategy_run_epoch WHERE run_id = ?",
                    (record.run_id,),
                ).fetchone()
                if current != ("initializing",):
                    raise StrategyRunIntegrityError(
                        "strategy_run_partial_binding"
                    )
                if record.predecessor_run_id is not None:
                    predecessor = tuple(
                        item
                        for item in records
                        if item.run_id == record.predecessor_run_id
                        and item.status == "active"
                    )
                    if len(predecessor) != 1:
                        raise StrategyRunIntegrityError(
                            "strategy_run_predecessor_changed"
                        )
                    if any(
                        lease.run_id == record.predecessor_run_id
                        for lease in leases
                    ):
                        raise StrategyRunIntegrityError(
                            "strategy_run_inflight_not_drained"
                        )
                    self._validate_locked_predecessor_stores_and_flat(
                        predecessor[0],
                        store_rows,
                    )
                    changed = connection.execute(
                        """
                        UPDATE paper_strategy_run_epoch
                        SET status = 'closed', ended_at = ?
                        WHERE run_id = ? AND status = 'active'
                        """,
                        (now.isoformat(), record.predecessor_run_id),
                    ).rowcount
                    if changed != 1:
                        raise StrategyRunIntegrityError(
                            "strategy_run_predecessor_changed"
                        )
                changed = connection.execute(
                    """
                    UPDATE paper_strategy_run_epoch
                    SET status = 'active', activated_at = ?
                    WHERE run_id = ? AND status = 'initializing'
                    """,
                    (now.isoformat(), record.run_id),
                ).rowcount
                if changed != 1:
                    raise StrategyRunIntegrityError(
                        "strategy_run_partial_binding"
                    )
                connection.commit()
            except BaseException:
                connection.rollback()
                raise
            cache = self._mutation_lease_cache
            if cache is not None:
                self._cache_mutation_lease_state(
                    connection=connection,
                    expected_data_version=data_version,
                    max_event_sequence=cache.max_event_sequence,
                    history_head_sha256=cache.history_head_sha256,
                    active_leases=cache.active_leases,
                )
        return _make_active_strategy_run(
            record=record,
            bindings=bindings,
            registry_path=self.path,
            store_paths=normalized,
            registry=self,
        )


def _revalidate_active_strategy_run(
    active: ActiveStrategyRun,
) -> tuple[_PersistedStrategyRunMutationLease, ...]:
    if _file_identity(
        active.registry_path,
        reason="strategy_run_registry_file_replaced",
    ) != active.registry_file_identity:
        raise StrategyRunIntegrityError("strategy_run_registry_file_replaced")
    try:
        registry = active._registry
        if registry.path != active.registry_path:
            raise StrategyRunIntegrityError(
                "strategy_run_registry_file_replaced"
            )
        with registry._lock, registry._connect() as connection:
            connection.execute("BEGIN")
            if not all(
                _table_exists(connection, table)
                for table in (
                    "paper_strategy_run_epoch",
                    "paper_strategy_run_store",
                    "paper_strategy_run_meta",
                    "paper_strategy_run_mutation_lease",
                    "paper_strategy_run_mutation_lease_meta",
                )
            ):
                raise StrategyRunIntegrityError(
                    "strategy_run_registry_invalid"
                )
            records, all_store_rows, active_leases = (
                registry._validated_registry_state(connection)
            )
            active_records = tuple(
                record for record in records if record.status == "active"
            )
            if (
                len(active_records) != 1
                or active_records[0].run_id != active.run_id
            ):
                raise StrategyRunIntegrityError("strategy_run_not_active")
            record = active_records[0]
            store_rows = [
                row[1:] for row in all_store_rows if row[0] == active.run_id
            ]
    except sqlite3.Error as exc:
        raise StrategyRunIntegrityError(
            "strategy_run_registry_invalid"
        ) from exc
    expected_identity_sha256 = next(
        iter(active.store_bindings.values())
    ).identity_sha256
    if (
        record.epoch != active.epoch
        or record.strategy_run_fingerprint
        != active.strategy_run_fingerprint
        or record.identity != active.identity
        or record.identity_sha256 != expected_identity_sha256
        or record.started_at != active.started_at
    ):
        raise StrategyRunIntegrityError("strategy_run_identity_mismatch")
    registered: dict[str, tuple[Path, str]] = {}
    try:
        for role, raw_path, instance_id, _registry_checksum in store_rows:
            if role in registered or role not in STRATEGY_RUN_STORE_ROLES:
                raise StrategyRunIntegrityError(
                    "strategy_run_store_registry_invalid"
                )
            registered[role] = (Path(raw_path).absolute(), instance_id)
    except (TypeError, ValueError) as exc:
        raise StrategyRunIntegrityError(
            "strategy_run_store_registry_invalid"
        ) from exc
    if set(registered) != set(STRATEGY_RUN_STORE_ROLES):
        raise StrategyRunIntegrityError("strategy_run_partial_binding")
    for role in STRATEGY_RUN_STORE_ROLES:
        path = active.store_paths[role]
        if _file_identity(
            path,
            reason="strategy_run_store_file_replaced",
        ) != active.store_file_identities[role]:
            raise StrategyRunIntegrityError(
                "strategy_run_store_file_replaced"
            )
        registered_path, instance_id = registered[role]
        expected = active.store_bindings[role]
        if (
            registered_path != path
            or instance_id != expected.store_instance_id
        ):
            raise StrategyRunIntegrityError(
                "strategy_run_store_binding_mismatch"
            )
        if read_strategy_run_binding(path) != expected:
            raise StrategyRunIntegrityError(
                "strategy_run_store_binding_mismatch"
            )
    return tuple(
        lease for lease in active_leases if lease.run_id == active.run_id
    )


def establish_strategy_run(
    registry_path: str | Path,
    *,
    requested_epoch: int,
    identity: StrategyRunIdentity,
    store_paths: Mapping[str, str | Path],
    now: datetime,
) -> ActiveStrategyRun:
    registry = SQLiteStrategyRunRegistry(registry_path)
    resumed = registry.resume(
        requested_epoch=requested_epoch,
        identity=identity,
        store_paths=store_paths,
    )
    if resumed is not None:
        return resumed
    record = registry.prepare_epoch(
        requested_epoch=requested_epoch,
        identity=identity,
        store_paths=store_paths,
        now=now,
    )
    return registry.bind_and_activate(record, store_paths=store_paths, now=now)


def _create_strategy_run_bootstrap_reservation(
    registry_path: str | Path,
    *,
    requested_epoch: int,
    identity: StrategyRunIdentity,
    store_paths: Mapping[str, str | Path],
    now: datetime,
) -> StrategyRunBootstrapReservation:

    registry = SQLiteStrategyRunRegistry(registry_path)
    normalized = registry._normalize_store_paths(store_paths)
    if registry.path in normalized.values():
        raise ValueError("strategy-run registry path must differ from store paths")
    active = registry.resume(
        requested_epoch=requested_epoch,
        identity=identity,
        store_paths=normalized,
        allow_partial_initializing=True,
    )
    if active is None:
        record = registry.prepare_epoch(
            requested_epoch=requested_epoch,
            identity=identity,
            store_paths=normalized,
            now=now,
            allow_uninitialized_stores=True,
        )
        registered = registry._store_rows(record.run_id)
        if set(registered) != set(STRATEGY_RUN_STORE_ROLES):
            raise StrategyRunIntegrityError("strategy_run_partial_binding")
        expected_bindings = {
            role: StrategyRunStoreBinding(
                run_id=record.run_id,
                epoch=record.epoch,
                strategy_run_fingerprint=record.strategy_run_fingerprint,
                identity_sha256=record.identity_sha256,
                store_role=role,
                store_instance_id=registered[role][1],
                bound_at=record.started_at,
            )
            for role in STRATEGY_RUN_STORE_ROLES
        }
        for role in STRATEGY_RUN_STORE_ROLES:
            registered_path, _instance_id = registered[role]
            if registered_path != normalized[role]:
                raise StrategyRunIntegrityError(
                    "strategy_run_store_binding_mismatch"
                )
            existing = read_strategy_run_binding(normalized[role])
            if existing not in (None, expected_bindings[role]):
                raise StrategyRunIntegrityError(
                    "strategy_run_store_binding_mismatch"
                )
            if existing is None and _unbound_store_has_evidence(
                normalized[role],
                role,
            ):
                raise StrategyRunIntegrityError(f"legacy_unbound:{role}")
        bindings = {
            role: _bind_store(
                normalized[role],
                role=role,
                record=record,
                store_instance_id=registered[role][1],
            )
            for role in STRATEGY_RUN_STORE_ROLES
        }
    else:
        matches = tuple(
            record
            for record in registry.list_epochs()
            if record.run_id == active.run_id
        )
        if len(matches) != 1:
            raise StrategyRunIntegrityError("strategy_run_registry_invalid")
        record = matches[0]
        bindings = dict(active.store_bindings)
    return StrategyRunBootstrapReservation(
        record=record,
        store_bindings=bindings,
        store_paths=normalized,
        registry_path=registry.path,
        _registry=registry,
        _active=active,
    )


@contextmanager
def reserve_strategy_run_bootstrap(
    registry_path: str | Path,
    *,
    requested_epoch: int,
    identity: StrategyRunIdentity,
    store_paths: Mapping[str, str | Path],
    now: datetime,
    lock_timeout: float = 10.0,
) -> Iterator[StrategyRunBootstrapReservation]:
    """Exclusively reserve exact stores before any store factory runs."""

    normalized = SQLiteStrategyRunRegistry._normalize_store_paths(store_paths)
    normalized_registry = Path(registry_path).expanduser().absolute()
    if normalized_registry in normalized.values():
        raise ValueError("strategy-run registry path must differ from store paths")
    with _exclusive_bootstrap_lock_set(
        (normalized_registry, *normalized.values()),
        timeout=lock_timeout,
    ):
        reservation = _create_strategy_run_bootstrap_reservation(
            normalized_registry,
            requested_epoch=requested_epoch,
            identity=identity,
            store_paths=normalized,
            now=now,
        )
        try:
            yield reservation
        finally:
            reservation.close()


__all__ = [
    "ActiveStrategyRun",
    "SQLiteStrategyRunRegistry",
    "STRATEGY_RUN_MUTATION_LEASE_PROTOCOL",
    "STRATEGY_RUN_STORE_ROLES",
    "STRATEGY_RUN_SWITCH_CAPABILITY",
    "StrategyRunEpochRecord",
    "StrategyRunBootstrapReservation",
    "StrategyRunIdentity",
    "StrategyRunIntegrityError",
    "StrategyRunMutationLease",
    "StrategyRunStoreBinding",
    "build_monitor_policy_fingerprint",
    "build_review_runtime_policy_fingerprint",
    "build_review_policy_fingerprint",
    "build_rule_algorithm_fingerprint",
    "build_universe_policy_fingerprint",
    "establish_strategy_run",
    "read_strategy_run_binding",
    "reserve_strategy_run_bootstrap",
    "trusted_bar_schema_fingerprint",
]
