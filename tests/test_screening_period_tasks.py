"""Native period commits are durable before a sibling minute task completes."""
from copy import deepcopy
import json

import pytest

from chanlun.screening import runner
from chanlun.screening.period_tasks import PeriodProgress, period_tasks
from chanlun.screening.worker_pool import _failed_result


PERIODS = ['1m', '5m', '30m']
STOCKS = [{'market': 'a', 'code': 'SH.600000', 'name': 'holding', 'holding': True},
          {'market': 'a', 'code': 'SZ.000001', 'name': 'watchlist', 'origin': 'watchlist'}]


def record(stock, frequencies=None):
    periods = stock.get('_monitor_periods', frequencies or PERIODS)
    return {'market': stock['market'], 'code': stock['code'], 'name': stock['name'],
            'requested_frequencies': list(periods),
            'rows': [{'market': stock['market'], 'code': stock['code'], 'name': stock['name'],
                      'frequency': frequency, 'selected': [], 'data_errors': [], 'source_closed_at': 1800}
                     for frequency in periods]}


def saved(directory):
    state = json.loads((directory / 'status.json').read_text(encoding='utf-8'))
    path = directory / 'results.jsonl'
    records = [json.loads(line) for line in path.read_text(encoding='utf-8').splitlines()] if path.exists() else []
    return state, records


@pytest.fixture
def run_fixture(tmp_path, monkeypatch):
    monkeypatch.setattr(runner, 'source_revision', lambda: 'isolated-period-contract')
    monkeypatch.setattr(runner, '_a_calendar_context', lambda _now, frequency, *_: {
        'frequency': frequency, 'cutoff': 1800, 'calendar_source': 'isolated'})
    monkeypatch.setattr('chanlun.screening.compute_capacity.worker_capacity', lambda *_args, **_kwargs: 2)
    monkeypatch.setattr(runner, 'fetch_frame', lambda *_args, **_kwargs: pytest.fail('period scheduling requested live data'))
    def run(stream, *, stocks=None, incremental=True, native=True):
        stocks = deepcopy(STOCKS if stocks is None else stocks)
        settings = {'scope': 'symbols', 'symbols': stocks, 'frequencies': PERIODS,
                    'strategy': 'direct_structural', 'recent_sessions': 5,
                    'max_anchor_sessions': 20, 'workers': 2}
        if native:
            settings['alert_mode'] = 'native_center_signals'
        request = {'run_id': 'a' * 32, 'observed_at': '2026-10-09T15:00:00+08:00', 'settings': settings}
        if incremental is not None:
            request['incremental_periods'] = incremental
        runner.write_json(tmp_path / 'request.json', request)
        monkeypatch.setattr(runner, 'bounded_catalog', lambda *_: iter([
            {'stocks': stocks, 'excluded_requested_codes': []}]))
        monkeypatch.setattr(runner, 'bounded_scan', stream)
        runner.run_screening(tmp_path)
        return saved(tmp_path)
    return run


def test_priority_waves_keep_catalog_identity_and_never_mutate_the_universe():
    before = deepcopy(STOCKS)
    tasks = period_tasks(STOCKS, PERIODS)
    assert [(task['code'], task['_monitor_periods']) for task in tasks] == [
        ('SH.600000', ['5m']), ('SH.600000', ['30m']),
        ('SZ.000001', ['5m']), ('SZ.000001', ['30m']),
        ('SH.600000', ['1m']), ('SZ.000001', ['1m'])]
    assert all(task['holding'] is True for task in tasks if task['code'] == 'SH.600000')
    assert STOCKS == before and all('_monitor_periods' not in stock for stock in STOCKS)


def test_first_5m_commit_is_readable_before_1m_starts_and_symbol_counts_finish_separately(run_fixture):
    checked = []
    def stream(tasks, settings, contexts, directory, workers, revision, **kwargs):
        assert len(tasks) == 6 and workers == 2
        assert json.loads((directory / 'universe.json').read_text(encoding='utf-8')) == STOCKS
        for index, task in enumerate(tasks, start=1):
            yield record(task), [task['code']]
            state, records = saved(directory)
            # This executes before the generator permits its next task. A
            # consumer can already use each committed period independently.
            assert len(records) == state['completed'] == index
            assert state['total'] == 6 and state['total_symbols'] == 2
            assert state['progress_unit'] == 'symbol_period'
            assert state['completed_symbols'] == max(0, index - 4)
            assert sum(value['completed'] for value in state['period_progress'].values()) == index
            assert all(value['total'] == 2 for value in state['period_progress'].values())
            if index == 1:
                assert records[0]['requested_frequencies'] == ['5m']
                assert all(r['frequency'] != '1m' for item in records for r in item['rows'])
                assert state['period_progress']['5m']['completed'] == 1
                assert state['period_progress']['1m']['completed'] == 0
            checked.append(index)
    state, records = run_fixture(stream)
    assert checked == [1, 2, 3, 4, 5, 6]
    assert state['status'] == 'completed' and state['completed_symbols'] == 2
    assert len(records) == 6 and state['successful_period_count'] == 6
    assert state['universe_coverage']['markets'] == {'a': 2}
    assert state['universe_coverage']['exchanges'] == {'SH': 1, 'SZ': 1}


@pytest.mark.parametrize('kind', ['worker_failure', 'top_level_error'])
def test_failed_period_keeps_its_identity_and_other_periods_still_commit(run_fixture, kind):
    def stream(tasks, settings, contexts, directory, *_args, **_kwargs):
        for task in tasks:
            if task['_monitor_periods'] == ['30m']:
                failed = _failed_result(task, settings, 'fixture source failure')
                if kind == 'top_level_error':
                    failed.update(rows=[], error='fixture source failure')
                yield failed, []
            else:
                yield record(task), []
    state, records = run_fixture(stream, stocks=STOCKS[:1])
    assert state['status'] == 'completed'
    assert state['completed'] == state['total'] == 3
    assert state['completed_symbols'] == state['total_symbols'] == 1
    assert state['error_count'] == 1 and state['successful_period_count'] == 2
    assert [item['requested_frequencies'] for item in records] == [['5m'], ['30m'], ['1m']]
    assert all(not item.get('error') and not item['rows'][0]['data_errors']
               for item in records if item['requested_frequencies'] != ['30m'])
    assert all(row['frequency'] == '30m' for row in records[1]['rows'])


def test_duplicate_period_commit_fails_without_appending_or_counting_it_twice(run_fixture):
    def stream(tasks, *_args, **_kwargs):
        item = record(tasks[0])
        yield item, []
        yield deepcopy(item), []
    state, records = run_fixture(stream, stocks=STOCKS[:1])
    assert state['status'] == 'failed' and 'committed twice' in state['error']
    assert state['completed'] == len(records) == 1 and state['completed_symbols'] == 0


def test_cancel_preserves_only_committed_period_prefix(run_fixture):
    def stream(tasks, settings, contexts, directory, *_args, **_kwargs):
        yield record(tasks[0]), []
        state, records = saved(directory)
        assert state['completed'] == len(records) == 1
        (directory / 'cancel').write_text('fixture cancellation', encoding='utf-8')
        return
    state, records = run_fixture(stream, stocks=STOCKS[:1])
    assert state['status'] == 'cancelled'
    assert state['completed'] == 1 and state['total'] == 3
    assert state['completed_symbols'] == 0 and state['total_symbols'] == 1
    assert records[0]['requested_frequencies'] == ['5m']


@pytest.mark.parametrize('incremental,native', [(None, True), (False, True), (True, False)])
def test_legacy_or_non_native_requests_retain_symbol_records(run_fixture, incremental, native):
    def stream(tasks, settings, *_args, **_kwargs):
        assert tasks == STOCKS
        for task in tasks:
            yield record(task, settings['frequencies']), []
    state, records = run_fixture(stream, incremental=incremental, native=native)
    assert state['status'] == 'completed' and state['completed'] == state['total'] == 2
    assert 'progress_unit' not in state and 'completed_symbols' not in state
    assert len(records) == 2 and all(len(item['rows']) == 3 for item in records)


def test_market_symbol_identity_keeps_separate_completion_and_rejects_unrequested_periods():
    stocks = [{'market': 'a', 'code': 'same', 'name': 'A'}, {'market': 'us', 'code': 'same', 'name': 'US'}]
    progress = PeriodProgress(stocks, ['5m', '30m'])
    assert progress.accept(record({**stocks[0], '_monitor_periods': ['5m']})) == 0
    assert progress.accept(record({**stocks[1], '_monitor_periods': ['5m']})) == 0
    assert progress.accept(record({**stocks[0], '_monitor_periods': ['30m']})) == 1
    with pytest.raises(ValueError, match='does not match'):
        progress.accept(record({**stocks[1], '_monitor_periods': ['1m']}))
    assert progress.accept(record({**stocks[1], '_monitor_periods': ['30m']})) == 2
