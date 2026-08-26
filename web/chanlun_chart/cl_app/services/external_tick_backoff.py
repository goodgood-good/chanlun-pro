"""Shared failure backoff for external-market quote probes.

Browser tabs already retry independently.  This service adds one process-wide,
per-market gate so several tabs cannot keep calling the same unavailable
provider at the same time.  A-share QMT quotes deliberately do not use this
gate; callers opt in only for external markets.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
import math
from threading import RLock
import time


@dataclass(frozen=True)
class TickProbeDecision:
    allowed: bool
    reason_code: str
    retry_after_seconds: int
    failure_count: int


@dataclass
class _MarketBackoffState:
    failure_count: int = 0
    retry_at: float = 0.0
    probe_in_flight: bool = False
    probe_started_at: float | None = None


class ExternalMarketTickBackoff:
    """Coordinate one external quote probe per market with bounded backoff."""

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
        self._states: dict[str, _MarketBackoffState] = {}

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
                    )
                # A terminated request must not leave a market closed forever.
                state.probe_in_flight = False
                state.probe_started_at = None
            if state.retry_at > now:
                return TickProbeDecision(
                    allowed=False,
                    reason_code="PROVIDER_FAILURE_BACKOFF",
                    retry_after_seconds=max(1, math.ceil(state.retry_at - now)),
                    failure_count=state.failure_count,
                )
            state.probe_in_flight = True
            state.probe_started_at = now
            return TickProbeDecision(
                allowed=True,
                reason_code="PROBE_ALLOWED",
                retry_after_seconds=0,
                failure_count=state.failure_count,
            )

    def record_failure(self, market: str) -> TickProbeDecision:
        normalized = self._market(market)
        now = float(self._clock())
        with self._lock:
            state = self._states.setdefault(normalized, _MarketBackoffState())
            state.failure_count += 1
            delay = self._retry_delays_seconds[
                min(state.failure_count - 1, len(self._retry_delays_seconds) - 1)
            ]
            state.retry_at = now + delay
            state.probe_in_flight = False
            state.probe_started_at = None
            return TickProbeDecision(
                allowed=False,
                reason_code="PROVIDER_FAILURE_BACKOFF",
                retry_after_seconds=max(1, math.ceil(delay)),
                failure_count=state.failure_count,
            )

    def record_success(self, market: str) -> None:
        normalized = self._market(market)
        with self._lock:
            self._states.pop(normalized, None)

    @staticmethod
    def _market(value: str) -> str:
        normalized = str(value or "").strip().lower()
        if not normalized:
            raise ValueError("market must be non-empty")
        return normalized


__all__ = ("ExternalMarketTickBackoff", "TickProbeDecision")
