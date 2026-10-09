"""Bounded, revalidated uSMART history shared by chart and screening processes.

Stable history is reused for at most 15 minutes. Every new tail must overlap
unchanged completed candles; revisions, session transitions and explicit force
requests rebuild the entire bounded provider window. This is a source cache,
not an assertion that older forward-adjusted prices can never be revised.
"""
from functools import lru_cache
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import threading
import time
from uuid import uuid4
import weakref

import numpy as np
import pandas as pd

from chanlun.exchange._lookback import get_lookback_timedelta
from chanlun.exchange.closed_history_cache import closed_us_session
from chanlun.persistence.file_lock import interprocess_file_lock, InterprocessLockTimeout
from chanlun.exchange.errors import USmartAPIError,USmartHistoryPending
from chanlun.exchange.lb_priority import quote_request_priority
from chanlun.tools.log_util import LogUtil

META = "_usmart_history_cache"
FETCH = "_usmart_history_fetch"
SCHEMA = "usmart-incremental-history-v1"
VALUES = ["open", "high", "low", "close", "volume"]
_registry_lock = threading.Lock()
_locks = weakref.WeakValueDictionary()


@lru_cache(maxsize=1)
def _revision():
    root = Path(__file__).parent
    digest = hashlib.sha256()
    for name in ("usmart_history_cache.py", "exchange_usmart.py", "price_basis.py", "kline_precision.py", "_lookback.py"):
        digest.update((root / name).read_bytes())
    return digest.hexdigest()


def _clean(frame):
    if frame is None:
        return None
    result = frame.copy(deep=True).reset_index(drop=True)
    result.attrs = {k:v for k,v in frame.attrs.items() if k not in {META, FETCH}}
    return result


def public_history_frame(frame):
    stats = {} if frame is None else frame.attrs.get(FETCH,{})
    if stats.get("invalid_rows") or stats.get("stalled"):
        raise USmartAPIError("盈立历史分页未完整返回，暂不使用本次行情",code="usmart_history_incomplete")
    return _clean(frame)


@contextmanager
def _flight(lock,path,timeout):
    started=time.monotonic()
    if not lock.acquire(timeout=timeout):
        raise USmartHistoryPending()
    try:
        remaining=timeout-(time.monotonic()-started)
        if remaining<=0:
            raise USmartHistoryPending()
        with interprocess_file_lock(path.with_suffix(".lock"),timeout_seconds=remaining):
            yield
    except InterprocessLockTimeout as exc:
        raise USmartHistoryPending() from exc
    finally:
        lock.release()


def _fingerprint(frame):
    clean = _clean(frame)
    digest = hashlib.sha256(pd.util.hash_pandas_object(clean, index=False).values.tobytes())
    digest.update(json.dumps(clean.attrs,sort_keys=True,separators=(",", ":"),allow_nan=False).encode())
    return digest.hexdigest()


def _metadata_digest(meta):
    return hashlib.sha256(json.dumps({k:v for k,v in meta.items() if k!='checksum'},
        sort_keys=True,separators=(",", ":"),allow_nan=False).encode()).hexdigest()


def _usable(frame):
    if frame is None or len(frame) < 3 or not set(["date", *VALUES]) <= set(frame):
        return False
    try:
        return (isinstance(frame.date.dtype,pd.DatetimeTZDtype)
                and not frame.date.isna().any() and frame.date.is_monotonic_increasing and frame.date.is_unique
                and np.isfinite(frame[VALUES].to_numpy(dtype=float)).all()
                and (frame[VALUES[:4]] > 0).all().all() and (frame.volume >= 0).all()
                and (frame.high >= frame[["open","close","low"]].max(axis=1)).all()
                and (frame.low <= frame[["open","close","high"]].min(axis=1)).all()
                and frame.attrs.get("price_basis_provider") == "usmart"
                and bool(frame.attrs.get("price_basis_revision"))
                and not frame.attrs.get(FETCH,{}).get("invalid_rows")
                and not frame.attrs.get(FETCH,{}).get("stalled")
                and not frame.attrs.get(FETCH,{}).get("duplicate_rows")
                and not frame.attrs.get("fetch_incomplete"))
    except (TypeError,ValueError,AttributeError):
        return False


def _epoch(observed):
    closed = closed_us_session(observed)
    local = observed.tz_convert("America/New_York")
    return "closed:" + closed if closed else "active:" + str(local.date())


class USmartHistoryCache:
    def __init__(self, root=None, *, clock=time.time, full_validation_seconds=900, coalesce_seconds=2):
        self.root = None if root is None else Path(root)
        self.clock = clock
        self.full_validation_seconds = full_validation_seconds
        self.coalesce_seconds = coalesce_seconds

    def _path(self, identity):
        from chanlun import config
        root = self.root if self.root is not None else config.get_data_path() / "cache" / "usmart_history"
        digest = hashlib.sha256(json.dumps(identity,sort_keys=True,separators=(",", ":")).encode()).hexdigest()
        return root / (digest + ".parquet")

    @staticmethod
    def _load(path):
        try:
            frame = pd.read_parquet(path)
            meta = frame.attrs.get(META)
            if (not isinstance(meta,dict) or meta.get("schema") != SCHEMA or meta.get("identity") != path.stem
                    or meta.get("checksum") != _metadata_digest(meta)
                    or meta.get("fingerprint") != _fingerprint(frame)):
                return None
            return frame if _usable(frame) else None
        except (OSError,ValueError,TypeError,KeyError):
            return None

    @staticmethod
    def _save(path, frame, meta):
        value = _clean(frame)
        value.attrs[META] = {**meta,"schema":SCHEMA,"identity":path.stem,"fingerprint":_fingerprint(value)}
        value.attrs[META]['checksum'] = _metadata_digest(value.attrs[META])
        temp = path.with_name("." + path.stem + "." + uuid4().hex + ".tmp")
        try:
            value.to_parquet(temp,index=False)
            os.replace(temp,path)
        finally:
            temp.unlink(missing_ok=True)

    def read(self, *, code, frequency, end, host, channel, right, count, pages, loader, force=False):
        """loader(start) reads one bounded window ending at the captured end."""
        observed = pd.Timestamp(self.clock(),unit="s",tz="UTC")
        end = pd.Timestamp(end)
        interval = pd.Timedelta(minutes=int(frequency[:-1]))
        closed = closed_us_session(observed)
        recent = abs((observed-end).total_seconds()) <= max(600,interval.total_seconds()*2)
        if not recent and closed:
            recent = (str(end.tz_convert("America/New_York").date()) == closed
                      and end.tz_convert("America/New_York").hour >= 16)
        if not recent or end > observed + interval:
            return public_history_frame(loader(None))
        identity = {"host":host,"channel":channel,"code":code,"frequency":frequency,
                    "right":right,"count":count,"pages":pages,"revision":_revision()}
        path = self._path(identity)
        # Spread periodic audits across symbols. Warming many charts together
        # must not cause another quota-sized burst exactly fifteen minutes later.
        validation_budget = self.full_validation_seconds * (.75 + .25 * int(path.stem[:8],16)/0xffffffff)
        with _registry_lock:
            lock = _locks.get(path)
            if lock is None:
                lock = threading.RLock()
                _locks[path] = lock
        started = time.monotonic()
        # One transaction owns fetch + publication so another process cannot
        # publish an earlier tail over newer history or start the same download.
        try:
            with _flight(lock,path,8 if quote_request_priority()=="interactive" else 60):
                now = float(self.clock())
                cached = self._load(path)
                meta = {} if cached is None else cached.attrs[META]
                floor = end.tz_convert("UTC").floor(f"{int(frequency[:-1])}min").timestamp()
                if cached is not None and floor < meta.get("end_floor",0):
                    # Even an older force query owns only its own window.
                    # Preserve the newer shared source on success and failure.
                    return public_history_frame(loader(None))
                if force:
                    # A failed forced validation must not resurrect the old
                    # source in another reader. Leave an explicit invalid frame.
                    self._save(path,pd.DataFrame(),{"invalidated":True})
                    cached,meta = None,{}
                try:
                    valid = (cached is not None and meta["epoch"] == _epoch(observed)
                             and 0 <= now-meta["validated_at"] < validation_budget
                             and 0 <= now-meta["fetched_at"]
                             and floor >= meta["end_floor"])
                    if valid and floor == meta["end_floor"] and now-meta["fetched_at"] < self.coalesce_seconds:
                        return _clean(cached.loc[(cached.date>=end-get_lookback_timedelta(frequency))
                                                 & (cached.date<=end)].reset_index(drop=True))
                except (KeyError,TypeError,ValueError):
                    valid = False
                mode, result = "full", None
                if valid and len(cached) >= 12:
                    overlap_start = cached.date.iloc[-12]
                    tail = loader(overlap_start.isoformat())
                    if _usable(tail) and list(tail.columns) == list(cached.columns):
                        same_basis = all(tail.attrs.get(k)==cached.attrs.get(k) for k in (
                            "price_basis_revision","structure_price_quantum","price_basis_provider","price_basis_adjustment"))
                        old = cached.loc[(cached.date>=tail.date.iloc[0]) & (cached.date<cached.date.iloc[-2])].set_index("date")
                        fresh = tail.set_index("date")
                        same_overlap = tail.date.iloc[0]<=overlap_start and len(old)>=3 and old.index.isin(fresh.index).all()
                        if same_overlap:
                            same_overlap = old[VALUES].equals(fresh.loc[old.index,VALUES])
                        if same_basis and same_overlap and tail.date.iloc[-1]>=cached.date.iloc[-1]:
                            keep = _clean(cached.loc[cached.date<tail.date.iloc[0]])
                            clean_tail = _clean(tail)
                            result = pd.concat([keep,clean_tail],ignore_index=True)
                            result.attrs = dict(clean_tail.attrs)
                            result = result.loc[(result.date>=end-get_lookback_timedelta(frequency)) & (result.date<=end)]
                            limit = (meta["base_limit"]-int(tail.attrs.get(FETCH,{}).get("future_rows",0))
                                     if meta.get("window_capped") else meta["row_limit"])
                            if meta.get("window_capped") and len(result)<limit:
                                result = None  # A newly usable head candle requires a cold read.
                            else:
                                result = result.tail(limit).reset_index(drop=True)
                                mode = "tail"
                if result is None:
                    result = loader(None)
                    stats = {} if result is None else result.attrs.get(FETCH,{})
                    base_limit = int(stats.get("page_rows",count))*pages
                    meta = {"validated_at":now,"base_limit":base_limit,
                            "window_capped":stats.get("window_capped",result is not None and len(result)>=base_limit),
                            "row_limit":max(0 if result is None else len(result),base_limit-int(stats.get("future_rows",0)))}
                if _usable(result):
                    publish = {**meta,"epoch":_epoch(observed),"end_floor":floor,"fetched_at":float(self.clock())}
                    # An earlier historical request may not roll back a newer
                    # shared source; its independent result remains usable.
                    old_end = cached.attrs[META].get("end_floor",0) if cached is not None else 0
                    if floor >= old_end:
                        try:
                            self._save(path,result,publish)
                        except (OSError,ValueError,TypeError) as exc:
                            LogUtil.warning(f"[usmart_history] cache write skipped {code} {frequency}: {type(exc).__name__}")
                LogUtil.info(f"[usmart_history] {code} {frequency} mode={mode} rows={0 if result is None else len(result)} "
                             f"elapsed_ms={(time.monotonic()-started)*1000:.0f}")
                return public_history_frame(result)
        except InterprocessLockTimeout as exc:
            raise USmartAPIError("盈立历史正在由另一请求更新，请稍后重试",retry_after=2) from exc
