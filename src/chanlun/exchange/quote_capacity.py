"""Bound worker quote connections without reducing the CPU calculation pool.

Only spawned screening workers participate. The web process keeps its shared
provider connection. A worker owns at most one of four process-safe slots until
its provider context is closed; idle reusable workers retain no quote socket.
"""
from contextlib import ExitStack, contextmanager
import multiprocessing
import os
from pathlib import Path
import sys
import threading
import time

from chanlun.persistence.file_lock import interprocess_file_lock, InterprocessLockTimeout

WORKER_QUOTE_SLOTS = 4
WAIT_SECONDS = 30.0
_worker_pid = None
_run_dir = None
_slot_stack = None
_lock = threading.RLock()
_request = threading.local()


class QuoteConnectionCancelled(RuntimeError):
    """The owning calculation stopped; another attempt cannot revive it."""


class QuoteConnectionBusy(TimeoutError):
    """Local quote admission is pending; no provider request has failed."""
    retryable = False
    code = 'quote_connection_capacity_busy'
    retry_after = 2


def is_worker_quote_process():
    return _worker_pid == os.getpid()


def worker_quote_cancelled():
    event=getattr(_request,'cancel',None)
    return bool(event is not None and event.is_set()) or (
        is_worker_quote_process() and (_run_dir is None or (_run_dir/'cancel').exists()))


def current_quote_cancel_event():
    """Carry a request's cancellation through provider executor boundaries."""
    return getattr(_request,'cancel',None)


@contextmanager
def quote_request_scope(cancel):
    previous=getattr(_request,'cancel',None)
    _request.cancel=cancel
    try:
        yield
    finally:
        _request.cancel=previous


def _budget_root():
    from chanlun import config
    return config.get_data_path() / "quote_budget" / "longbridge"


def acquire_worker_quote_slot():
    """Called at context creation, including provider executor threads."""
    global _slot_stack
    if not is_worker_quote_process():
        return
    with _lock:
        if _slot_stack is not None:
            return
        if _run_dir is None:
            raise QuoteConnectionCancelled("Idle screening worker cannot open a quote connection")
        from chanlun import config
        budget=min(WAIT_SECONDS,.25) if str(getattr(config,'US_HISTORY_KLINE_SOURCE','')).lower()=='dual' else WAIT_SECONDS
        deadline = time.monotonic() + budget
        while True:
            if worker_quote_cancelled():
                raise QuoteConnectionCancelled("Quote connection wait cancelled")
            for index in range(WORKER_QUOTE_SLOTS):
                stack = ExitStack()
                try:
                    stack.enter_context(interprocess_file_lock(
                        _budget_root() / f"worker_{index}.lock",
                        timeout_seconds=.001, poll_seconds=.001))
                except InterprocessLockTimeout:
                    stack.close()
                    continue
                except BaseException:
                    stack.close()
                    raise
                _slot_stack = stack
                return
            if time.monotonic() >= deadline:
                raise QuoteConnectionBusy("Worker quote connection capacity is busy")
            time.sleep(.025)


def release_worker_quote_slot():
    """Provider calls this only after closing its connection; idempotent."""
    global _slot_stack
    if not is_worker_quote_process():
        return
    with _lock:
        if _slot_stack is not None:
            _slot_stack.close()
            _slot_stack = None


def release_worker_quote_resources():
    """Do not import or initialize a provider solely to clean up a worker."""
    if not is_worker_quote_process():
        return
    provider = sys.modules.get("chanlun.exchange.exchange_cq")
    cleanup = getattr(provider, "release_worker_quote_context", None)
    if callable(cleanup):
        cleanup()
    else:
        release_worker_quote_slot()


@contextmanager
def worker_quote_scope(run_dir):
    """Identify the dedicated worker process and clean up every task outcome."""
    global _worker_pid, _run_dir
    if multiprocessing.current_process().name == "MainProcess":
        yield
        return
    _worker_pid = os.getpid()
    _run_dir = Path(run_dir)
    try:
        yield
    finally:
        try:
            release_worker_quote_resources()
        finally:
            # Keep the worker identity while idle: heartbeat must not reconnect.
            _run_dir = None
