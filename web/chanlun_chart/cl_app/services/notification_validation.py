"""Bound dependency reads without abandoning a task that can change signals.

Only read-only dependency functions belong here. A completed read can resume
the same request briefly; the caller alone may inspect state and authorize it.
"""

from concurrent.futures import Future
import threading
import time


VALIDATION_BUDGET_SECONDS = .2
COMPLETED_RESULT_TTL_SECONDS = 1
MAX_DEPENDENCY_AGE_SECONDS = 2


class DeferredEvents(list):
    """A read budget yielded; retry sooner than a genuinely unknown proof."""


def retry_delay(events):
    return .5 if isinstance(events, DeferredEvents) else 5


def validation_deadline():
    return time.monotonic() + VALIDATION_BUDGET_SECONDS


def remaining(deadline):
    return max(0, deadline - time.monotonic())


def state_epoch(state, control_version, events):
    # This epoch belongs to the lane, so two different held symbols share it.
    # Event-specific admission times are still checked against live state.
    membership = (tuple(sorted(state.get('holding_symbols', []))),
                  tuple(sorted(state.get('holding_notification_since', {}).items())))
    return (control_version, state.get('enabled'), state.get('dingtalk_enabled'),
            state.get('notification_since'), state.get('holding_source_current'), membership,
            tuple(sorted(state.get('notification_frequency_since', {}).items())),
            tuple(sorted((state.get('seed') or {}).get('settings', {}).get('point_types', []))),
            state.get('consolidation_enabled_at'), state.get('consolidation_warning_enabled_at'))


class DependencyReads:
    """At most one in-flight read per priority lane, with no waiting queue."""

    def __init__(self):
        self._lock = threading.Lock()
        self._lanes = {'ordinary': None, 'holding': None}

    def read(self, lane, reader, deadline, *, key=None, context_key=None):
        if time.monotonic() >= deadline:
            return None
        with self._lock:
            previous = self._lanes[lane]
            if previous is not None and not previous.done():
                return None
            if (previous is not None and key is not None and previous.context_key == context_key
                    and not previous.consumed
                    and time.monotonic() - previous.completed_at <= COMPLETED_RESULT_TTL_SECONDS
                    and time.monotonic() - previous.started_at <= MAX_DEPENDENCY_AGE_SECONDS):
                # Another due receipt must not repeatedly evict this request's
                # completed read before its short retry can consume it.
                if previous.request_key != key:
                    return None
                previous.consumed = True
                try:
                    return previous.result()
                except Exception:
                    return None
            future = Future()
            future.request_key = key
            future.context_key = context_key
            future.started_at = time.monotonic()
            future.consumed = False
            self._lanes[lane] = future

            def run():
                try:
                    result = reader()
                except Exception as exc:
                    future.completed_at = time.monotonic()
                    future.set_exception(exc)
                else:
                    future.completed_at = time.monotonic()
                    future.set_result(result)

            # A blocked dependency retains its single lane until it finishes.
            # Calls never enqueue work or start replacement threads for it.
            threading.Thread(target=run, name=f'NotificationRead-{lane}', daemon=True).start()
        try:
            result = future.result(timeout=max(0, deadline - time.monotonic()))
        except Exception:
            return None
        if time.monotonic() >= deadline:
            return None
        with self._lock:
            if future.consumed:
                return None
            future.consumed = True
        return result


def holding_lane(events):
    return any(event.get('holding') or event.get('signal_type') in {
        'holding_risk', 'consolidation_divergence'} for event in events)
