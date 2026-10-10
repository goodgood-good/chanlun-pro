"""Bounded, process-local CL continuation for screening (never audit replays)."""

from collections import OrderedDict
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
import os
from pathlib import Path
import threading

from .cache import input_fingerprint


# A reusable worker can hold several symbols without retaining a whole market.
# Disk calculation snapshots still cover unchanged inputs after worker eviction.
MAX_ENTRIES = 12
MAX_SOURCE_BARS = 40_000
_entries = OrderedDict()
_registry_lock = threading.RLock()
_stripes = tuple(threading.RLock() for _ in range(32))
_pid = os.getpid()
_scope = ContextVar("screening_incremental_runtime", default=None)


@dataclass
class _Entry:
    cd: object
    fingerprint: str
    bars: int
    priority: bool = False


@contextmanager
def incremental_snapshot_scope(namespace, revision, *, enabled=True, priority=False):
    """Opt in only around a live calculation, outside every independent replay."""
    metrics = {"mode": "cold", "reused_bars": 0, "appended_bars": 0}
    scope = (str(Path(namespace).resolve()), revision, metrics, priority) if enabled and namespace is not None else None
    token = _scope.set(scope)
    try:
        yield metrics
    finally:
        _scope.reset(token)


def clear_runtime_cache():
    """Drop optional calculation state; frozen evidence never uses this cache."""
    with _registry_lock:
        _entries.clear()


@contextmanager
def runtime_for_snapshot(frame, code, frequency):
    """Lock one CL through processing and rendering, then publish its prefix.

    Revision, market and storage namespace isolate workers' calculation state.
    A full input prefix hash includes *every* column and semantic attribute.
    Rolling windows, corrections, backfills and any rule/price/time change
    therefore cold-build. Failed processing or rendering discards mutated CL.
    No runtime object or pickle is deserialized from external files.
    """
    from chanlun.cl_utils.strict_chart_runtime import build_strict_chart_cd, StrictChartRuntimeResult

    market = frame.attrs.get("screening_market", "a")
    scope = _scope.get()
    if scope is None:
        yield build_strict_chart_cd(market=market, code=code, frequency=frequency,
                                   frame=frame, last_bar_closed=True)
        return
    namespace, revision, metrics, priority = scope
    global _pid
    with _registry_lock:
        if _pid != os.getpid():
            _entries.clear()
            _pid = os.getpid()
    identity = (namespace, market, code, frequency, revision)
    with _stripes[hash(identity) % len(_stripes)]:
        # Remove ownership during mutation: readers of other keys cannot evict
        # and then republish this object. Same-key readers serialize here.
        with _registry_lock:
            entry = _entries.pop(identity, None)
        fingerprint = input_fingerprint(frame, code, frequency)
        reusable = entry is not None and (
            (entry.bars == len(frame) and entry.fingerprint == fingerprint) or
            (entry.bars < len(frame) and
             input_fingerprint(frame.iloc[:entry.bars], code, frequency) == entry.fingerprint)
        )
        if reusable:
            if entry.bars < len(frame):
                entry.cd.process_validated_incremental_klines(frame, last_bar_closed=True)
                metrics.update(mode="append", reused_bars=entry.bars,
                               appended_bars=len(frame) - entry.bars)
            else:
                metrics.update(mode="unchanged", reused_bars=entry.bars)
            runtime = StrictChartRuntimeResult.success(entry.cd)
        else:
            runtime = build_strict_chart_cd(market=market, code=code, frequency=frequency,
                                           frame=frame, last_bar_closed=True)
        # Exceptions from the renderer leave no potentially tainted state.
        yield runtime
        if runtime.cd is not None and len(frame) <= MAX_SOURCE_BARS:
            with _registry_lock:
                _entries[identity] = _Entry(runtime.cd, fingerprint, len(frame), priority)
                while len(_entries) > MAX_ENTRIES or sum(item.bars for item in _entries.values()) > MAX_SOURCE_BARS:
                    # Held 5m/30m positions survive ordinary universe churn;
                    # the same hard caps still apply if holdings fill the cache.
                    victim = next((key for key, item in _entries.items() if not item.priority), next(iter(_entries)))
                    del _entries[victim]
