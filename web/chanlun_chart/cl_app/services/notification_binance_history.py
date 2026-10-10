"""Resumable Binance Spot background history for notification images only.

Official contract: /api/v3/klines returns UTC opening timestamps, a last
millisecond closing timestamp, and at most1000 rows. This reader keeps the
signal's normalized closing-time clock and never changes its frozen evidence.
https://developers.binance.com/docs/binance-spot-api-docs/rest-api/market-data-endpoints
https://developers.binance.com/docs/binance-spot-api-docs/faqs/market_data_only
"""
from __future__ import annotations

from functools import lru_cache
import hashlib
import math
from pathlib import Path
import re
import threading
import time

import pandas as pd

from .notification_history import (
    HistoryContractError, HistoryResult, SCHEMA, TARGET_DAYS,
    _basis, _digest, _frame_contract, _frame_digest, _overlap,
)

SOURCE_URL = 'https://data-api.binance.vision/api/v3/klines'
SOURCE_LABEL = 'Binance现货公开历史行情'


def _reader_revision():
    from chanlun.exchange import exchange_binance_common
    root = Path(exchange_binance_common.__file__).parent
    digest = hashlib.sha256(Path(__file__).read_bytes())
    for name in ('exchange_binance_common.py', 'price_basis.py', 'kline_precision.py'):
        digest.update((root / name).read_bytes())
    return digest.hexdigest()


class BinanceHistoryUnavailable(RuntimeError):
    def __init__(self, status_code=None, retry_after=None):
        self.status_code, self.retry_after = status_code, retry_after
        self.code = f'binance_http_{status_code}' if status_code is not None else 'binance_history_unavailable'
        super().__init__(self.code)


def _request_parameters(code, frequency, cursor):
    if not isinstance(code, str) or not re.fullmatch(r'[A-Z0-9]+/[A-Z0-9]+', code):
        raise HistoryContractError('invalid_spot_symbol')
    if frequency not in TARGET_DAYS or pd.Timestamp(cursor).tzinfo is None:
        raise HistoryContractError('invalid_spot_period_clock')
    step = int(frequency[:-1]) * 60000
    # endTime selects OPEN times. Closing timestamps are used by our state.
    return {'symbol': code.replace('/', ''), 'interval': frequency,
            'endTime': int(pd.Timestamp(cursor).value // 1_000_000) - step, 'limit': 1000}


class BinanceSpotPageSource:
    """A dedicated public-only session; one GET per page, with no HTTP retries."""
    identity = _digest({'provider': 'binance', 'market': 'currency_spot', 'url': SOURCE_URL})

    def __init__(self, *, session=None, monotonic=time.monotonic, sleep=time.sleep):
        self._session = session
        self.monotonic, self.sleep = monotonic, sleep
        self._lock, self._last_request, self._retry_at = threading.Lock(), 0., 0.

    def _get_session(self):
        if self._session is None:
            import requests
            from chanlun.utils import config_get_proxy
            session = requests.Session()
            session.trust_env = False  # Never load netrc/account credentials.
            proxy = config_get_proxy()
            if proxy.get('host'):
                address = f"http://{proxy['host']}:{proxy['port']}"
                session.proxies.update({'http': address, 'https': address})
            self._session = session
        return self._session

    def __call__(self, *, code, frequency, cursor, remaining, **_):
        parameters = _request_parameters(code, frequency, cursor)
        deadline = self.monotonic() + remaining
        if not self._lock.acquire(timeout=max(0., remaining)):
            raise TimeoutError('notification history client busy')
        try:
            now = self.monotonic()
            if now < self._retry_at:
                raise BinanceHistoryUnavailable(429, max(1, int(self._retry_at - now)))
            wait = max(0., self._last_request + .2 - now)
            if deadline - now <= wait:
                raise TimeoutError('notification history deadline')
            if wait:
                self.sleep(wait)
            budget = deadline - self.monotonic()
            if budget <= 0:
                raise TimeoutError('notification history deadline')
            self._last_request = self.monotonic()
            response = self._get_session().get(SOURCE_URL, params=parameters,
                         timeout=max(.001, min(8., budget / 2)), allow_redirects=False)
            if response.status_code != 200:
                delay = str(response.headers.get('Retry-After', ''))
                delay = min(3600, int(delay)) if delay.isascii() and delay.isdecimal() else 60
                if response.status_code in (418, 429):
                    self._retry_at = self.monotonic() + max(1, delay)
                raise BinanceHistoryUnavailable(response.status_code, delay)
            return response.json()
        finally:
            self._lock.release()

    def close(self):
        with self._lock:
            if self._session is not None:
                self._session.close()
                self._session = None


@lru_cache(maxsize=1)
def _default_page_source():
    return BinanceSpotPageSource()


def _decode_page(data, *, code, frequency, cursor):
    from chanlun.exchange.exchange_binance_common import normalize_binance_kline_frame
    if not isinstance(data, list) or len(data) > 1000:
        raise HistoryContractError('invalid_binance_page')
    if not data:
        return None
    parameters = _request_parameters(code, frequency, cursor)
    step = int(frequency[:-1]) * 60000
    rows = []
    for raw in data:
        if (not isinstance(raw, list) or len(raw) != 12
                or type(raw[0]) is not int or type(raw[6]) is not int
                or raw[0] < 0 or raw[0] % step or raw[6] != raw[0] + step - 1
                or raw[0] > parameters['endTime']):
            raise HistoryContractError('invalid_binance_bar_clock')
        try:
            if any(isinstance(value, bool) for value in raw[1:6]):
                raise ValueError('boolean OHLCV')
            values = [float(value) for value in raw[1:6]]
        except (ValueError, TypeError, OverflowError):
            raise HistoryContractError('invalid_binance_bar_values') from None
        op, high, low, close, volume = values
        if (not all(math.isfinite(v) for v in values) or low <= 0 or volume < 0
                or not low <= min(op, close) <= max(op, close) <= high):
            raise HistoryContractError('invalid_binance_bar_values')
        rows.append([raw[0], op, high, low, close, volume])
    frame = pd.DataFrame(rows, columns=['date', 'open', 'high', 'low', 'close', 'volume'])
    frame['date'] = pd.to_datetime(frame.date, unit='ms', utc=True)
    frame['code'], frame['frequency'] = code, frequency
    frame = normalize_binance_kline_frame(frame, market='currency_spot', code=code)
    frame['date'] = frame.date + pd.Timedelta(milliseconds=step)
    frame.attrs['bar_time_label'] = 'end'
    _frame_contract(frame, code, frequency, pd.Timestamp(cursor))
    return frame


def prepare_binance_notification_history(event, frequency, primary_attrs, *, anchor_frame,
                                        aligned_cutoff, page_source=None, previous=None,
                                        max_pages=4, timeout_seconds=20., monotonic=time.monotonic,
                                        fetch=True):
    """Return the shared HistoryResult contract; no signal recomputation or writes."""
    from chanlun import config
    if frequency not in TARGET_DAYS:
        raise ValueError('unsupported notification frequency')
    if (type(max_pages) is not int or not 1 <= max_pages <= 64
            or not math.isfinite(timeout_seconds) or not 0 < timeout_seconds <= 120):
        raise ValueError('invalid notification history budget')
    cutoff = pd.Timestamp(aligned_cutoff, unit='s', tz='UTC')
    if cutoff.timestamp() > event['source_closed_at']:
        raise HistoryContractError('cutoff_after_event')
    anchor = anchor_frame.copy(deep=True)
    _frame_contract(anchor, event['code'], frequency, cutoff)
    _basis(anchor, primary_attrs)
    if anchor.date.max() != cutoff or anchor.attrs.get('bar_time_label') != 'end':
        raise HistoryContractError('anchor_cutoff_mismatch')
    days = TARGET_DAYS[frequency]
    target = None if days is None else cutoff - pd.Timedelta(days=days)
    supported = (event['market'] == 'currency_spot' and primary_attrs.get('price_basis_provider') == 'binance'
                 and primary_attrs.get('price_basis_adjustment') == 'none'
                 and str(config.EXCHANGE_CURRENCY_SPOT).lower() == 'binance_spot')
    reusable = target is not None and anchor.date.min() <= target
    source = page_source or (_default_page_source() if supported and not reusable else None)
    identity = _digest({'event_id': event.get('id'), 'market': event['market'], 'code': event['code'],
        'frequency': frequency, 'cutoff': aligned_cutoff, 'event_cutoff': event['source_closed_at'],
        'anchor': _frame_digest(anchor), 'source': getattr(source, 'identity', None),
        'schema': SCHEMA, 'reader_revision': _reader_revision()})
    frame, cursor = anchor.iloc[:0].copy(), cutoff
    pages, checked, proofs = 0, 0, []
    reason, error_code, origin, usable, next_retry = 'page_budget', None, False, True, 2
    started = monotonic()

    def result():
        oldest = None if frame.empty else int(frame.date.min().timestamp())
        newest = None if frame.empty else int(frame.date.max().timestamp())
        reached = bool(target is not None and oldest is not None and oldest <= target.timestamp())
        state = {'schema': SCHEMA, 'identity': identity, 'cursor': cursor.tz_convert('UTC').isoformat(),
            'pages': pages, 'overlap_rows': checked, 'proofs': proofs, 'frame_hash': _frame_digest(frame),
            'reason': reason, 'source_origin': origin, 'usable': usable, 'error_code': error_code,
            'next_retry':next_retry}
        state['checksum'] = _digest(state)
        coverage = {'purpose': 'independent_background', 'consumer_validation_required': True,
            'price_basis_overlap_verified': checked > 0, 'anchor_rows': len(anchor),
            'anchor_overlap_rows': checked, 'anchor_complete': checked == len(anchor),
            'target_days': days, 'requested_from': None if target is None else int(target.timestamp()),
            'requested_to': int(cutoff.timestamp()), 'available_from': oldest, 'available_to': newest,
            'rows': len(frame), 'target_reached': reached, 'source_origin': origin, 'reason': reason,
            'error_code': error_code, 'pages': pages, 'next_retry':next_retry,
            'can_resume': reason in {'page_budget', 'deadline', 'source_error'},
            'compatible_with_anchor': usable and checked > 0, 'elapsed_seconds': max(0., monotonic() - started),
            'source_scope': 'binance_spot', 'source_label': SOURCE_LABEL}
        return HistoryResult(frame.copy(deep=True), coverage, state)

    if reusable or not supported:
        frame, checked = anchor, len(anchor)
        reason = 'frozen_target' if reusable else 'unsupported_source'
        return result()
    if previous is not None:
        state = previous.state
        if (state.get('identity') != identity or state.get('schema') != SCHEMA
                or state.get('checksum') != _digest({k:v for k,v in state.items() if k != 'checksum'})
                or state.get('frame_hash') != _frame_digest(previous.frame)):
            raise HistoryContractError('resume_identity_conflict')
        frame = previous.frame.copy(deep=True)
        cursor, pages = pd.Timestamp(state['cursor']), state['pages']
        checked, proofs = state['overlap_rows'], list(state['proofs'])
        reason, origin, usable = state['reason'], state['source_origin'], state['usable']
        if reason not in {'page_budget', 'deadline', 'source_error'}:
            error_code = state['error_code']
            next_retry = state.get('next_retry',2)
            return result()
    reason = 'page_budget'
    if not fetch:
        return result()
    for _ in range(max_pages):
        remaining = timeout_seconds - (monotonic() - started)
        if remaining <= 0:
            reason = 'deadline'
            break
        try:
            data = source(code=event['code'], frequency=frequency, cursor=cursor, remaining=remaining)
            page = _decode_page(data, code=event['code'], frequency=frequency, cursor=cursor)
            proof = {'before': cursor.tz_convert('UTC').isoformat(), 'response_hash': _digest(data),
                     'request': _request_parameters(event['code'], frequency, cursor),
                     'source_url': SOURCE_URL, 'rows': 0 if page is None else len(page)}
            pages += 1
            if page is None:
                proofs.append(proof)
                origin, reason = True, 'source_origin'
                if checked != len(anchor):
                    usable, error_code = False, 'origin_before_anchor_covered'
                break
            _basis(page, primary_attrs)
            candidate = page if frame.empty else pd.concat([page, frame], ignore_index=True)
            candidate.attrs = dict(page.attrs)
            _frame_contract(candidate, event['code'], frequency, cutoff)
            overlap = _overlap(candidate, anchor)
            if not overlap:
                raise HistoryContractError('page_without_anchor_overlap')
            if candidate.date.max() != cutoff:
                raise HistoryContractError('page_missing_cutoff')
            frame, checked = candidate, overlap
            cursor = frame.date.min() - pd.Timedelta(milliseconds=1)
            proofs.append(proof)
            if target is not None and frame.date.min() <= target:
                reason = 'target_reached'
                break
        except HistoryContractError as exc:
            reason, error_code, usable = 'contract_error', exc.code, False
            break
        except Exception as exc:
            reason = 'deadline' if isinstance(exc, TimeoutError) or monotonic() - started >= timeout_seconds else 'source_error'
            error_code = getattr(exc, 'code', None) or type(exc).__name__
            delay = getattr(exc, 'retry_after', None)
            if type(delay) in (int, float) and math.isfinite(delay):
                next_retry = max(2, min(3600, math.ceil(delay)))
            break
    return result()
