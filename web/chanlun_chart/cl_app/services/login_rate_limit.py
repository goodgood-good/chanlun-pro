"""Small in-process login limiter for the single-process desktop server."""

from __future__ import annotations

import threading
import time
from collections import deque

from cachetools import TTLCache


class LoginRateLimiter:
    def __init__(
        self,
        max_failures=5,
        window_seconds=300,
        block_seconds=900,
        max_entries=2048,
        clock=None,
    ):
        self.max_failures = int(max_failures)
        self.window_seconds = float(window_seconds)
        self.block_seconds = float(block_seconds)
        self._clock = clock or time.monotonic
        self.max_entries = int(max_entries)
        self._states = TTLCache(
            maxsize=self.max_entries,
            ttl=max(self.window_seconds, self.block_seconds) * 2,
            timer=self._clock,
        )
        self._lock = threading.Lock()

    def is_blocked(self, key: str) -> bool:
        now = self._clock()
        with self._lock:
            state = self._states.get(key)
            if state is None:
                return False
            until = state["blocked_until"]
            if until > now:
                return True
            state["blocked_until"] = 0
            self._states[key] = state
            return False

    def record_failure(self, key: str) -> bool:
        now = self._clock()
        cutoff = now - self.window_seconds
        with self._lock:
            state = self._states.get(
                key, {"failures": deque(), "blocked_until": 0}
            )
            failures = state["failures"]
            while failures and failures[0] < cutoff:
                failures.popleft()
            failures.append(now)
            if len(failures) >= self.max_failures:
                state["blocked_until"] = now + self.block_seconds
                failures.clear()
                self._states[key] = state
                return True
            self._states[key] = state
            return False

    def clear(self, key: str) -> None:
        with self._lock:
            self._states.pop(key, None)

    def tracked_keys(self) -> int:
        with self._lock:
            return len(self._states)
