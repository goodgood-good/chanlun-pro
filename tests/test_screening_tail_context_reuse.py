"""Peer-clock reuse must stay local to a preparation call and symbol-owned."""
from datetime import datetime
from types import SimpleNamespace

import pytest

from chanlun.screening import runner, tail_preparation as prep
from chanlun.screening.rules import CN
from chanlun.screening.timeframe_policy import POLICY


class CaptureWorker:
    def __init__(self):
        self.process = SimpleNamespace(pid=12345)
        self.batches = None

    def start(self, batches):
        self.batches = batches
        return self

    def poll(self, timeout=0):
        return True

    def recv(self):
        return {'done': True}

    def finish(self, *, successful):
        assert successful


@pytest.fixture
def calendar(monkeypatch):
    calls, returned = [], []

    def build(observed, period, recent, anchor):
        main = int(observed.timestamp())
        calls.append((main, period, recent, anchor))
        value = {'cutoff': main if period == '1m' else main // 1800 * 1800,
                 'calendar': {'sessions': [observed.date().isoformat()]},
                 'main': main, 'recent': recent, 'anchor': anchor}
        returned.append(value)
        return value

    monkeypatch.setattr(runner, '_a_calendar_context', build)
    monkeypatch.setattr(prep, 'saved_preparation', lambda *args: None)
    monkeypatch.setattr(prep, 'preparation_needed', lambda *args: True)
    return calls, returned


def prepare(tmp_path, main, *, recent=7, anchor=23):
    stocks = [{'market': 'a', 'code': f'SH.{600000 + index}',
               '_monitor_periods': ['5m']} for index in range(4)]
    settings = {'scope': 'symbols', 'timeframe_policy': POLICY,
                'frequencies': ['5m'], 'recent_sessions': recent,
                'max_anchor_sessions': anchor}
    worker = CaptureWorker()
    list(prep.prepare_inputs(stocks, settings, {'5m': {'cutoff': main}},
                            tmp_path / 'cache', tmp_path, worker=worker))
    targets = {}
    for codes, base, cutoff, batch_targets in worker.batches:
        for code, periods in batch_targets.items():
            targets.setdefault(code, {}).update(periods)
    return targets


def test_same_main_clock_builds_each_peer_once_with_independent_nested_contexts(tmp_path, calendar):
    calls, original = calendar
    main = int(datetime(2026, 10, 29, 10, 25, tzinfo=CN).timestamp())
    targets = prepare(tmp_path, main)
    assert calls == [(main, '1m', 7, 23), (main, '30m', 7, 23)]
    values = list(targets.values())
    for period in ('1m', '30m'):
        assert len({id(value[period]) for value in values}) == 4
        assert len({id(value[period]['calendar']['sessions']) for value in values}) == 4
        assert all(value[period] == original[0 if period == '1m' else 1] for value in values)
    values[0]['1m']['calendar']['sessions'].append('changed by consumer')
    assert all(len(value['1m']['calendar']['sessions']) == 1 for value in values[1:])
    assert all(len(value['calendar']['sessions']) == 1 for value in original)


def test_repeated_preparation_rebuilds_peer_clocks_even_for_identical_inputs(tmp_path, calendar):
    calls, _ = calendar
    main = int(datetime(2026, 10, 29, 10, 25, tzinfo=CN).timestamp())
    first = prepare(tmp_path, main)
    second = prepare(tmp_path, main)
    assert calls == [(main, '1m', 7, 23), (main, '30m', 7, 23)] * 2
    assert first == second
    assert first['SH.600000']['1m'] is not second['SH.600000']['1m']


def test_different_main_cutoffs_and_calendar_settings_keep_their_own_contexts(tmp_path, calendar):
    calls, _ = calendar
    main = int(datetime(2026, 10, 29, 10, 25, tzinfo=CN).timestamp())
    first = prepare(tmp_path, main)
    second = prepare(tmp_path, main + 300, recent=5, anchor=20)
    assert calls == [(main, '1m', 7, 23), (main, '30m', 7, 23),
                     (main + 300, '1m', 5, 20), (main + 300, '30m', 5, 20)]
    for periods in first.values():
        assert periods['1m']['cutoff'] == main
        assert periods['30m']['cutoff'] == main - 1500
        assert periods['30m']['anchor'] == 23
    for periods in second.values():
        assert periods['1m']['cutoff'] == periods['30m']['cutoff'] == main + 300
        assert periods['30m']['anchor'] == 20


def test_calendar_failure_keeps_original_exception(tmp_path, calendar, monkeypatch):
    failure = RuntimeError('calendar unavailable')

    def fail(*args):
        raise failure

    monkeypatch.setattr(runner, '_a_calendar_context', fail)
    with pytest.raises(RuntimeError) as caught:
        prepare(tmp_path, int(datetime(2026, 10, 29, 10, 25, tzinfo=CN).timestamp()))
    assert caught.value is failure
