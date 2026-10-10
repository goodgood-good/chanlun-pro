"""Public Spot history stays separate from immutable notification evidence."""
from copy import deepcopy
import json
from pathlib import Path
from types import SimpleNamespace

import pandas as pd
import pytest

from chanlun import config
from cl_app.services import notification_binance_history as history
from cl_app.services.notification_history import HistoryContractError, HistoryResult


@pytest.fixture(autouse=True)
def binance_config(monkeypatch):
    monkeypatch.setattr(config, 'EXCHANGE_CURRENCY_SPOT', 'binance_spot')


def raw_page(closes, frequency='1m'):
    step = int(frequency[:-1]) * 60000
    return [[int(pd.Timestamp(stamp).value // 1_000_000) - step, '10', '12', '9', '11', '100',
             int(pd.Timestamp(stamp).value // 1_000_000) - 1, '1100', 10, '40', '440', '0'] for stamp in closes]


class Pages:
    identity = 'fake-binance-spot'

    def __init__(self, *pages):
        self.pages, self.calls = list(pages), []

    def __call__(self, **kwargs):
        self.calls.append(kwargs)
        value = self.pages.pop(0)
        if isinstance(value, Exception):
            raise value
        return deepcopy(value)


def inputs(frequency='1m'):
    cutoff = pd.Timestamp('2026-10-09 09:30Z')
    anchor = history._decode_page(raw_page([cutoff - pd.Timedelta(minutes=int(frequency[:-1])), cutoff], frequency),
                                  code='BTC/USDT', frequency=frequency, cursor=cutoff)
    event = {'id': 'fixture', 'market': 'currency_spot', 'code': 'BTC/USDT',
             'frequency': '1m', 'source_closed_at': int(cutoff.timestamp())}
    return event, frequency, dict(anchor.attrs), {'anchor_frame': anchor, 'aligned_cutoff': int(cutoff.timestamp())}


def run(inp, **kwargs):
    return history.prepare_binance_notification_history(*inp[:3], **inp[3], **kwargs)


@pytest.mark.parametrize('frequency,days', [('1m', 30), ('5m', 90)])
def test_targets_use_real_native_pages_with_original_cutoff_and_exact_overlap(frequency, days):
    inp = inputs(frequency)
    saved_event, saved_anchor = deepcopy(inp[0]), deepcopy(inp[3]['anchor_frame'])
    anchor = inp[3]['anchor_frame']
    pages = Pages(raw_page(anchor.date, frequency), raw_page([anchor.date.max() - pd.Timedelta(days=days)], frequency))
    result = run(inp, page_source=pages)
    assert isinstance(result, HistoryResult)
    assert result.coverage['target_reached'] and result.coverage['anchor_complete']
    assert result.coverage['source_scope'] == 'binance_spot' and 'scope' not in result.coverage
    assert not result.coverage['source_origin']
    assert pages.calls[1]['cursor'] == anchor.date.min() - pd.Timedelta(milliseconds=1)
    assert inp[0] == saved_event
    pd.testing.assert_frame_equal(inp[3]['anchor_frame'], saved_anchor)
    assert result.frame.attrs['price_basis_revision'] == saved_anchor.attrs['price_basis_revision']


def test_close_clock_is_plus_period_not_raw_last_millisecond():
    cutoff = pd.Timestamp('2026-10-09 09:30Z')
    page = raw_page([cutoff], '5m')
    result = history._decode_page(page, code='BTC/USDT', frequency='5m', cursor=cutoff)
    assert result.date.iloc[0] == cutoff and result.attrs['bar_time_label'] == 'end'
    parameters = history._request_parameters('BTC/USDT', '5m', cutoff)
    assert parameters == {'symbol': 'BTCUSDT', 'interval': '5m', 'endTime': page[0][0], 'limit': 1000}


def test_short_page_and_caps_never_prove_origin_and_resume_is_immutable():
    inp = inputs('30m')
    pages = Pages(raw_page(inp[3]['anchor_frame'].date, '30m'), [])
    first = run(inp, page_source=pages, max_pages=1)
    original = deepcopy(first)
    assert first.coverage['can_resume'] and not first.coverage['source_origin']
    second = run(inp, page_source=pages, previous=first, max_pages=1)
    assert second.coverage['source_origin'] and second.state['proofs'][-1]['rows'] == 0
    assert second.state['proofs'][-1]['source_url'] == history.SOURCE_URL
    assert first.state == original.state
    pd.testing.assert_frame_equal(first.frame, original.frame)
    assert second.coverage['requested_from'] is None


def test_empty_page_before_full_anchor_verified_is_not_usable():
    inp = inputs('30m')
    result = run(inp, page_source=Pages(raw_page(inp[3]['anchor_frame'].date.tail(1), '30m'), []))
    assert result.coverage['source_origin'] and not result.coverage['compatible_with_anchor']
    assert result.coverage['error_code'] == 'origin_before_anchor_covered'


def test_provider_cooldown_is_preserved_for_chart_scheduler():
    result = run(inputs(), page_source=Pages(history.BinanceHistoryUnavailable(429, 300)))
    assert result.coverage['reason'] == 'source_error'
    assert result.coverage['next_retry'] == result.state['next_retry'] == 300


@pytest.mark.parametrize('fault', ['price', 'volume', 'close_clock', 'open_clock', 'duplicate', 'reverse', 'future', 'bad_envelope', 'nan', 'bool', 'ohlc'])
def test_invalid_or_conflicting_pages_do_not_authorize_history(fault):
    inp = inputs()
    raw = raw_page(inp[3]['anchor_frame'].date)
    if fault == 'price': raw[0][1] = '10.5'
    elif fault == 'volume': raw[0][5] = '101'
    elif fault == 'close_clock': raw[0][6] += 1
    elif fault == 'open_clock': raw[0][0] += 1
    elif fault == 'duplicate': raw.append(raw[-1])
    elif fault == 'reverse': raw.reverse()
    elif fault == 'future': raw[-1][0] += 60000; raw[-1][6] += 60000
    elif fault == 'bad_envelope': raw = {'code': -1, 'msg': 'not a successful empty page'}
    elif fault == 'nan': raw[0][1] = 'NaN'
    elif fault == 'bool': raw[0][5] = True
    elif fault == 'ohlc': raw[0][2] = '8'
    result = run(inp, page_source=Pages(raw))
    assert result.coverage['reason'] == 'contract_error'
    assert not result.coverage['compatible_with_anchor'] and not result.coverage['source_origin']
    assert not result.coverage['can_resume']


def test_failed_http_is_resumable_not_origin_and_error_message_not_persisted():
    inp = inputs('30m')
    first = run(inp, page_source=Pages(raw_page(inp[3]['anchor_frame'].date, '30m'), RuntimeError('private-debug')))
    assert first.coverage['can_resume'] and first.coverage['reason'] == 'source_error'
    assert not first.coverage['source_origin'] and 'private-debug' not in str(first)
    resumed = run(inp, page_source=Pages([]), previous=first)
    assert resumed.coverage['source_origin']


def test_resume_identity_and_frame_cannot_be_replaced():
    inp = inputs('30m')
    first = run(inp, page_source=Pages(raw_page(inp[3]['anchor_frame'].date, '30m')), max_pages=1)
    first.frame.loc[0, 'close'] = 10.1
    with pytest.raises(HistoryContractError, match='resume_identity_conflict'):
        run(inp, page_source=Pages([]), previous=first)


def test_fetch_false_and_unsupported_provider_never_request(monkeypatch):
    inp, pages = inputs(), Pages()
    result = run(inp, page_source=pages, fetch=False)
    assert result.coverage['can_resume'] and not result.coverage['source_origin'] and pages.calls == []
    monkeypatch.setattr(config, 'EXCHANGE_CURRENCY_SPOT', 'other')
    result = run(inp, page_source=pages)
    assert result.coverage['reason'] == 'unsupported_source' and pages.calls == []


def test_deadline_stops_before_next_page():
    inp = inputs('30m'); clock = [100.]
    pages = Pages(raw_page(inp[3]['anchor_frame'].date, '30m'))
    def source(**kwargs):
        result = pages(**kwargs); clock[0] += 21; return result
    source.identity = pages.identity
    result = run(inp, page_source=source, monotonic=lambda: clock[0])
    assert result.coverage['reason'] == 'deadline' and len(pages.calls) == 1
    assert result.coverage['can_resume'] and not result.coverage['source_origin']


def test_public_source_reuses_session_has_no_auth_and_respects_rate_backoff():
    clock, calls, closed = [100.], [], []
    responses = iter([SimpleNamespace(status_code=200, headers={}, json=lambda: []),
                      SimpleNamespace(status_code=429, headers={'Retry-After': '30'}, json=lambda: [])])
    def get(url, **kwargs):
        calls.append((url, kwargs)); return next(responses)
    source = history.BinanceSpotPageSource(session=SimpleNamespace(get=get, close=lambda: closed.append(True)),
              monotonic=lambda: clock[0], sleep=lambda wait: clock.__setitem__(0, clock[0] + wait))
    kwargs = {'code': 'BTC/USDT', 'frequency': '1m', 'cursor': pd.Timestamp('2026-10-09 09:30Z'), 'remaining': 10}
    assert source(**kwargs) == []
    with pytest.raises(history.BinanceHistoryUnavailable): source(**kwargs)
    with pytest.raises(history.BinanceHistoryUnavailable): source(**kwargs)
    assert len(calls) == 2 and all(url == history.SOURCE_URL for url, _ in calls)
    assert all(set(kwargs) == {'params', 'timeout', 'allow_redirects'} for _, kwargs in calls)
    assert all(kwargs['allow_redirects'] is False for _, kwargs in calls)
    source.close(); assert closed == [True]


def test_public_source_rejects_derivative_symbols_without_any_request():
    calls = []
    source = history.BinanceSpotPageSource(session=SimpleNamespace(get=lambda *_a, **_k: calls.append(True)))
    with pytest.raises(HistoryContractError, match='invalid_spot_symbol'):
        source(code='BTC/USDT:USDT', frequency='1m', cursor=pd.Timestamp('2026-10-09 09:30Z'), remaining=10)
    assert calls == []


def test_actual_archived_btc_precision_conflict_must_not_be_joined():
    fixture = json.loads((Path(__file__).parents[1] / 'fixtures/binance_history/BTC_USDT_frozen_overlap_20261010.json').read_text('utf-8'))
    bar = fixture['captured_frozen_bar']
    frame = pd.DataFrame([{**bar, 'code': fixture['code']}])
    frame['date'] = pd.to_datetime(frame.date, utc=True)
    frame.attrs = fixture['attrs']
    cutoff = int(frame.date.iloc[0].timestamp())
    event = {'id': 'actual-archived-conflict', 'market': 'currency_spot', 'code': fixture['code'],
             'frequency': '1m', 'source_closed_at': cutoff}
    result = history.prepare_binance_notification_history(event, '1m', frame.attrs,
        anchor_frame=frame, aligned_cutoff=cutoff, page_source=Pages([fixture['official_raw_bar']]))
    assert result.coverage['error_code'] == 'price_basis_conflict'
    assert not result.coverage['compatible_with_anchor'] and not result.coverage['can_resume']
    assert frame.open.iloc[0] == 81421.9
    assert float(fixture['official_raw_bar'][1]) == 81421.91
