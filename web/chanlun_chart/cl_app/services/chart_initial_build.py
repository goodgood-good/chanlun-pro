"""Bounded on-demand structure builds after the first candles are published."""

import threading

from chanlun.tools.daemon_executor import DaemonExecutor
from chanlun.tools.log_util import LogUtil


PENDING_CODE = "strict_structure_pending"
_lock = threading.RLock()
_executor = None
_closed = False
_builds = {}


def is_initial_build_active(cache_key):
    with _lock:
        future = _builds.get(cache_key)
        return future is not None and not future.done()


def initial_builds_accepting_results():
    with _lock:
        return not _closed


def submit_initial_build(cache_key, build):
    """Only explicitly opened charts enter this queue; duplicate keys coalesce."""
    global _executor
    with _lock:
        if _closed:
            return False
        if is_initial_build_active(cache_key):
            return False
        if _executor is None:
            _executor = DaemonExecutor(
                max_workers=2, max_pending=16, thread_name_prefix="ChartInitialBuild"
            )
        try:
            future = _executor.submit(build)
        except RuntimeError:
            return False
        _builds[cache_key] = future

    def finished(result):
        with _lock:
            if _builds.get(cache_key) is result:
                _builds.pop(cache_key, None)
        if not result.cancelled() and result.exception() is not None:
            LogUtil.warning(f"[chart_initial_build] failed key={cache_key}")

    future.add_done_callback(finished)
    return True


def start_initial_build_runtime():
    global _closed
    with _lock:
        if _closed and _builds:
            raise RuntimeError("chart initial builds are still stopping")
        _closed = False


def shutdown_initial_build_runtime():
    global _closed, _executor
    with _lock:
        _closed = True
        executor, _executor = _executor, None
    if executor is not None:
        executor.shutdown(wait=False, cancel_futures=True)
