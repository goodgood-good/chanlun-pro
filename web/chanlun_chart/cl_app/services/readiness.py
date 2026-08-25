"""Thread-safe in-process readiness state."""

import threading
import time


class WarmupHandle:
    """Small stoppable thread handle used by dependency warmups."""

    def __init__(self, stop_event, thread):
        self._stop_event = stop_event
        self._thread = thread

    def stop(self):
        self._stop_event.set()

    def join(self, timeout=None):
        self._thread.join(timeout)

    def is_alive(self):
        return self._thread.is_alive()

    @property
    def name(self):
        return self._thread.name


def _start_daemon_attempt(fn, name):
    done = threading.Event()
    outcome = {"value": None, "error": None}

    def _target():
        try:
            outcome["value"] = fn()
        except BaseException as exc:
            outcome["error"] = exc
        finally:
            done.set()

    thread = threading.Thread(target=_target, daemon=True, name=name)
    thread.start()
    return {"done": done, "outcome": outcome, "thread": thread}


def _wait_attempt(attempt, stop_event, timeout):
    deadline = time.monotonic() + max(0.0, float(timeout))
    while not attempt["done"].is_set():
        if stop_event.is_set():
            return "stopped"
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return "timeout"
        attempt["done"].wait(min(0.05, remaining))
    return "done"


def _wait_for_late_attempt(attempt, stop_event, retry_seconds):
    # ``retry_seconds`` controls the delay before a *new* dependency attempt.
    # It must not also delay observing the current attempt after its owner
    # timeout.  A quote call that finishes at 5.1s used to stay invisible for
    # the whole 30s retry interval, making /readyz look down for ~35s.  Poll the
    # attempt completion event at a short bounded cadence while still allowing
    # shutdown to interrupt promptly.
    poll_seconds = min(0.05, max(0.01, float(retry_seconds)))
    while not attempt["done"].is_set():
        if stop_event.is_set():
            return False
        attempt["done"].wait(poll_seconds)
    return True


def start_metadata_warmup(
    constants_service,
    market: str = "a",
    retry_seconds: float = 30.0,
    attempt_timeout: float = 6.0,
):
    """Warm metadata with bounded owner attempts and a stoppable handle."""
    stop_event = threading.Event()

    def _mapping_ready(mapping):
        try:
            return bool(mapping.status(market).get("ready"))
        except Exception:
            return False

    def _worker():
        mappings = (
            constants_service.market_frequencys,
            constants_service.market_default_codes,
        )
        while not stop_event.is_set():
            for index, mapping in enumerate(mappings):
                if _mapping_ready(mapping):
                    continue
                attempt = _start_daemon_attempt(
                    lambda current=mapping: current[market],
                    f"MetadataWarmupAttempt-{market}-{index}",
                )
                status = _wait_attempt(attempt, stop_event, attempt_timeout)
                if status == "stopped":
                    return
                if status == "timeout" and not _wait_for_late_attempt(
                    attempt, stop_event, retry_seconds
                ):
                    return
            if all(_mapping_ready(mapping) for mapping in mappings):
                return
            if stop_event.wait(max(0.0, float(retry_seconds))):
                return

    thread = threading.Thread(
        target=_worker,
        daemon=True,
        name=f"MetadataWarmup-{market}",
    )
    thread.start()
    return WarmupHandle(stop_event, thread)


def _has_probe_data(value):
    if value is None:
        return False
    empty = getattr(value, "empty", None)
    if isinstance(empty, bool):
        return not empty
    try:
        return len(value) > 0
    except (TypeError, AttributeError):
        return bool(value)


def start_ticks_warmup(
    registry,
    probe,
    market: str = "a",
    retry_seconds: float = 30.0,
    attempt_timeout: float = 5.0,
):
    """Probe ticks until valid data arrives, without blocking shutdown."""
    stop_event = threading.Event()

    def _worker():
        while not stop_event.is_set():
            attempt = _start_daemon_attempt(
                lambda: probe(market),
                f"TicksWarmupAttempt-{market}",
            )
            status = _wait_attempt(attempt, stop_event, attempt_timeout)
            if status == "stopped":
                return
            if status == "timeout":
                registry.record_ticks_failure(
                    market,
                    "warmup_timeout",
                    f"ticks warmup timed out after {max(0.0, float(attempt_timeout)):g}s",
                )
                if not _wait_for_late_attempt(attempt, stop_event, retry_seconds):
                    return

            error = attempt["outcome"]["error"]
            value = attempt["outcome"]["value"]
            if error is not None:
                registry.record_ticks_failure(
                    market,
                    "warmup_error",
                    (str(error) or type(error).__name__)[:200],
                )
            elif _has_probe_data(value):
                registry.record_ticks_success(market)
            else:
                registry.record_ticks_failure(
                    market,
                    "empty_ticks",
                    "ticks warmup returned no data",
                )
            if stop_event.wait(max(0.0, float(retry_seconds))):
                return

    thread = threading.Thread(
        target=_worker,
        daemon=True,
        name=f"TicksWarmup-{market}",
    )
    thread.start()
    return WarmupHandle(stop_event, thread)

class ReadinessRegistry:
    """Track the latest dependency result without performing readiness I/O."""

    def __init__(
        self,
        tick_error_ttl=30.0,
        tick_success_ttl=90.0,
        clock=time.monotonic,
    ):
        self._lock = threading.Lock()
        self._ticks = {}
        self._tick_error_ttl = max(0.0, float(tick_error_ttl))
        self._tick_success_ttl = max(0.0, float(tick_success_ttl))
        self._clock = clock

    def record_ticks_success(self, market: str) -> None:
        with self._lock:
            self._ticks[market] = {
                "status": "ok",
                "error": None,
                "recorded_at": self._clock(),
            }

    def record_ticks_failure(self, market: str, code: str, message: str) -> None:
        with self._lock:
            self._ticks[market] = {
                "status": "error",
                "error": {"code": code, "message": message},
                "recorded_at": self._clock(),
            }

    def ticks_snapshot(self, market: str):
        with self._lock:
            state = self._ticks.get(market)
            if state is None:
                return {
                    "required": True,
                    "ready": False,
                    "status": "unknown",
                    "error": None,
                }

            error = state["error"]
            if (
                state["status"] == "ok"
                and self._clock() - state["recorded_at"]
                >= self._tick_success_ttl
            ):
                return {
                    "required": True,
                    "ready": False,
                    "status": "stale",
                    "error": None,
                }
            if (
                state["status"] == "error"
                and error is not None
                and self._clock() - state["recorded_at"] >= self._tick_error_ttl
            ):
        # 错误详情可以随时间淘汰，但失败依赖在真实行情请求成功前始终保持未就绪。
                state["error"] = None
                error = None

            return {
                "required": True,
                "ready": state["status"] == "ok",
                "status": state["status"],
                "error": dict(error) if error is not None else None,
            }
