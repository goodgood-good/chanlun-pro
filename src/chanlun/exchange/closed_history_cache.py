"""Bounded reuse of complete Longbridge intraday history between US sessions.

This is a display-history cache with a one-hour full-validation deadline, not
proof that forward-adjusted historical prices can never be revised. Active
sessions, historical range queries and forced refreshes always use the provider.
"""

from datetime import timedelta
from functools import lru_cache
import hashlib
import json
from pathlib import Path
import threading
import time
import weakref

import pandas as pd

from chanlun.exchange._lookback import get_lookback_timedelta
from chanlun.tools.log_util import LogUtil


_MAX_AGE = 3600
_META = "longbridge_closed_history_cache"
_NAMESPACE = "longbridge_history"
_registry_lock = threading.Lock()
_locks = weakref.WeakValueDictionary()


def closed_us_session(observed):
    """Conservative regular-session boundary, with publication/opening grace.

    The caller exclusively requests TradeSessions.Intraday. Holidays may cause
    unnecessary misses, never an extension of reuse across a possible session.
    """
    local = pd.Timestamp(observed).tz_convert("America/New_York")
    minutes = local.hour * 60 + local.minute
    if local.weekday() < 5 and 9 * 60 + 20 <= minutes <= 16 * 60 + 20:
        return None
    day = local.date()
    if local.weekday() < 5 and minutes < 9 * 60 + 20:
        day -= timedelta(days=1)
    while day.weekday() >= 5:
        day -= timedelta(days=1)
    return day.isoformat()


@lru_cache(maxsize=1)
def _source_revision():
    root = Path(__file__).parent
    digest = hashlib.sha256()
    for name in ("closed_history_cache.py", "exchange_cq.py", "_lookback.py",
                 "price_basis.py", "kline_precision.py"):
        digest.update(name.encode())
        digest.update((root / name).read_bytes())
    return digest.hexdigest()


def _fingerprint(frame):
    values = frame[["date", "open", "high", "low", "close", "volume"]]
    digest = hashlib.sha256(pd.util.hash_pandas_object(values, index=False).values.tobytes())
    attrs = {key: value for key, value in frame.attrs.items() if key != _META}
    digest.update(json.dumps(attrs, sort_keys=True, separators=(",", ":")).encode())
    return digest.hexdigest()


def _chart_frame(frame, valid_until=None):
    result = frame.copy(deep=True)
    result.attrs = {key: value for key, value in frame.attrs.items() if key != _META}
    if valid_until is not None:
        result.attrs["_history_source_valid_until"] = valid_until
    return result


def load_closed_us_history(*, code, frequency, observed, end, canonical, loader, storage=None, force=False):
    """Reuse only a recent complete frame from this exact closed-session epoch."""
    session = closed_us_session(observed)
    if (session is None or abs((observed - end).total_seconds()) > 600
            or closed_us_session(end) != session):
        return loader()
    if storage is None:
        from chanlun.persistence.file_db import fdb
        storage = fdb
    storage_code = hashlib.sha256(code.encode()).hexdigest()[:32]
    storage_frequency = frequency + ("_canonical" if canonical else "")
    key = (storage_code, storage_frequency)
    with _registry_lock:
        lock = _locks.get(key)
        if lock is None:
            lock = threading.RLock()
            _locks[key] = lock
    started = time.perf_counter()
    with lock:
        revision = _source_revision()
        # A manual validation must also replace/invalidate the durable source,
        # otherwise a later algorithm rebuild could resurrect the old prices.
        if force:
            storage.save_klines_parquet(_NAMESPACE, *key, pd.DataFrame())
        cached = None if force else storage.load_klines_parquet(_NAMESPACE, *key)
        if cached is not None and not cached.empty:
            meta = cached.attrs.get(_META) or {}
            try:
                age = observed.timestamp() - float(meta["validated_at"])
                valid = (
                    0 <= age < _MAX_AGE and meta["session"] == session
                    and meta["revision"] == revision and meta["code"] == code
                    and meta["frequency"] == frequency and meta["canonical"] is canonical
                    and cached.attrs.get("price_basis_provider") == "longbridge"
                    and cached.attrs.get("price_basis_adjustment") == "forward"
                    and meta["fingerprint"] == _fingerprint(cached)
                )
                if valid:
                    if not canonical:
                        # Wall-clock lookbacks move their left edge even while
                        # closed; a canonical minute window is anchored to its
                        # actual last completed candle instead.
                        start = end - get_lookback_timedelta(frequency)
                        if start.timestamp() < meta["requested_start"]:
                            valid = False
                        else:
                            cached = cached.loc[cached.date >= start].reset_index(drop=True)
                    if valid and not cached.empty:
                        LogUtil.info(f"[history_source] {code} {frequency} cache=disk "
                                     f"rows={len(cached)} age={age:.0f}s "
                                     f"elapsed={(time.perf_counter() - started) * 1000:.0f}ms")
                        return _chart_frame(cached, float(meta["validated_at"]) + _MAX_AGE)
            except (KeyError, TypeError, ValueError, OverflowError):
                pass
        frame = loader()
        if (frame is None or frame.empty or frame.attrs.get("fetch_incomplete")
                or frame.attrs.get("price_basis_provider") != "longbridge"
                or frame.attrs.get("price_basis_adjustment") != "forward"):
            return frame
        snapshot = _chart_frame(frame)
        try:
            snapshot.attrs[_META] = {
                "session": session, "revision": revision, "code": code,
                "frequency": frequency, "canonical": canonical,
                "validated_at": observed.timestamp(),
                "requested_start": (end - get_lookback_timedelta(frequency)).timestamp(),
                "fingerprint": _fingerprint(snapshot),
            }
            storage.save_klines_parquet(_NAMESPACE, *key, snapshot)
        except (TypeError, ValueError, OSError) as exc:
            LogUtil.warning(f"[history_source] cache write skipped {code} {frequency}: {type(exc).__name__}")
        return _chart_frame(frame, observed.timestamp() + _MAX_AGE)
