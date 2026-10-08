"""Nonblocking progress checks for otherwise-live background threads."""

from contextlib import contextmanager
import time


class WorkerProgress:
    def __init__(self, timeout_seconds):
        self.timeout_seconds = timeout_seconds
        self._active = None

    @contextmanager
    def cycle(self, phase):
        self._active = (time.monotonic(), phase)
        try:
            yield
        finally:
            self._active = None

    def phase(self, name):
        active = self._active
        if active is not None:
            # Changing phase must not reset the total execution deadline.
            self._active = (active[0], name)

    def snapshot(self):
        active = self._active
        elapsed = max(0, time.monotonic() - active[0]) if active else 0
        return {
            "state": "stalled"
            if elapsed > self.timeout_seconds
            else "running"
            if active
            else "idle",
            "phase": active[1] if active else None,
            "elapsed_seconds": round(elapsed, 3),
            "timeout_seconds": self.timeout_seconds,
        }
