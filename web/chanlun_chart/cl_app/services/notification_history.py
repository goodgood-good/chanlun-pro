"""Bounded, resumable notification background history; never signal evidence.

This module never changes the event's frozen frame. Production uSMART history
uses the shared canonical minute source; returned candles still require the
consumer's session and strict OHLCV validation. The raw-page parser is retained
only for explicit source replay and is not the production history path.
"""
from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
import gzip
import hashlib
import io
import json
import math
import os
from pathlib import Path
import re
import threading
import time
from types import SimpleNamespace
from uuid import uuid4

import pandas as pd


TARGET_DAYS = {"1m": 30, "5m": 90, "30m": None}
VALUES = ["open", "high", "low", "close", "volume"]
BASIS = ("structure_price_quantum", "price_basis_revision",
         "price_basis_provider", "price_basis_adjustment")
SCHEMA = "notification-background-history-v1"


class HistoryContractError(ValueError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class HistoryResult:
    frame: pd.DataFrame
    coverage: dict
    state: dict


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                    allow_nan=False).encode()).hexdigest()


def _reader_revision():
    from chanlun.exchange import exchange_usmart
    root = Path(exchange_usmart.__file__).parent
    digest = hashlib.sha256(Path(__file__).read_bytes())
    for name in ("exchange_usmart.py", "price_basis.py", "kline_precision.py",
                 "usmart_session_aggregation.py", "usmart_canonical_history.py"):
        if (root / name).is_file():
            digest.update((root / name).read_bytes())
    return digest.hexdigest()


def _frame_digest(frame):
    digest = hashlib.sha256(pd.util.hash_pandas_object(frame, index=False).values.tobytes())
    digest.update(_digest({key: frame.attrs.get(key) for key in (*BASIS, "bar_time_label")}).encode())
    return digest.hexdigest()


def _basis(frame, primary_attrs):
    for key in BASIS:
        if not primary_attrs.get(key) or frame.attrs.get(key) != primary_attrs[key]:
            raise HistoryContractError("price_basis_conflict")


def _frame_contract(frame, code, frequency, cutoff):
    if (not isinstance(frame, pd.DataFrame) or frame.empty
            or not set(["date", *VALUES]) <= set(frame)
            or not isinstance(frame.date.dtype, pd.DatetimeTZDtype)
            or frame.date.isna().any() or not frame.date.is_unique
            or not frame.date.is_monotonic_increasing
            or frame.date.max() > cutoff):
        raise HistoryContractError("invalid_frame_clock")
    for key, expected in (("code", code), ("frequency", frequency)):
        if key in frame and not frame[key].eq(expected).all():
            raise HistoryContractError("frame_identity_conflict")
    # Bad OHLC geometry is deliberately not repaired or dropped here. The
    # renderer/structure consumer must reject it with its existing validator.
    if any(not pd.api.types.is_numeric_dtype(frame[key]) for key in VALUES):
        raise HistoryContractError("invalid_frame_values")


def _overlap(frame, anchor):
    expected = anchor.loc[anchor.date >= frame.date.min()]
    expected = expected.loc[expected.date <= frame.date.max()]
    actual = frame.set_index("date").reindex(expected.date)
    if not expected.empty:
        left = expected.set_index("date")[VALUES].astype(float)
        right = actual[VALUES].astype(float)
        # Exact normalized values, including volume, are evidence. Equal NaN
        # remains invalid for consumers but does not authorize price repair.
        if not expected.date.isin(frame.date).all():
            raise HistoryContractError("anchor_rows_missing")
        if not ((left == right) | (left.isna() & right.isna())).all().all():
            raise HistoryContractError("anchor_values_changed")
    return len(expected)


class USmartPageSource:
    """A dedicated background client with a bounded gate and no page retry.

    Creating this object is offline. Credentials/session are reused across pages
    and slices. A quote can replay once after an explicit rejected credential;
    login and replay share the same deadline. Ordinary network retries are off.
    """
    def __init__(self, *, monotonic=time.monotonic):
        from chanlun import config
        self.monotonic = monotonic
        self.identity = _digest({"provider": "usmart", "market": "us",
                                "host": config.USMART_QUOTE_HOST,
                                "channel": str(config.USMART_CHANNEL)})
        self._lock = threading.Lock()
        self._client = None
        self._deadline = 0.

    def __call__(self, *, code, frequency, cursor, right, remaining):
        from chanlun.exchange.exchange_usmart import _timestamp_to_api_time
        from chanlun.exchange.lb_priority import lb_low_priority
        deadline = self.monotonic() + remaining
        if not self._lock.acquire(timeout=max(0., remaining)):
            raise TimeoutError("notification history client busy")
        try:
            self._deadline = deadline
            if self._client is None:
                self._client = self._make_client()
            with lb_low_priority():
                return self._client.quote("kline", {
                    "secuId": "us" + code.removesuffix(".US"),
                    "type": {"1m": 1, "5m": 2, "30m": 5}[frequency],
                    "start": _timestamp_to_api_time(cursor.tz_convert("US/Eastern")),
                    "right": right, "count": 300,
                }, transient_retries=0)
        finally:
            self._lock.release()

    def _make_client(self):
        import requests
        from chanlun.exchange.exchange_usmart import USmartClient
        from chanlun.exchange.usmart_rate_limit import USmartRateLimiter
        owner, limiter = self, USmartRateLimiter()
        session = requests.Session()
        session.trust_env = os.environ.get("CHANLUN_USMART_TRUST_ENV_PROXY", "1").strip().lower() in {"1","true","yes","on"}

        class DeadlineSession:
            def post(self, *args, **kwargs):
                budget = owner._deadline - owner.monotonic()
                if budget <= 0:
                    raise TimeoutError("notification history deadline")
                kwargs['timeout'] = max(.001, min(8., budget / 2))
                return session.post(*args, **kwargs)

            def close(self):
                session.close()

        class DeadlineGate:
            def wait(self, **kwargs):
                budget = owner._deadline - owner.monotonic()
                if budget <= 0:
                    raise TimeoutError("notification history deadline")
                return limiter.wait(**kwargs, timeout=min(10., budget / 2))

        return USmartClient(session=DeadlineSession(), rate_limiter=DeadlineGate())

    def close(self):
        with self._lock:
            if self._client is not None:
                self._client.session.close()
                self._client = None


@lru_cache(maxsize=4)
def _cached_page_source(public_identity, credential_revision):
    # Neither key nor credentials are persisted or exposed in chart metadata.
    return USmartPageSource()


def _default_page_source():
    from chanlun import config
    public = _digest([config.USMART_QUOTE_HOST, str(config.USMART_CHANNEL)])
    credentials = _digest([getattr(config, 'USMART_'+name, '')
                           for name in ('TOKEN','PRIVATE_KEY','PUBLIC_KEY','PHONE','LOGIN_PASSWORD')])
    return _cached_page_source(public, credentials)


def _decode_page(data, *, code, frequency, cursor, right):
    from chanlun.exchange.exchange_usmart import ExchangeUSmart, _parse_api_time
    if (not isinstance(data, dict)
            or data.get("market") != "us"
            or data.get("symbol") != code.removesuffix(".US")
            or data.get("type") != {"1m": 1, "5m": 2, "30m": 5}[frequency]
            or data.get("right") != right or "list" not in data):
        raise HistoryContractError("response_identity_conflict")
    page = data["list"]
    if page is None or page == []:
        return None
    if not isinstance(page, list):
        raise HistoryContractError("invalid_page")
    exchange = ExchangeUSmart("us", client=SimpleNamespace(quote=lambda *_: data))
    stamps = [_parse_api_time(row.get("latestTime"), exchange.tz)
              if isinstance(row, dict) else None for row in page]
    if any(stamp is None for stamp in stamps):
        raise HistoryContractError("invalid_page_clock")
    if any(stamp > cursor for stamp in stamps):
        raise HistoryContractError("page_ignored_cursor")
    frame = exchange._fetch_native_klines(code, frequency, end_date=cursor,
        args={"right": right, "count": 300, "pages": 1, "_history_keep_overlap": True})
    stats = frame.attrs.pop("_usmart_history_fetch", {})
    if (stats.get("invalid_rows") or stats.get("duplicate_rows") or stats.get("stalled")
            or stats.get("future_rows") or len(frame) != len(page)):
        raise HistoryContractError("incomplete_page")
    frame.attrs["bar_time_label"] = "end"
    return frame


def _canonical_history(event, frequency, primary_attrs, *, anchor, cutoff,
                       target, previous, max_pages, timeout_seconds, fetch):
    """Use the same verified minute source as charts/monitoring at a frozen clock."""
    from chanlun.exchange import get_exchange
    from chanlun.market import Market
    from chanlun.exchange.errors import USmartHistoryPending
    from chanlun.exchange.lb_priority import lb_low_priority
    exchange = get_exchange(Market.US)
    right = {'none':0, 'forward':1, 'backward':2}.get(primary_attrs.get('price_basis_adjustment'))
    if right is None:
        raise HistoryContractError('unsupported_adjustment')
    identity = _digest({'event':event.get('id'), 'code':event['code'], 'frequency':frequency,
        'cutoff':int(cutoff.timestamp()), 'anchor':_frame_digest(anchor), 'reader':_reader_revision()})
    if previous is not None:
        state = previous.state
        if (state.get('identity') != identity
                or state.get('checksum') != _digest({k:v for k,v in state.items() if k != 'checksum'})
                or state.get('frame_hash') != _frame_digest(previous.frame)):
            raise HistoryContractError('resume_identity_conflict')
    frame = anchor.iloc[:0].copy()
    coverage = {'purpose':'independent_background', 'consumer_validation_required':True,
        'target_days':TARGET_DAYS[frequency], 'requested_from':int(target.timestamp()) if target is not None else None,
        'requested_to':int(cutoff.timestamp()), 'reason':'page_budget', 'error_code':None,
        'source_origin':False, 'target_reached':False, 'can_resume':True,
        'compatible_with_anchor':False, 'anchor_rows':len(anchor), 'anchor_overlap_rows':0,
        'anchor_complete':False, 'price_basis_overlap_verified':False, 'next_retry':3}
    try:
        with lb_low_priority():
            frame, source = exchange.notification_history_window(event['code'], frequency,
                cutoff=cutoff, target_from=target, all_origin=target is None,
                max_pages=max_pages, timeout_seconds=timeout_seconds, fetch=fetch, args={'right':right})
        coverage.update({key:source[key] for key in ('source_origin','target_reached','can_resume',
                            'progress_token','next_retry','pages') if key in source})
        if source.get('reason') in {'minute_contract_error', 'source_empty'}:
            raise HistoryContractError(source['reason'])
        if not frame.empty:
            _frame_contract(frame, event['code'], frequency, cutoff)
            _basis(frame, primary_attrs)
            overlap = _overlap(frame, anchor)
            if not overlap or frame.date.max() != cutoff:
                raise HistoryContractError('page_missing_cutoff')
            coverage.update(anchor_overlap_rows=overlap, anchor_complete=overlap==len(anchor),
                            price_basis_overlap_verified=True, compatible_with_anchor=True)
        if source.get('reason') in {'source_error','deadline'}:
            coverage['reason'] = source['reason']
        elif source.get('complete') is True and (coverage['source_origin'] or coverage['target_reached']):
            if not coverage['anchor_complete']:
                raise HistoryContractError('origin_before_anchor_covered')
            coverage.update(reason='target_reached' if coverage['target_reached'] else 'source_origin', can_resume=False)
    except USmartHistoryPending as exc:
        coverage['progress_token'] = getattr(exc, 'preparation', {}).get('progress_token')
    except HistoryContractError as exc:
        coverage.update(reason='contract_error', error_code=exc.code, compatible_with_anchor=False, can_resume=False)
    except Exception as exc:
        coverage.update(reason='source_error', error_code=type(exc).__name__)
        delay = getattr(exc, 'retry_after', None)
        if type(delay) in (int,float) and math.isfinite(delay):
            coverage['next_retry'] = max(3, min(3600, math.ceil(delay)))
    coverage.update(rows=len(frame), available_from=int(frame.date.min().timestamp()) if not frame.empty else None,
                    available_to=int(frame.date.max().timestamp()) if not frame.empty else None)
    state = {'schema':'notification-canonical-background-v1', 'identity':identity,
             'frame_hash':_frame_digest(frame), 'source_progress':coverage.get('progress_token'),
             'reason':coverage['reason'], 'source_origin':coverage['source_origin']}
    state['checksum'] = _digest(state)
    return HistoryResult(frame, coverage, state)


def prepare_notification_history(event, frequency, primary_attrs, *, anchor_frame,
                                 aligned_cutoff, page_source=None, previous=None,
                                 max_pages=4, timeout_seconds=20., monotonic=time.monotonic,
                                 fetch=True):
    """Return raw independent context plus honest coverage and resume state.

    ``aligned_cutoff`` must come from the verified period calendar and may not
    exceed the captured event clock. ``anchor_frame`` is same-period evidence
    ending exactly there. ``previous`` is an earlier HistoryResult for the same
    immutable inputs. A custom page_source has USmartPageSource's call signature
    and a stable, non-sensitive ``identity`` attribute. Caps/deadlines/errors do
    not prove provider origin. Caller schedules continuation; this does no I/O
    beyond bounded native pages and never modifies caller data or the event.
    """
    if frequency not in TARGET_DAYS:
        raise ValueError("unsupported notification frequency")
    if (type(max_pages) is not int or not 1 <= max_pages <= 64
            or not math.isfinite(timeout_seconds) or not 0 < timeout_seconds <= 120):
        raise ValueError("invalid notification history budget")
    cutoff = pd.Timestamp(aligned_cutoff, unit="s", tz="UTC")
    if cutoff.timestamp() > event["source_closed_at"]:
        raise HistoryContractError("cutoff_after_event")
    anchor = anchor_frame.copy(deep=True)
    _frame_contract(anchor, event["code"], frequency, cutoff)
    _basis(anchor, primary_attrs)
    if anchor.date.max() != cutoff:
        raise HistoryContractError("anchor_cutoff_mismatch")
    days = TARGET_DAYS[frequency]
    target = None if days is None else cutoff - pd.Timedelta(days=days)
    from chanlun import config
    supported = (event["market"] == "us" and primary_attrs.get("price_basis_provider") == "usmart"
                 and str(config.US_HISTORY_KLINE_SOURCE).lower() == "usmart"
                 and str(config.EXCHANGE_US).lower() == "usmart")
    # Frozen evidence already spanning a finite target needs no new requests.
    reusable = target is not None and anchor.date.min() <= target
    if page_source is None and supported and not reusable:
        return _canonical_history(event, frequency, primary_attrs, anchor=anchor, cutoff=cutoff,
            target=target, previous=previous, max_pages=max_pages, timeout_seconds=timeout_seconds, fetch=fetch)
    source = page_source
    if source is None and supported and not reusable:
        source = _default_page_source()
    identity = _digest({"event_id": event.get("id"), "market": event["market"],
        "code": event["code"], "frequency": frequency, "cutoff": aligned_cutoff,
        "event_cutoff": event["source_closed_at"], "anchor": _frame_digest(anchor),
        "source": getattr(source, "identity", None), "schema": SCHEMA, "reader_revision": _reader_revision()})
    frame = anchor.iloc[:0].copy()
    cursor = cutoff
    pages, checked, proofs = 0, 0, []
    reason, error_code, origin, usable, next_retry = "page_budget", None, False, True, 2
    started = monotonic()

    def result():
        oldest = None if frame.empty else int(frame.date.min().timestamp())
        newest = None if frame.empty else int(frame.date.max().timestamp())
        reached = bool(target is not None and oldest is not None and oldest <= target.timestamp())
        state = {"schema": SCHEMA, "identity": identity, "cursor": cursor.tz_convert("UTC").isoformat(),
                 "pages": pages, "overlap_rows": checked, "proofs": proofs,
                 "frame_hash": _frame_digest(frame), "reason": reason, "source_origin": origin,
                 "usable": usable, "error_code": error_code, "next_retry":next_retry}
        state["checksum"] = _digest(state)
        coverage = {"purpose": "independent_background", "consumer_validation_required": True,
            "price_basis_overlap_verified": checked > 0, "anchor_rows": len(anchor),
            "anchor_overlap_rows": checked, "anchor_complete": checked == len(anchor),
            "target_days": days, "requested_from": None if target is None else int(target.timestamp()),
            "requested_to": int(cutoff.timestamp()), "available_from": oldest, "available_to": newest,
            "rows": len(frame), "target_reached": reached, "source_origin": origin,
            "reason": reason, "error_code": error_code, "pages": pages, "next_retry":next_retry,
            "can_resume": reason in {"page_budget", "deadline", "source_error"},
            "compatible_with_anchor": usable and checked > 0, "elapsed_seconds": max(0., monotonic() - started)}
        return HistoryResult(frame.copy(deep=True), coverage, state)

    if reusable or not supported:
        frame, checked = anchor, len(anchor)
        reason = "frozen_target" if reusable else "unsupported_source"
        return result()
    if previous is not None:
        state = previous.state
        if (state.get("identity") != identity or state.get("schema") != SCHEMA
                or state.get("checksum") != _digest({k:v for k,v in state.items() if k != "checksum"})
                or state.get("frame_hash") != _frame_digest(previous.frame)):
            raise HistoryContractError("resume_identity_conflict")
        frame = previous.frame.copy(deep=True)
        cursor, pages = pd.Timestamp(state["cursor"]), state["pages"]
        checked, proofs = state["overlap_rows"], list(state["proofs"])
        reason, origin, usable = state["reason"], state["source_origin"], state["usable"]
        if reason not in {"page_budget", "deadline", "source_error"}:
            error_code = state["error_code"]
            next_retry = state.get('next_retry',2)
            return result()
    right = {"none": 0, "forward": 1, "backward": 2}.get(primary_attrs["price_basis_adjustment"])
    if right is None:
        raise HistoryContractError("unsupported_adjustment")
    reason = "page_budget"
    if not fetch:
        return result()
    for _ in range(max_pages):
        remaining = timeout_seconds - (monotonic() - started)
        if remaining <= 0:
            reason = "deadline"
            break
        try:
            data = source(code=event["code"], frequency=frequency, cursor=cursor,
                          right=right, remaining=remaining)
            page = _decode_page(data, code=event["code"], frequency=frequency,
                                cursor=cursor, right=right)
            proof = {"before": cursor.tz_convert("UTC").isoformat(), "response_hash": _digest(data),
                     "rows": 0 if page is None else len(page)}
            pages += 1
            if page is None:
                proofs.append(proof)
                origin, reason = True, "source_origin"
                if checked != len(anchor):
                    usable, error_code = False, "origin_before_anchor_covered"
                break
            _frame_contract(page, event["code"], frequency, cursor)
            _basis(page, primary_attrs)
            candidate = page if frame.empty else pd.concat([page, frame], ignore_index=True)
            candidate.attrs = dict(page.attrs)
            _frame_contract(candidate, event["code"], frequency, cutoff)
            overlap = _overlap(candidate, anchor)
            if not overlap:
                raise HistoryContractError("page_without_anchor_overlap")
            if candidate.date.max() != cutoff:
                raise HistoryContractError("page_missing_cutoff")
            frame, checked = candidate, overlap
            cursor = frame.date.min() - pd.Timedelta(milliseconds=1)
            proofs.append(proof)
            if target is not None and frame.date.min() <= target:
                reason = "target_reached"
                break
        except HistoryContractError as exc:
            reason, error_code, usable = "contract_error", exc.code, False
            break
        except Exception as exc:
            # No provider message/request body is copied into persisted state.
            reason = "deadline" if (isinstance(exc, TimeoutError) or monotonic() - started >= timeout_seconds
                or getattr(exc, 'code', None) == 'usmart_quote_rate_limit_timeout') else "source_error"
            error_code = type(exc).__name__
            delay = getattr(exc,'retry_after',None)
            if type(delay) in (int,float) and math.isfinite(delay):
                next_retry = max(2,min(3600,math.ceil(delay)))
            break
    return result()


class HistoryArchive:
    """Atomic per-event display preparation, separate from signal evidence.

    Callers hold ``lock_path`` while reading/updating. Object names are content
    hashes; the manifest is published last, then superseded objects are removed.
    A crash cannot pair an old cursor with a new candle frame.
    """
    schema = "notification-background-archive-v1"

    def __init__(self, root, scope):
        if not re.fullmatch(r"[a-f0-9]{64}", scope):
            raise ValueError("invalid notification history scope")
        self.root = Path(root) / scope
        self.scope = scope
        self.lock_path = self.root / "prepare.lock"

    def _read_object(self, ref):
        name = ref["name"]
        if not re.fullmatch(r"[a-f0-9]{64}\.(?:parquet|json\.gz)", name):
            raise HistoryContractError("invalid_history_object")
        data = (self.root / name).read_bytes()
        if hashlib.sha256(data).hexdigest() != name.split(".")[0]:
            raise HistoryContractError("history_object_changed")
        return data

    def _put(self, data, suffix):
        name = hashlib.sha256(data).hexdigest() + suffix
        path = self.root / name
        if not path.is_file():
            temporary = self.root / (uuid4().hex + ".tmp")
            try:
                temporary.write_bytes(data)
                os.replace(temporary, path)
            finally:
                temporary.unlink(missing_ok=True)
        elif path.read_bytes() != data:
            raise HistoryContractError("history_object_changed")
        return {"name": name}

    def read(self):
        path = self.root / "state.json"
        if not path.exists():
            return None, None, None, None
        try:
            manifest = json.loads(path.read_text(encoding="utf8"))
            if (manifest.get("schema") != self.schema or manifest.get("scope") != self.scope
                    or manifest.get("checksum") != _digest({k:v for k,v in manifest.items() if k != "checksum"})):
                raise HistoryContractError("history_manifest_changed")
            anchor = pd.read_parquet(io.BytesIO(self._read_object(manifest["anchor"])))
            result = None
            if manifest.get("result"):
                saved = manifest["result"]
                frame = pd.read_parquet(io.BytesIO(self._read_object(saved["frame"])))
                result = HistoryResult(frame, saved["coverage"], saved["state"])
            snapshot = None
            if manifest.get("snapshot"):
                snapshot = json.loads(gzip.decompress(self._read_object(manifest["snapshot"])))
            display_frame = None
            if manifest.get("display_frame"):
                display_frame = pd.read_parquet(io.BytesIO(self._read_object(manifest['display_frame'])))
            return anchor, result, snapshot, display_frame
        except HistoryContractError:
            raise
        except (OSError, ValueError, TypeError, KeyError) as exc:
            raise HistoryContractError("history_archive_unreadable") from exc

    def write(self, anchor, result=None, snapshot=None, display_frame=None):
        self.root.mkdir(parents=True, exist_ok=True)
        def store_frame(frame):
            buffer = io.BytesIO()
            frame.to_parquet(buffer, index=False)
            return self._put(buffer.getvalue(), ".parquet")
        manifest = {"schema": self.schema, "scope": self.scope, "anchor": store_frame(anchor),
                    "result": None, "snapshot": None, "display_frame":None, "updated_at": time.time()}
        if result is not None:
            manifest["result"] = {"frame": store_frame(result.frame),
                                  "coverage": result.coverage, "state": result.state}
        if snapshot is not None:
            encoded = json.dumps(snapshot, allow_nan=False, separators=(",", ":")).encode()
            manifest["snapshot"] = self._put(gzip.compress(encoded, mtime=0), ".json.gz")
        if display_frame is not None:
            manifest['display_frame'] = store_frame(display_frame)
        manifest["checksum"] = _digest(manifest)
        temporary = self.root / (uuid4().hex + ".tmp")
        try:
            temporary.write_text(json.dumps(manifest, allow_nan=False, separators=(",", ":")), encoding="utf8")
            os.replace(temporary, self.root / "state.json")
        finally:
            temporary.unlink(missing_ok=True)
        keep = {manifest["anchor"]["name"]}
        if manifest["result"]:
            keep.add(manifest["result"]["frame"]["name"])
        if manifest["snapshot"]:
            keep.add(manifest["snapshot"]["name"])
        if manifest['display_frame']:
            keep.add(manifest['display_frame']['name'])
        for path in self.root.iterdir():
            if (re.fullmatch(r"[a-f0-9]{64}\.(?:parquet|json\.gz)", path.name) and path.name not in keep
                    and not path.is_symlink() and path.resolve().parent == self.root.resolve()):
                path.unlink(missing_ok=True)

    @classmethod
    def cleanup(cls, root, *, now=None, lifetime=8*86400):
        """Prune expired display objects only; open job locks are never removed."""
        from chanlun.persistence.file_lock import interprocess_file_lock, InterprocessLockTimeout
        current = time.time() if now is None else now
        root = Path(root).resolve()
        if not root.is_dir():
            return
        for directory in root.iterdir():
            if (not directory.is_dir() or not re.fullmatch(r"[a-f0-9]{64}", directory.name)
                    or directory.is_symlink() or directory.resolve() != directory):
                continue
            manifest = directory / "state.json"
            try:
                lock = directory / 'prepare.lock'
                if (not manifest.exists() or manifest.is_symlink() or manifest.resolve().parent != directory
                        or lock.is_symlink() or lock.resolve().parent != directory
                        or current-manifest.stat().st_mtime <= lifetime):
                    continue
                with interprocess_file_lock(lock, timeout_seconds=.01):
                    if current-manifest.stat().st_mtime <= lifetime:
                        continue
                    for path in directory.iterdir():
                        if ((path.name == "state.json" or re.fullmatch(r"[a-f0-9]{64}\.(?:parquet|json\.gz)", path.name))
                                and not path.is_symlink() and path.resolve().parent == directory):
                            path.unlink(missing_ok=True)
            except (OSError, InterprocessLockTimeout):
                continue
