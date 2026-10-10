"""Shared, bounded native-minute preparation for canonical US intraday bars.

Only uSMART type1 opening-labelled rows enter this store. Native type2/type5
bars and old frozen notification frames cannot be relabelled into this basis.
Each caller advances a bounded slice; incomplete history is explicit pending.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
from pathlib import Path
import time
from uuid import uuid4

import pandas as pd

from chanlun.exchange.errors import USmartAPIError, USmartHistoryPending
from chanlun.exchange.kline_precision import normalize_kline_precision, resolve_structure_price_quantum
from chanlun.exchange.price_basis import attach_price_basis_metadata, build_provider_price_basis_metadata
from chanlun.exchange.usmart_session_aggregation import aggregate_closed_us_minutes, expected_closed_window_start
from chanlun.exchange.usmart_rate_limit import USmartRateLimitTimeout
from chanlun.persistence.file_lock import interprocess_file_lock, InterprocessLockTimeout

SOURCE_TIME_CONTRACT = 'usmart-native-open-1m-to-closed-xnys-v1'
RAW_SCHEMA = 'usmart-shared-native-open-minutes-v1'
META = '_usmart_canonical_source'
VALUES = ['open', 'high', 'low', 'close', 'volume']
COLUMNS = ['date', 'frequency', 'code', *VALUES]
# Exact pre-migration raw cache contracts; these are numeric native1m caches,
# not signal snapshots. Only fresh, hash-verified files may seed preparation.
LEGACY_REVISIONS = ('e23574fc60bb6618bdac72b8383af620852760d7d0a8c361375652832888d9e7',
                    '419f65393b2c651d5d3b2b0772f25cdde7d5b406e204a404039359bc6a2e25ac')


class CanonicalHistoryPending(USmartHistoryPending):
    public_message = '正在补齐该标的已收盘分钟历史，请稍后重试。'

    def __init__(self, preparation):
        super().__init__()
        self.preparation = {key: preparation.get(key) for key in
            ('progress_token', 'available_rows', 'required_rows', 'retry_after')}
        self.retry_after = max(1, int(self.preparation.get('retry_after') or 3))

    def __reduce__(self):
        return type(self), (self.preparation,)


def _stamp(value):
    stamp = pd.Timestamp(value, unit='s', tz='UTC') if isinstance(value, (int, float)) else pd.Timestamp(value)
    if pd.isna(stamp) or stamp.tzinfo is None:
        raise ValueError('canonical cutoff must be timezone-aware')
    return stamp.tz_convert('UTC')


def _fingerprint(frame):
    return hashlib.sha256(pd.util.hash_pandas_object(frame[COLUMNS], index=False).values.tobytes()).hexdigest()


def _same_values(left, right):
    # Parquet/legacy minutes can store integer volume as float. Compare exact
    # values, not pandas dtypes; no price tolerance or rounding is permitted.
    return bool((left.eq(right) | (left.isna() & right.isna())).all().all())


def _empty():
    frame = pd.DataFrame(columns=COLUMNS)
    frame['date'] = pd.to_datetime(frame.date, utc=True)
    for column in VALUES:
        frame[column] = frame[column].astype(float)
    frame.attrs['bar_time_label'] = 'start'
    return frame


def _raw_valid(frame, code):
    return (isinstance(frame, pd.DataFrame) and set(COLUMNS) <= set(frame)
        and isinstance(frame.date.dtype, pd.DatetimeTZDtype) and frame.date.notna().all()
        and frame.date.is_unique and frame.date.is_monotonic_increasing
        and frame.code.eq(code).all() and frame.frequency.eq('1m').all()
        and (frame.date.astype('datetime64[ns, UTC]').astype('int64') % 60_000_000_000 == 0).all())


def canonical_metadata(frame, code, right):
    metadata = build_provider_price_basis_metadata(provider='usmart', market='us', code=code,
        adjustment={0: 'none', 1: 'forward', 2: 'backward'}[right],
        structure_price_quantum=resolve_structure_price_quantum('us', code),
        normalization_revision=SOURCE_TIME_CONTRACT)
    result = attach_price_basis_metadata(frame, metadata)
    result.attrs.update(bar_time_label='end', source_time_contract=SOURCE_TIME_CONTRACT)
    return result


class CanonicalUSmartHistory:
    def __init__(self, client, *, root=None, loader=None, legacy_cache=None,
                 clock=time.time, monotonic=time.monotonic):
        self.client, self.root, self.loader = client, None if root is None else Path(root), loader
        self.legacy_cache, self.clock, self.monotonic = legacy_cache, clock, monotonic

    def _path(self, code, right):
        from chanlun import config
        identity = {'host': self.client.quote_host, 'channel': self.client.channel,
                    'code': code, 'right': right, 'schema': RAW_SCHEMA}
        key = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()
        root = self.root if self.root is not None else config.get_data_path() / 'cache' / 'usmart_canonical_minutes'
        return root / (key + '.parquet')

    def _load(self, path, code):
        try:
            frame = pd.read_parquet(path)
            meta = frame.attrs.get(META, {})
            if (meta.get('schema') != RAW_SCHEMA or meta.get('identity') != path.stem
                    or not _raw_valid(frame, code) or meta.get('fingerprint') != _fingerprint(frame)):
                return _empty(), {}
            frame['date'] = pd.to_datetime(frame.date, utc=True)
            frame.attrs = {'bar_time_label': 'start'}
            return frame, meta
        except (OSError, ValueError, TypeError, KeyError):
            return _empty(), {}

    def _save(self, path, frame, meta):
        path.parent.mkdir(parents=True, exist_ok=True)
        value = frame.copy(deep=True)
        value.attrs = {'bar_time_label': 'start', META: {**meta, 'schema': RAW_SCHEMA,
                       'identity': path.stem, 'fingerprint': _fingerprint(frame)}}
        temporary = path.with_name(path.stem + '.' + uuid4().hex + '.tmp')
        try:
            value.to_parquet(temporary, index=False)
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)

    def _seed(self, code, right, count, pages):
        if self.legacy_cache is None:
            return _empty()
        from chanlun.exchange.usmart_history_cache import META as OLD_META
        for revision in LEGACY_REVISIONS:
            path = self.legacy_cache._path({'host': self.client.quote_host, 'channel': self.client.channel,
                'code': code, 'frequency': '1m', 'right': right, 'count': count, 'pages': pages,
                'revision': revision})
            frame = self.legacy_cache._load(path)
            if frame is None or not _raw_valid(frame, code):
                continue
            if not 0 <= self.clock() - frame.attrs.get(OLD_META, {}).get('validated_at', 0) < 900:
                continue
            if (frame.attrs.get('price_basis_provider') != 'usmart'
                    or frame.attrs.get('price_basis_adjustment') != {0:'none',1:'forward',2:'backward'}[right]):
                continue
            frame = frame[COLUMNS].copy()
            frame['date'] = pd.to_datetime(frame.date, utc=True)
            frame.attrs = {'bar_time_label': 'start'}
            return frame
        return _empty()

    def _page(self, code, cursor, right, count, deadline):
        if self.loader is not None:
            frame = self.loader(code=code, cursor=cursor, right=right, count=count, deadline=deadline)
        else:
            from chanlun.exchange.exchange_usmart import _parse_api_time, _timestamp_to_api_time
            import pytz
            zone = pytz.timezone('US/Eastern')
            data = self.client.quote('kline', {'secuId': 'us' + code.removesuffix('.US'), 'type': 1,
                'start': _timestamp_to_api_time(cursor.tz_convert(zone)), 'right': right, 'count': count},
                transient_retries=0, deadline=deadline)
            if (not isinstance(data, dict) or 'list' not in data
                    or (data['list'] is not None and not isinstance(data['list'], list))
                    or data.get('market') != 'us' or data.get('symbol') != code.removesuffix('.US')
                    or data.get('type') != 1 or data.get('right') != right):
                raise USmartAPIError('canonical minute response identity invalid', code='usmart_minute_contract_error')
            records = []
            for item in data['list'] or []:
                if not isinstance(item, dict):
                    raise USmartAPIError('canonical minute row invalid', code='usmart_minute_contract_error')
                stamp = _parse_api_time(item.get('latestTime'), zone)
                if stamp is None:
                    raise USmartAPIError('canonical minute timestamp invalid', code='usmart_minute_contract_error')
                records.append({'date':stamp,'frequency':'1m','code':code,
                                **{key:item.get(key) for key in VALUES}})
            frame = pd.DataFrame(records, columns=COLUMNS) if records else _empty()
            if not frame.empty:
                frame['date'] = pd.to_datetime(frame.date, utc=True)
                for key in VALUES:
                    frame[key] = pd.to_numeric(frame[key], errors='coerce')
                frame = frame.sort_values('date').reset_index(drop=True)
                frame = normalize_kline_precision(frame, 'us', code)
        frame = frame.copy(deep=True)
        if 'date' in frame:
            frame['date'] = pd.to_datetime(frame.date, utc=True)
        if not _raw_valid(frame, code) or (not frame.empty and frame.date.max() > cursor):
            raise USmartAPIError('canonical minute page clock invalid', code='usmart_minute_contract_error')
        frame.attrs = {'bar_time_label':'start'}
        return frame

    @staticmethod
    def _merge(raw, page, meta, *, head):
        if raw.empty:
            merged, frontier = page, page.date.min()
        else:
            old = raw.set_index('date')
            fresh = page.set_index('date')
            common = old.index.intersection(fresh.index)
            stable = common[common < raw.date.max() - pd.Timedelta(minutes=1)] if head else common
            covered = old.index[(old.index >= page.date.min()) & (old.index <= page.date.max())]
            conflict = (not covered.isin(fresh.index).all()
                        or (len(stable) and not _same_values(old.loc[stable, VALUES], fresh.loc[stable, VALUES])))
            previous_frontier = _stamp(meta.get('frontier', raw.date.min().timestamp()))
            if conflict:
                raw = _empty() if head else raw.loc[raw.date >= previous_frontier].copy()
                meta.pop('retained_frontier', None)
                meta['origin'] = False
            elif head and not len(common) and page.date.min() > raw.date.max():
                meta['retained_frontier'] = previous_frontier.timestamp()
                meta['retained_origin'] = bool(meta.get('origin'))
                meta['origin'] = False
            merged = pd.concat([raw, page], ignore_index=True).drop_duplicates('date', keep='last').sort_values('date').reset_index(drop=True)
            frontier = page.date.min()
            if not conflict and len(stable) >= 3:
                frontier = min(previous_frontier, page.date.min())
                if meta.get('retained_frontier') is not None:
                    frontier = min(frontier, _stamp(meta.pop('retained_frontier')))
                    meta['origin'] = meta.pop('retained_origin', False)
            elif not head:
                frontier = min(previous_frontier, page.date.min())
        merged.attrs = {'bar_time_label':'start'}
        meta['frontier'] = frontier.timestamp()
        meta['cursor'] = (frontier - pd.Timedelta(milliseconds=1)).isoformat()
        return merged

    @staticmethod
    def _faults(coverage):
        diagnostic = coverage['diagnostics']
        return set(diagnostic['missing_minute_opens']) | set(diagnostic['invalid_minute_opens'])

    def _recovery_due(self, meta, faults, attempted=(), *, force=False):
        attempts = meta.get('recovery_attempts', {})
        return sorted((stamp for stamp in faults if stamp not in attempted and
            (force or attempts.get(str(stamp), {}).get('retry_at', 0) <= self.clock())), reverse=True)

    def _record_recovery(self, meta, faults, observed, resolved=()):
        attempts = meta.setdefault('recovery_attempts', {})
        for stamp in resolved:
            attempts.pop(str(stamp), None)
        for stamp in faults & set(observed):
            failures = min(5, attempts.get(str(stamp), {}).get('failures', 0) + 1)
            attempts[str(stamp)] = {'failures': failures,
                'retry_at': self.clock() + min(900, 60 * 2 ** (failures - 1))}

    @staticmethod
    def _merge_recovery(raw, page, meta, invalid):
        """Replace a provider page only while its valid overlap has the same basis.

        A change to any normal neighbour, or insufficient overlap, invalidates
        the old cache. Rebuild from the head instead of joining revised history
        to unverified prices elsewhere. Known invalid values are never repaired
        locally: the entire replacement page must come from the provider.
        """
        old, fresh = raw.set_index('date'), page.set_index('date')
        common = old.index.intersection(fresh.index)
        invalid = pd.to_datetime(sorted(invalid), unit='s', utc=True)
        stable = common.difference(invalid)
        covered = old.index[(old.index >= page.date.min()) & (old.index <= page.date.max())]
        conflict = (len(stable) < 3 or not covered.isin(fresh.index).all()
                    or not _same_values(old.loc[stable, VALUES], fresh.loc[stable, VALUES]))
        if conflict:
            meta.clear()
            meta['rebuilding'] = True
            return _empty(), True
        # Do not advance the history frontier or infer an origin from a repair.
        merged = pd.concat([raw, page], ignore_index=True).drop_duplicates('date', keep='last').sort_values('date').reset_index(drop=True)
        merged.attrs = {'bar_time_label': 'start'}
        return merged, False

    def _view(self, raw, meta, code, frequency, cutoff, right, target_from, all_origin, target_rows):
        visible = raw
        if not raw.empty and meta.get('frontier') is not None:
            visible = raw.loc[raw.date >= _stamp(meta['frontier'])].copy()
        visible = visible.loc[visible.date + pd.Timedelta(minutes=1) <= cutoff].copy()
        visible.attrs = {'bar_time_label':'start'}
        beginning = target_from
        if target_rows is not None and not all_origin:
            expected_start = expected_closed_window_start(frequency, cutoff, target_rows)
            beginning = expected_start if beginning is None else max(beginning, expected_start)
        # Keep the predecessor: otherwise a missing first expected minute can
        # be mistaken for the source's paginated left boundary.
        frame, diagnostic = aggregate_closed_us_minutes(visible, frequency, cutoff, start=beginning)
        frame = canonical_metadata(frame, code, right)
        invalid = any(diagnostic.get(key, 0) for key in ('missing_minute_count','duplicate_minute_count','invalid_minute_count'))
        reached = bool(target_from is not None and not visible.empty
                       and visible.date.min() <= target_from - pd.Timedelta(minutes=int(frequency[:-1]))
                       and not diagnostic.get('leading_partial_bucket'))
        enough = bool(target_rows is not None and len(frame) >= target_rows)
        complete = bool(len(frame) and not invalid and not diagnostic.get('stale_source')
            and not diagnostic.get('trailing_partial_bucket')
            and (bool(meta.get('origin')) if all_origin else (reached or enough or bool(meta.get('origin')))))
        if target_rows is not None and not all_origin:
            frame = frame.tail(target_rows).reset_index(drop=True)
        progress = _fingerprint(visible)
        empty_origin = frame.empty and bool(meta.get('origin'))
        coverage = {'source_time_contract':SOURCE_TIME_CONTRACT, 'source_origin':bool(meta.get('origin')),
            'target_reached':reached or enough, 'can_resume':not complete and not invalid and not empty_origin,
            'complete':complete, 'reason':'ready' if complete else 'minute_contract_error' if invalid else 'source_empty' if empty_origin else 'preparing',
            'progress_token':progress,'available_rows':len(frame),'required_rows':target_rows,'retry_after':3,
            'raw_minute_rows':len(visible),'requested_from':None if target_from is None else int(target_from.timestamp()),
            'requested_to':int(cutoff.timestamp()),'available_from':None if frame.empty else int(frame.date.min().timestamp()),
            'available_to':None if frame.empty else int(frame.date.max().timestamp()),
            'diagnostics':{key:diagnostic.get(key) for key in ('missing_minute_count','duplicate_minute_count',
                'invalid_minute_count','leading_partial_bucket','trailing_partial_bucket','stale_source','expected_cutoff',
                'missing_minute_opens','invalid_minute_opens')}}
        faults = self._faults(coverage)
        if faults:
            attempts = meta.get('recovery_attempts', {})
            retry_at = min(attempts.get(str(stamp), {}).get('retry_at', 0) for stamp in faults)
            coverage['retry_after'] = max(3, math.ceil(retry_at - self.clock()))
        frame.attrs['canonical_source_progress'] = progress
        frame.attrs['canonical_history_coverage'] = coverage
        return frame, coverage

    def window(self, code, frequency, *, cutoff, right=1, target_from=None, all_origin=False,
               target_rows=None, max_pages=4, timeout_seconds=15., fetch=True, count=1000, legacy_pages=10,
               force=False):
        if frequency not in {'1m','5m','30m'} or right not in (0,1,2):
            raise ValueError('unsupported canonical minute request')
        if (type(max_pages) is not int or not 0 <= max_pages <= 16 or not math.isfinite(timeout_seconds)
                or not 0 < timeout_seconds <= 120 or type(count) is not int or not 1 <= count <= 1000):
            raise ValueError('invalid canonical preparation budget')
        cutoff = _stamp(cutoff)
        target_from = None if target_from is None else _stamp(target_from)
        if target_from is not None and target_from > cutoff:
            raise ValueError('canonical start follows cutoff')
        path = self._path(code, right)
        raw, meta = self._load(path, code)
        if not fetch:
            return self._view(raw, meta, code, frequency, cutoff, right, target_from, all_origin, target_rows)
        deadline = self.monotonic() + timeout_seconds
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with interprocess_file_lock(path.with_suffix('.lock'), timeout_seconds=.05):
                raw, meta = self._load(path, code)
                if raw.empty and not meta.get('rebuilding'):
                    raw = self._seed(code, right, count, legacy_pages)
                    if not raw.empty:
                        meta = {'frontier':raw.date.min().timestamp(), 'cursor':(raw.date.min()-pd.Timedelta(milliseconds=1)).isoformat()}
                from chanlun.screening.markets import closed_bar_cutoff
                required_close = pd.Timestamp(closed_bar_cutoff('us', code, cutoff.to_pydatetime(), '1m'), unit='s', tz='UTC')
                if not force and raw.empty and meta.get('origin') and meta.get('tail_checked',0) >= required_close.timestamp():
                    return self._view(raw, meta, code, frequency, cutoff, right, target_from, all_origin, target_rows)
                head = (force or raw.empty or meta.get('tail_checked',0) < required_close.timestamp()
                        or self.clock()-meta.get('tail_checked_at',0) >= 900)
                admission_timeout = None
                attempted = set()
                for _ in range(max_pages):
                    if self.monotonic() >= deadline:
                        break
                    view, coverage = self._view(raw, meta, code, frequency, cutoff, right, target_from, all_origin, target_rows)
                    faults = self._faults(coverage)
                    due = self._recovery_due(meta, faults, attempted, force=force)
                    if not head and (coverage['complete'] or (not coverage['can_resume'] and not due)):
                        return view, coverage
                    recovery = not head and bool(due)
                    target = due[0] if recovery else None
                    cursor = (min(required_close - pd.Timedelta(minutes=1), _stamp(target) + pd.Timedelta(minutes=3))
                              if recovery else required_close - pd.Timedelta(minutes=1) if head else pd.Timestamp(meta['cursor']))
                    try:
                        page = self._page(code, cursor, right, count, deadline)
                    except USmartRateLimitTimeout as exc:
                        # Local admission exhausted this slice, not the data
                        # source. Previously saved minutes remain resumable.
                        admission_timeout = exc
                        break
                    except TimeoutError:
                        if recovery:
                            self._record_recovery(meta, faults, [target])
                            attempted.add(target)
                            self._save(path, raw, meta)
                        break
                    except USmartAPIError:
                        if recovery:
                            self._record_recovery(meta, faults, [target])
                            self._save(path, raw, meta)
                        raise
                    if page.empty:
                        if recovery:
                            # An empty interior page is not the source origin.
                            self._record_recovery(meta, faults, [target])
                            attempted.add(target)
                            self._save(path, raw, meta)
                            continue
                        if not head or raw.empty:
                            meta['origin'] = True
                            meta['origin_before'] = cursor.isoformat()
                        meta.update(tail_checked=required_close.timestamp(), tail_checked_at=self.clock())
                        self._save(path, raw, meta)
                        head = False
                        break
                    if recovery:
                        raw, rebuild = self._merge_recovery(raw, page, meta, coverage['diagnostics']['invalid_minute_opens'])
                        if rebuild:
                            self._save(path, raw, meta)
                            head = True
                            continue
                    else:
                        raw = self._merge(raw, page, meta, head=head)
                    if head:
                        meta.update(tail_checked=required_close.timestamp(), tail_checked_at=self.clock())
                        meta.pop('rebuilding', None)
                    meta['page_rows'] = max(len(page), meta.get('page_rows',0))
                    _, updated = self._view(raw, meta, code, frequency, cutoff, right, target_from, all_origin, target_rows)
                    remaining = self._faults(updated)
                    observed = {stamp for stamp in remaining if page.date.min().timestamp() <= stamp <= cursor.timestamp()}
                    if recovery:
                        observed.add(target)
                    attempted.update(observed)
                    self._record_recovery(meta, remaining, observed, faults - remaining)
                    self._save(path, raw, meta)
                    head = False
                frame, coverage = self._view(raw, meta, code, frequency, cutoff, right, target_from, all_origin, target_rows)
                if self._recovery_due(meta, self._faults(coverage), attempted, force=force):
                    # More interior pages can advance in another bounded slice.
                    coverage.update(complete=False, can_resume=True, reason='preparing')
                if head or admission_timeout is not None:
                    coverage.update(complete=False, can_resume=True, reason='deadline')
                if admission_timeout is not None:
                    coverage['retry_after'] = max(coverage['retry_after'], admission_timeout.retry_after)
                return frame, coverage
        except InterprocessLockTimeout:
            raw, meta = self._load(path, code)
            frame, coverage = self._view(raw, meta, code, frequency, cutoff, right, target_from, all_origin, target_rows)
            coverage['lock_wait'] = True
            if force or self._faults(coverage):
                # Another writer may be verifying the bad page. Lock admission
                # is preparation work, not another failed source observation.
                coverage.update(complete=False, can_resume=True, reason='preparing')
            return frame, coverage
