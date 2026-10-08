from copy import deepcopy
from contextlib import contextmanager
import json
from types import SimpleNamespace
from unittest.mock import Mock

import pandas as pd
import pytest

from cl_app.services import notification_charts, notification_periods, tradingview_snapshot as tv


def source(frequency, cutoff=1791426600):
    frame = pd.DataFrame({'date': pd.date_range(end=pd.Timestamp(cutoff, unit='s', tz='UTC'),
                                               periods=240, freq=frequency.replace('m', 'min')),
                          'open': 10., 'high': 12., 'low': 9., 'close': 11., 'volume': 100.})
    frame.attrs.update(price_basis_revision='fixture-basis', structure_price_quantum='0.01')
    chart = {name: [value] * len(frame) for name, value in (
        ('macd_dif', .4), ('macd_dea', .2), ('macd_hist', .4),
        ('higher_macd_dif', .8), ('higher_macd_dea', .3), ('higher_macd_hist', 1.))}
    snapshot = {'source_frequency': frequency, 'source_closed_at': cutoff, 'chart': chart,
                'structure_price_quantum': '0.01', 'price_basis_revision': 'fixture-basis',
                'levels': [], 'screening_segments': []}
    return frame, snapshot, {'anchor_at': cutoff - int(frequency[:-1]) * 60, 'anchor_price': 11.}


@pytest.fixture
def signal(monkeypatch):
    event = {'market': 'a', 'code': 'SZ.002611', 'name': 'fixture', 'frequency': '5m',
             'source_closed_at': 1791426600, 'anchor_price': 11., 'point_type': '1buy',
             'stage': 'confirmed', 'evidence_run_id': 'e' * 32}
    frozen = source('5m')
    monkeypatch.setattr(notification_charts, 'load_chart_evidence', lambda *_: frozen)
    return event, frozen


def test_three_periods_keep_both_indicator_series_and_primary_evidence(signal, monkeypatch, tmp_path):
    event, frozen = signal
    original = deepcopy(frozen[1])
    requested = []

    def context(event, root, frequency, attrs):
        requested.append(frequency)
        frame, snapshot, _ = source(frequency, event['source_closed_at'] - (600 if frequency == '30m' else 0))
        return frame, snapshot

    monkeypatch.setattr(notification_periods, 'context_evidence', context)
    panels = tv.prepare_periods(event, tmp_path)['panels']
    assert [p['settings']['frequency'] for p in panels] == ['1m', '5m', '30m']
    assert requested == ['1m', '30m']
    assert [p['settings']['signal_panel'] for p in panels] == [False, True, False]
    assert panels[1] == tv.prepare(event, tmp_path)
    for panel in panels:
        assert panel['history']['macd_dif'] == [.4] * 240
        assert panel['history']['higher_macd_dif'] == [.8] * 240
        assert panel['settings']['cutoff'] <= event['source_closed_at']
    assert frozen[1] == original


def test_auxiliary_chart_does_not_repeat_the_trigger_or_stop_line(signal, monkeypatch, tmp_path):
    event, _ = signal
    event['exit_plan'] = {'stop_loss': {'status': 'reference', 'price': 9.}}
    monkeypatch.setattr(notification_periods, 'context_evidence', lambda e, r, f, a: source(f)[:2])
    panels = tv.prepare_periods(event, tmp_path)['panels']
    assert [p['settings']['stop_price'] for p in panels] == [None, 9., None]
    assert '信号来自 5m' in panels[0]['settings']['title']


def test_missing_context_is_explicit_and_cannot_suppress_the_signal_chart(signal, monkeypatch, tmp_path):
    event, _ = signal
    monkeypatch.setattr(notification_periods, 'context_evidence', Mock(side_effect=OSError('offline')))
    panels = tv.prepare_periods(event, tmp_path)['panels']
    assert 'error' in panels[0] and 'error' in panels[2]
    assert panels[1]['history']['s'] == 'ok' and panels[1]['settings']['signal_panel']
    assert 'offline' not in panels[0]['error']


def test_primary_evidence_failure_is_not_converted_to_a_context_warning(signal, monkeypatch, tmp_path):
    event, _ = signal
    monkeypatch.setattr(notification_charts, 'load_chart_evidence', Mock(side_effect=ValueError('mismatched')))
    with pytest.raises(ValueError, match='mismatched'):
        tv.prepare_periods(event, tmp_path)


@pytest.mark.parametrize('field', ['higher_macd_dif', 'higher_macd_dea', 'higher_macd_hist'])
def test_missing_htf_is_rejected_instead_of_drawing_local_macd_twice(signal, tmp_path, field):
    event, (_, snapshot, _) = signal
    snapshot['chart'][field] = []
    with pytest.raises(ValueError, match='MACD is incomplete'):
        tv.prepare(event, tmp_path)


def test_context_crops_future_bars_before_running_the_structure_engine(signal, monkeypatch, tmp_path):
    from chanlun.screening import runner, rules
    event, (primary, _, _) = signal
    frame, _, _ = source('1m', event['source_closed_at'] + 600)
    context = {'cutoff': event['source_closed_at'], 'frequency': '1m'}
    monkeypatch.setattr(rules, 'trading_context', lambda *_: context)
    monkeypatch.setattr(rules, 'frame_quality', lambda f, c: {'errors': [] if int(f.date.iloc[-1].timestamp()) == c['cutoff'] else ['STALE_DATA']})
    monkeypatch.setattr(runner, 'fetch_frame', lambda *_: frame.copy())
    build = Mock(return_value={'source_closed_at': context['cutoff']})
    monkeypatch.setattr(runner, 'snapshot_for', build)
    output, _ = notification_periods.context_evidence(event, tmp_path, '1m', primary.attrs)
    assert int(output.date.iloc[-1].timestamp()) == event['source_closed_at']
    assert len(output) == len(frame) - 10
    assert build.call_args.args[0].equals(output)


def test_context_rejects_different_adjustment_basis_before_building(signal, monkeypatch, tmp_path):
    from chanlun.screening import runner, rules
    event, (primary, _, _) = signal
    frame, _, _ = source('1m')
    frame.attrs['price_basis_revision'] = 'another-adjustment'
    monkeypatch.setattr(rules, 'trading_context', lambda *_: {'cutoff': event['source_closed_at']})
    monkeypatch.setattr(rules, 'frame_quality', lambda *_: {'errors': []})
    monkeypatch.setattr(runner, 'fetch_frame', lambda *_: frame)
    build = Mock()
    monkeypatch.setattr(runner, 'snapshot_for', build)
    with pytest.raises(ValueError, match='price basis differs'):
        notification_periods.context_evidence(event, tmp_path, '1m', primary.attrs)
    build.assert_not_called()


def test_five_minute_bars_cannot_be_presented_as_one_minute_context(signal, monkeypatch, tmp_path):
    from chanlun.screening import runner
    event, (primary, _, _) = signal
    event = {**event, 'market': 'currency_spot', 'code': 'BTC/USDT'}
    frame, _, _ = source('5m')
    monkeypatch.setattr(runner, 'fetch_frame', lambda *_: frame)
    build = Mock()
    monkeypatch.setattr(runner, 'snapshot_for', build)
    with pytest.raises(ValueError, match='candles failed validation'):
        notification_periods.context_evidence(event, tmp_path, '1m', primary.attrs)
    build.assert_not_called()


def test_period_routes_cannot_serve_a_different_resolution(signal, tmp_path):
    event, _ = signal
    payload = tv.prepare(event, tmp_path)
    route = Mock(request=SimpleNamespace(method='GET', url=tv.ORIGIN + '/tv/history?symbol=a%3ASZ.002611&resolution=1&firstDataRequest=true'))
    tv.route_request(route, payload)
    route.abort.assert_called_once()
    route.fulfill.assert_not_called()


@pytest.mark.parametrize('failed_period', ['1m', '30m'])
def test_auxiliary_browser_failure_preserves_the_primary_image(monkeypatch, failed_period):
    png = 'data:image/png;base64,iVBORw0KGgo='
    composed = []
    pages = []

    class Page:
        closed = False

        def route(self, *_):
            pass

        def add_init_script(self, value):
            self.settings = json.loads(value.split('=', 1)[1][:-1])

        def goto(self, *_args, **_kwargs):
            if self.settings['frequency'] == failed_period:
                raise RuntimeError('context chart failed')

        def wait_for_function(self, *_args, **_kwargs):
            pass

        def evaluate(self, expression, *args):
            assert not self.closed
            if '__notificationError' in expression:
                return None
            if 'composeNotificationCharts' in expression:
                composed.extend(args[0])
            return png

        def close(self):
            self.closed = True

    def new_page():
        page = Page()
        pages.append(page)
        return page

    browser = Mock()
    browser.new_context.return_value.new_page.side_effect = new_page

    @contextmanager
    def playwright():
        yield SimpleNamespace(chromium=SimpleNamespace(launch=lambda **_: browser))

    monkeypatch.setattr('playwright.sync_api.sync_playwright', playwright)
    panels = [{'settings': {'frequency': frequency, 'signal_panel': frequency == '5m'}} for frequency in tv.PERIODS]
    assert tv.capture({'panels': panels}).startswith(b'\x89PNG')
    assert [p['frequency'] for p in composed] == list(tv.PERIODS)
    assert [p['frequency'] for p in composed if p.get('error')] == [failed_period]
    assert composed[1]['data'] == png
    assert pages[0].closed and pages[2].closed
    browser.close.assert_called_once()
