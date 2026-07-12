"""Opt-in bounded monitoring runtime for decision support.

This module schedules analysis and review work only.  It intentionally has no
broker or order execution dependency.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from threading import Lock

from .fingerprints import normalize_datetime
from .mutation_fence import MutationLeaseGuard, mutation_fenced


_CONFIG_FIELDS = frozenset(
    {
        "enabled",
        "markets",
        "scan_interval_seconds",
        "review_workers",
        "review_queue_limit",
        "max_llm_reviews_per_day",
        "paper_enabled",
        "auto_order_enabled",
    }
)


def _positive_int(value: object, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{field_name} must be a positive integer")
    return value


@dataclass(frozen=True, slots=True)
class MonitorConfig:
    enabled: bool = False
    markets: tuple[str, ...] = ("a",)
    scan_interval_seconds: int = 30
    review_workers: int = 1
    review_queue_limit: int = 20
    max_llm_reviews_per_day: int = 20
    paper_enabled: bool = True
    auto_order_enabled: bool = False

    def __post_init__(self) -> None:
        for field_name in (
            "enabled",
            "paper_enabled",
            "auto_order_enabled",
        ):
            if type(getattr(self, field_name)) is not bool:
                raise ValueError(f"{field_name} must be boolean")
        markets = tuple(self.markets)
        if not markets or any(
            not isinstance(market, str) or market != "a" for market in markets
        ):
            raise ValueError("markets must contain only the A-share market 'a'")
        if len(markets) != len(set(markets)):
            raise ValueError("markets must not contain duplicates")
        object.__setattr__(self, "markets", markets)
        _positive_int(self.scan_interval_seconds, "scan_interval_seconds")
        if self.review_workers != 1:
            raise ValueError("review_workers must be exactly one")
        _positive_int(self.review_queue_limit, "review_queue_limit")
        _positive_int(
            self.max_llm_reviews_per_day,
            "max_llm_reviews_per_day",
        )
        if self.auto_order_enabled:
            raise ValueError("auto_order_enabled must remain false")

    @classmethod
    def from_mapping(cls, value: Mapping[str, object] | None) -> MonitorConfig:
        if value is None:
            return cls()
        if not isinstance(value, Mapping):
            raise TypeError("monitor config must be a mapping")
        if set(value) - _CONFIG_FIELDS:
            raise ValueError("monitor config contains unknown fields")
        payload = dict(value)
        if "markets" in payload:
            markets = payload["markets"]
            if isinstance(markets, (str, bytes)) or not isinstance(
                markets,
                Sequence,
            ):
                raise ValueError("markets must be a sequence")
            payload["markets"] = tuple(markets)
        return cls(**payload)


@dataclass(frozen=True, slots=True)
class RuntimeCycleResult:
    code: str
    occurred_at: datetime
    bar_closed_at: datetime | None = None
    event_id: str | None = None
    queued_reviews: int = 0
    queue_overflow: int = 0
    detail: str | None = None


@dataclass(frozen=True, slots=True)
class RuntimeHealth:
    enabled: bool
    scan_cycles: int
    scan_failures: int
    review_completed: int
    review_failures: int
    queue_depth: int
    queue_limit: int
    queue_overflow: int
    dead_letter_count: int
    last_bar_closed_at: datetime | None
    last_error: str | None


class DecisionSupportRuntime:
    def __init__(
        self,
        scanner: object,
        reviewer: Callable[[str], object],
        *,
        config: MonitorConfig | None = None,
        pending_review_loader: Callable[[], Sequence[str]] | None = None,
    ) -> None:
        scan = getattr(scanner, "scan_closed_bar", None)
        if not callable(scan):
            raise TypeError("scanner must provide scan_closed_bar")
        if not callable(reviewer):
            raise TypeError("reviewer must be callable")
        if config is not None and type(config) is not MonitorConfig:
            raise TypeError("config must be MonitorConfig")
        if pending_review_loader is not None and not callable(
            pending_review_loader
        ):
            raise TypeError("pending_review_loader must be callable")
        self._scanner = scanner
        self._reviewer = reviewer
        self.config = config or MonitorConfig()
        self._pending_review_loader = pending_review_loader
        self._queue: deque[str] = deque()
        self._queued: set[str] = set()
        self._in_review: set[str] = set()
        self._lock = Lock()
        self._scan_cycles = 0
        self._scan_failures = 0
        self._review_completed = 0
        self._review_failures = 0
        self._queue_overflow = 0
        self._review_attempts: dict[str, int] = {}
        self._dead_lettered: set[str] = set()
        self._last_bar_closed_at: datetime | None = None
        self._last_error: str | None = None
        self._review_day = None
        self._reviews_today = 0
        self._mutation_fence = MutationLeaseGuard()

    def bind_strategy_run(self, strategy_run: object) -> None:
        self._mutation_fence.bind(strategy_run)

    @staticmethod
    def _closed_bar(asof: datetime) -> datetime | None:
        if asof.minute % 5 != 0:
            return None
        return asof.replace(second=0, microsecond=0)

    @staticmethod
    def _reviewable_event_ids(result: object) -> tuple[str, ...]:
        event_ids: list[str] = []
        seen: set[str] = set()
        for field_name in ("trend_candidates", "reversal_candidates"):
            candidates = getattr(result, field_name, ())
            if isinstance(candidates, (str, bytes)) or not isinstance(
                candidates,
                Sequence,
            ):
                raise TypeError("scanner candidates must be sequences")
            for candidate in candidates:
                evaluation = getattr(candidate, "rule_evaluation", None)
                event = getattr(candidate, "event", None)
                event_id = getattr(event, "event_id", None)
                if (
                    getattr(candidate, "accepted", None) is not True
                    or getattr(evaluation, "safe_to_proceed", None) is not True
                    or not isinstance(event_id, str)
                    or not event_id
                    or event_id in seen
                ):
                    continue
                seen.add(event_id)
                event_ids.append(event_id)
        return tuple(event_ids)

    def _enqueue(self, event_id: str) -> tuple[bool, bool]:
        if not isinstance(event_id, str) or not event_id:
            raise ValueError("event_id must be a non-empty string")
        with self._lock:
            if (
                event_id in self._queued
                or event_id in self._in_review
                or event_id in self._dead_lettered
            ):
                return False, False
            if len(self._queue) >= self.config.review_queue_limit:
                self._queue_overflow += 1
                return False, True
            self._queue.append(event_id)
            self._queued.add(event_id)
            return True, False

    @mutation_fenced("decision_support_runtime.scan_cycle")
    def scan_cycle(self, asof: datetime) -> RuntimeCycleResult:
        self._mutation_fence.require()
        occurred_at = normalize_datetime(asof, "asof")
        if not self.config.enabled:
            return RuntimeCycleResult("disabled", occurred_at)
        bar_closed_at = self._closed_bar(occurred_at)
        if bar_closed_at is None:
            return RuntimeCycleResult("bar_not_closed", occurred_at)
        try:
            result = self._scanner.scan_closed_bar(bar_closed_at)
            scan_code = getattr(result, "code", None)
            failures = getattr(result, "failures", None)
            if not isinstance(scan_code, str) or not scan_code:
                raise TypeError("scanner result code must be a non-empty string")
            if isinstance(failures, (str, bytes)) or not isinstance(
                failures,
                Sequence,
            ):
                raise TypeError("scanner failures must be a sequence")
            if scan_code != "ok" or failures:
                detail = (
                    scan_code
                    if scan_code != "ok"
                    else "scanner_failures_present"
                )
                with self._lock:
                    self._scan_failures += 1
                    self._last_error = detail
                return RuntimeCycleResult(
                    "scan_incomplete",
                    occurred_at,
                    bar_closed_at=bar_closed_at,
                    detail=detail,
                )
            event_ids = self._reviewable_event_ids(result)
        except Exception as exc:
            with self._lock:
                self._scan_failures += 1
                self._last_error = type(exc).__name__
            return RuntimeCycleResult(
                "scan_failed",
                occurred_at,
                bar_closed_at=bar_closed_at,
                detail=type(exc).__name__,
            )

        with self._lock:
            pending = tuple(
                event_id
                for event_id in event_ids
                if event_id not in self._queued
                and event_id not in self._in_review
                and event_id not in self._dead_lettered
            )
            available = self.config.review_queue_limit - len(self._queue)
            overflow = max(0, len(pending) - available)
            if overflow:
                self._queue_overflow += overflow
                self._scan_failures += 1
                self._last_error = "review_queue_overflow"
                return RuntimeCycleResult(
                    "scan_incomplete",
                    occurred_at,
                    bar_closed_at=bar_closed_at,
                    queue_overflow=overflow,
                    detail="review_queue_overflow",
                )
            self._queue.extend(pending)
            self._queued.update(pending)
            self._scan_cycles += 1
            self._last_bar_closed_at = bar_closed_at
            self._last_error = None
        return RuntimeCycleResult(
            "scan_complete",
            occurred_at,
            bar_closed_at=bar_closed_at,
            queued_reviews=len(pending),
        )

    @mutation_fenced("decision_support_runtime.review_cycle")
    def review_cycle(self, asof: datetime | None = None) -> RuntimeCycleResult:
        self._mutation_fence.require()
        occurred_at = normalize_datetime(
            asof or datetime.now(timezone.utc),
            "asof",
        )
        if not self.config.enabled:
            return RuntimeCycleResult("disabled", occurred_at)
        day = occurred_at.date()
        with self._lock:
            if day != self._review_day:
                self._review_day = day
                self._reviews_today = 0
            if self._reviews_today >= self.config.max_llm_reviews_per_day:
                return RuntimeCycleResult("review_quota_reached", occurred_at)
            if not self._queue:
                return RuntimeCycleResult("review_idle", occurred_at)
            event_id = self._queue.popleft()
            self._queued.remove(event_id)
            self._in_review.add(event_id)
        try:
            self._reviewer(event_id)
        except Exception as exc:
            with self._lock:
                self._in_review.discard(event_id)
                self._review_failures += 1
                self._last_error = type(exc).__name__
                attempts = self._review_attempts.get(event_id, 0) + 1
                self._review_attempts[event_id] = attempts
                abandoned = attempts >= 2
                if abandoned:
                    self._dead_lettered.add(event_id)
            if not abandoned:
                self._enqueue(event_id)
            return RuntimeCycleResult(
                (
                    "review_abandoned"
                    if abandoned
                    else "review_failed_retrying"
                ),
                occurred_at,
                event_id=event_id,
                detail=type(exc).__name__,
            )
        with self._lock:
            self._in_review.discard(event_id)
            self._review_attempts.pop(event_id, None)
            self._review_completed += 1
            self._reviews_today += 1
            self._last_error = None
        return RuntimeCycleResult(
            "review_complete",
            occurred_at,
            event_id=event_id,
        )

    @mutation_fenced("decision_support_runtime.restore_pending_reviews")
    def restore_pending_reviews(self) -> int:
        self._mutation_fence.require()
        if not self.config.enabled or self._pending_review_loader is None:
            return 0
        values = self._pending_review_loader()
        if isinstance(values, (str, bytes)) or not isinstance(values, Sequence):
            raise TypeError("pending review loader must return a sequence")
        restored = 0
        for event_id in values:
            added, full = self._enqueue(event_id)
            restored += int(added)
            if full:
                raise RuntimeError("pending review restore overflow")
        return restored

    def health(self) -> RuntimeHealth:
        with self._lock:
            return RuntimeHealth(
                enabled=self.config.enabled,
                scan_cycles=self._scan_cycles,
                scan_failures=self._scan_failures,
                review_completed=self._review_completed,
                review_failures=self._review_failures,
                queue_depth=len(self._queue),
                queue_limit=self.config.review_queue_limit,
                queue_overflow=self._queue_overflow,
                dead_letter_count=len(self._dead_lettered),
                last_bar_closed_at=self._last_bar_closed_at,
                last_error=self._last_error,
            )


def register_decision_support_jobs(
    scheduler: object,
    runtime: DecisionSupportRuntime,
) -> dict[str, object]:
    if not isinstance(runtime, DecisionSupportRuntime):
        raise TypeError("runtime must be DecisionSupportRuntime")
    add_job = getattr(scheduler, "add_job", None)
    if not callable(add_job):
        raise TypeError("scheduler must provide add_job")
    if not runtime.config.enabled:
        return {}

    def scan_job() -> RuntimeCycleResult:
        return runtime.scan_cycle(datetime.now(timezone.utc))

    common = {
        "trigger": "interval",
        "replace_existing": True,
        "max_instances": 1,
        "coalesce": True,
        "misfire_grace_time": runtime.config.scan_interval_seconds,
    }
    scan = add_job(
        scan_job,
        id="decision_support_scan",
        seconds=runtime.config.scan_interval_seconds,
        **common,
    )
    review = add_job(
        runtime.review_cycle,
        id="decision_support_review",
        seconds=max(1, min(5, runtime.config.scan_interval_seconds)),
        **common,
    )
    return {"scan": scan, "review": review}
