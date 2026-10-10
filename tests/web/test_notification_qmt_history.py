"""A notification may expand local history without rewriting signal evidence."""
from copy import deepcopy
from unittest.mock import Mock

import pandas as pd
import pytest

from cl_app.services.notification_qmt_history import prepare_qmt_notification_history
from chanlun.exchange.exchange_qmt import ExchangeQMT


@pytest.fixture
def qmt_history(monkeypatch):
    dates = pd.date_range('2026-06-01 15:00', '2026-10-09 15:00', freq='B', tz='Asia/Shanghai')
    frame = pd.DataFrame({'date': dates, 'code': 'SH.513100', 'open': 2., 'high': 2.2,
                          'low': 1.9, 'close': 2.1, 'volume': 100.})
    frame.attrs = {'price_basis_provider': 'qmt', 'price_basis_adjustment': 'front_ratio',
                   'price_basis_revision': 'controlled-frozen-factor-ledger',
                   'structure_price_quantum': '0.001', 'bar_time_label': 'end'}
    anchor = frame.tail(5).copy(deep=True)
    cutoff = int(frame.date.iloc[-1].timestamp())
    event = {'id': 'frozen-point', 'market': 'a', 'code': 'SH.513100', 'source_closed_at': cutoff}
    source = Mock(side_effect=lambda *_args, **_kwargs: frame.copy(deep=True))
    monkeypatch.setattr(ExchangeQMT, '_klines_once', source)
    # Session/holiday validity is deliberately outside this source-contract
    # fixture; integration verifies real frames with the unchanged validator.
    monkeypatch.setattr('chanlun.screening.runner._resolve_suspensions', lambda data, *_: data)
    return frame, anchor, event, cutoff, source


def read(fixture, frequency='30m', **kwargs):
    _frame, anchor, event, cutoff, _source = fixture
    return prepare_qmt_notification_history(event, frequency, anchor.attrs,
        anchor_frame=anchor, aligned_cutoff=cutoff, **kwargs)


def test_first_render_never_reads_more_history(qmt_history):
    _frame, _anchor, _event, _cutoff, source = qmt_history
    result = read(qmt_history, fetch=False)
    assert result.coverage['can_resume'] and not result.coverage['source_origin']
    source.assert_not_called()


def test_all_30m_history_means_complete_local_source_not_remote_exchange(qmt_history):
    frame, anchor, event, cutoff, source = qmt_history
    original, original_event = anchor.copy(deep=True), deepcopy(event)
    result = read(qmt_history)
    request = source.call_args.kwargs
    assert request['start_date'] == '19700101'
    assert request['end_date'] == '2026-10-09 15:00:00'
    assert request['args'] == {'exact_end': True, 'skip_download': True, 'local_only': True,
                               'dividend_type': 'front_ratio'}
    assert result.coverage['source_scope'] == 'qmt_local'
    assert result.coverage['source_origin'] and result.coverage['anchor_complete']
    assert result.coverage['consumer_validation_required']
    pd.testing.assert_frame_equal(result.frame, frame)
    pd.testing.assert_frame_equal(anchor, original)
    assert event == original_event
    again = read(qmt_history, previous=result)
    pd.testing.assert_frame_equal(again.frame, result.frame)
    assert source.call_count == 1


@pytest.mark.parametrize('frequency,days', [('1m', 30), ('5m', 90)])
def test_finite_history_uses_a_bounded_local_window(qmt_history, frequency, days):
    result = read(qmt_history, frequency)
    source = qmt_history[-1]
    target = pd.Timestamp(qmt_history[3], unit='s', tz='Asia/Shanghai') - pd.Timedelta(days=days + 14)
    assert source.call_args.kwargs['start_date'] == target.strftime('%Y%m%d')
    assert result.coverage['target_reached'] and not result.coverage['source_origin']


def test_sufficient_frozen_range_requires_no_source_request(qmt_history):
    frame, _anchor, event, cutoff, source = qmt_history
    result = prepare_qmt_notification_history(event, '5m', frame.attrs,
        anchor_frame=frame, aligned_cutoff=cutoff)
    assert result.coverage['reason'] == 'frozen_target'
    source.assert_not_called()


@pytest.mark.parametrize('damage', ['price', 'volume', 'basis', 'missing_anchor', 'future'])
def test_changed_or_incomplete_source_never_replaces_original_anchor(qmt_history, damage):
    frame, anchor, _event, cutoff, source = qmt_history
    damaged = frame.copy(deep=True)
    if damage == 'price':
        damaged.loc[damaged.index[-2], 'close'] += .01
    elif damage == 'volume':
        damaged.loc[damaged.index[-2], 'volume'] += 1
    elif damage == 'basis':
        damaged.attrs['price_basis_revision'] = 'different-factors'
    elif damage == 'missing_anchor':
        damaged = damaged.drop(damaged.index[-2])
    else:
        damaged.loc[damaged.index[-1], 'date'] = pd.Timestamp(cutoff+60, unit='s', tz='Asia/Shanghai')
    source.side_effect = lambda *_args, **_kwargs: damaged
    original = anchor.copy(deep=True)
    result = read(qmt_history)
    assert result.coverage['reason'] == 'contract_error'
    assert not result.coverage['compatible_with_anchor']
    assert not result.coverage['source_origin']
    pd.testing.assert_frame_equal(anchor, original)


def test_empty_or_failed_local_read_does_not_prove_origin(qmt_history):
    source = qmt_history[-1]
    source.side_effect = TimeoutError('controlled local service failure')
    result = read(qmt_history)
    assert result.coverage['reason'] == 'source_error' and result.coverage['can_resume']
    assert not result.coverage['source_origin']
    source.side_effect = lambda *_args, **_kwargs: qmt_history[0].iloc[:0].copy()
    assert not read(qmt_history).coverage['source_origin']


def test_short_finite_local_window_cannot_claim_entire_source(qmt_history):
    source = qmt_history[-1]
    source.side_effect = lambda *_args, **_kwargs: qmt_history[1].copy(deep=True)
    result = read(qmt_history, '1m')
    assert result.coverage['reason'] == 'unsupported_source'
    assert not result.coverage['target_reached'] and not result.coverage['source_origin']
