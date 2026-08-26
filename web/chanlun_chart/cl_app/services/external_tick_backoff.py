"""Single-flight coordination and failure backoff for external quote probes.

This service gives all browser tabs one process-wide probe per market, briefly
shares its real normalized result, and backs off genuine provider failures.
A-share QMT quotes deliberately do not use this coordinator.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
import copy
from dataclasses import dataclass
import math
from threading import Condition, RLock
import time


@dataclass(frozen=True)
class TickProbeDecision:
    allowed: bool
    reason_code: str
    retry_after_seconds: int
    failure_count: int
    probe_started_at: float | None = None
    probe_id: int | None = None


@dataclass(frozen=True)
class SharedTickSuccess:
    """A defensive copy of one recent, real provider response."""

    payload: dict[str, object]
    age_seconds: float


@dataclass
class _MarketBackoffState:
    failure_count: int = 0
    retry_at: float = 0.0
    probe_in_flight: bool = False
    probe_started_at: float | None = None
    probe_id: int | None = None


@dataclass
class _RecentTickSuccess:
    requested_codes: frozenset[str]
    response_payload: dict[str, object]
    recorded_at: float


class ExternalMarketTickBackoff:
    """Coordinate, share, and back off external quote probes by market."""

    def __init__(
        self,
        *,
        clock: Callable[[], float] = time.monotonic,
        retry_delays_seconds: Sequence[float] = (15, 30, 60, 120, 300),
        stale_probe_seconds: float = 30.0,
    ) -> None:
        delays = tuple(float(value) for value in retry_delays_seconds)
        if not delays or any(not math.isfinite(value) or value <= 0 for value in delays):
            raise ValueError("retry_delays_seconds must contain positive finite values")
        if not math.isfinite(stale_probe_seconds) or stale_probe_seconds <= 0:
            raise ValueError("stale_probe_seconds must be positive and finite")
        self._clock = clock
        self._retry_delays_seconds = delays
        self._stale_probe_seconds = float(stale_probe_seconds)
        self._lock = RLock()
        self._condition = Condition(self._lock)
        self._states: dict[str, _MarketBackoffState] = {}
        self._next_probe_id = 0
        # Only one short-lived response is retained per external market. It is
        # never persisted and is served only when it covers the caller's codes.
        self._recent_successes: dict[str, _RecentTickSuccess] = {}

    def acquire(self, market: str) -> TickProbeDecision:
        """Return a permit or the remaining wait without calling the provider."""

        normalized = self._market(market)
        now = float(self._clock())
        with self._lock:
            state = self._states.setdefault(normalized, _MarketBackoffState())
            if state.probe_in_flight:
                started_at = state.probe_started_at
                age = 0.0 if started_at is None else max(0.0, now - started_at)
                if age < self._stale_probe_seconds:
                    return TickProbeDecision(
                        allowed=False,
                        reason_code="PROVIDER_PROBE_IN_FLIGHT",
                        retry_after_seconds=max(
                            1,
                            min(5, math.ceil(self._stale_probe_seconds - age)),
                        ),
                        failure_count=state.failure_count,
                        probe_started_at=started_at,
                        probe_id=state.probe_id,
                    )
                # A terminated request must not leave a market closed forever.
                state.probe_in_flight = False
                state.probe_started_at = None
                state.probe_id = None
            if state.retry_at > now:
                return TickProbeDecision(
                    allowed=False,
                    reason_code="PROVIDER_FAILURE_BACKOFF",
                    retry_after_seconds=max(1, math.ceil(state.retry_at - now)),
                    failure_count=state.failure_count,
                    probe_started_at=None,
                    probe_id=None,
                )
            self._next_probe_id += 1
            state.probe_in_flight = True
            state.probe_started_at = now
            state.probe_id = self._next_probe_id
            return TickProbeDecision(
                allowed=True,
                reason_code="PROBE_ALLOWED",
                retry_after_seconds=0,
                failure_count=state.failure_count,
                probe_started_at=now,
                probe_id=state.probe_id,
            )

    def record_failure(
        self,
        market: str,
        *,
        probe_id: int | None = None,
    ) -> TickProbeDecision:
        normalized = self._market(market)
        now = float(self._clock())
        with self._condition:
            state = self._states.get(normalized)
            if probe_id is not None and (
                state is None
                or not state.probe_in_flight
                or state.probe_id != probe_id
            ):
                return self._stale_completion_decision(state, now)
            if state is None:
                state = self._states.setdefault(normalized, _MarketBackoffState())
            state.failure_count += 1
            delay = self._retry_delays_seconds[
                min(state.failure_count - 1, len(self._retry_delays_seconds) - 1)
            ]
            state.retry_at = now + delay
            state.probe_in_flight = False
            state.probe_started_at = None
            state.probe_id = None
            decision = TickProbeDecision(
                allowed=False,
                reason_code="PROVIDER_FAILURE_BACKOFF",
                retry_after_seconds=max(1, math.ceil(delay)),
                failure_count=state.failure_count,
                probe_started_at=None,
                probe_id=None,
            )
            self._condition.notify_all()
            return decision

    def record_success(
        self,
        market: str,
        *,
        probe_id: int | None = None,
        requested_codes: Sequence[str] | None = None,
        response_payload: Mapping[str, object] | None = None,
    ) -> bool:
        """Close the probe and optionally publish its normalized response."""

        normalized = self._market(market)
        if (requested_codes is None) != (response_payload is None):
            raise ValueError(
                "requested_codes and response_payload must be provided together"
            )
        codes = None if requested_codes is None else self._codes(requested_codes)
        with self._condition:
            state = self._states.get(normalized)
            if probe_id is not None and (
                state is None
                or not state.probe_in_flight
                or state.probe_id != probe_id
            ):
                return False
            cached = None
            if codes is not None and response_payload is not None:
                cached = _RecentTickSuccess(
                    requested_codes=codes,
                    response_payload=copy.deepcopy(dict(response_payload)),
                    recorded_at=float(self._clock()),
                )
            self._states.pop(normalized, None)
            if cached is not None:
                self._recent_successes[normalized] = cached
            self._condition.notify_all()
            return True

    def recent_success(
        self,
        market: str,
        requested_codes: Sequence[str],
        *,
        max_age_seconds: float,
    ) -> SharedTickSuccess | None:
        """Return a recent real response that covers all requested codes."""

        normalized = self._market(market)
        codes = self._codes(requested_codes)
        max_age = self._duration(max_age_seconds, "max_age_seconds")
        with self._lock:
            return self._recent_success_locked(
                normalized,
                codes,
                max_age_seconds=max_age,
                not_before=None,
            )

    def wait_for_success(
        self,
        market: str,
        requested_codes: Sequence[str],
        *,
        not_before: float | None,
        timeout_seconds: float,
        max_age_seconds: float,
    ) -> SharedTickSuccess | None:
        """Wait briefly for the in-flight provider probe and share its result."""

        normalized = self._market(market)
        codes = self._codes(requested_codes)
        timeout = self._duration(timeout_seconds, "timeout_seconds")
        max_age = self._duration(max_age_seconds, "max_age_seconds")
        lower_bound = None if not_before is None else float(not_before)
        if lower_bound is not None and not math.isfinite(lower_bound):
            raise ValueError("not_before must be finite")
        deadline = time.monotonic() + timeout
        with self._condition:
            while True:
                shared = self._recent_success_locked(
                    normalized,
                    codes,
                    max_age_seconds=max_age,
                    not_before=lower_bound,
                )
                if shared is not None:
                    return shared
                state = self._states.get(normalized)
                if state is None or not state.probe_in_flight:
                    return None
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return None
                self._condition.wait(remaining)

    def _recent_success_locked(
        self,
        market: str,
        requested_codes: frozenset[str],
        *,
        max_age_seconds: float,
        not_before: float | None,
    ) -> SharedTickSuccess | None:
        cached = self._recent_successes.get(market)
        if cached is None or not requested_codes.issubset(cached.requested_codes):
            return None
        if not_before is not None and cached.recorded_at < not_before:
            return None
        age = max(0.0, float(self._clock()) - cached.recorded_at)
        if age > max_age_seconds:
            return None
        return SharedTickSuccess(
            payload=copy.deepcopy(cached.response_payload),
            age_seconds=age,
        )

    @staticmethod
    def _codes(values: Sequence[str]) -> frozenset[str]:
        if isinstance(values, (str, bytes)):
            raise ValueError("requested_codes must be a sequence of strings")
        normalized = frozenset(values)
        if any(not isinstance(value, str) or not value for value in normalized):
            raise ValueError("requested_codes must contain non-empty strings")
        return normalized

    @staticmethod
    def _duration(value: float, name: str) -> float:
        normalized = float(value)
        if not math.isfinite(normalized) or normalized < 0:
            raise ValueError(f"{name} must be finite and non-negative")
        return normalized

    def _stale_completion_decision(
        self,
        state: _MarketBackoffState | None,
        now: float,
    ) -> TickProbeDecision:
        if state is not None and state.probe_in_flight:
            started_at = state.probe_started_at
            age = 0.0 if started_at is None else max(0.0, now - started_at)
            return TickProbeDecision(
                allowed=False,
                reason_code="PROVIDER_PROBE_IN_FLIGHT",
                retry_after_seconds=max(
                    1,
                    min(5, math.ceil(self._stale_probe_seconds - age)),
                ),
                failure_count=state.failure_count,
                probe_started_at=started_at,
                probe_id=state.probe_id,
            )
        if state is not None and state.retry_at > now:
            return TickProbeDecision(
                allowed=False,
                reason_code="PROVIDER_FAILURE_BACKOFF",
                retry_after_seconds=max(1, math.ceil(state.retry_at - now)),
                failure_count=state.failure_count,
                probe_started_at=None,
                probe_id=None,
            )
        return TickProbeDecision(
            allowed=False,
            reason_code="STALE_PROBE_COMPLETION",
            retry_after_seconds=1,
            failure_count=0 if state is None else state.failure_count,
            probe_started_at=None,
            probe_id=None,
        )

    @staticmethod
    def _market(value: str) -> str:
        normalized = str(value or "").strip().lower()
        if not normalized:
            raise ValueError("market must be non-empty")
        return normalized


__all__ = (
    "ExternalMarketTickBackoff",
    "SharedTickSuccess",
    "TickProbeDecision",
)
