"""Display and receipts preserve the distinction between periods and symbols."""
from copy import deepcopy

import pytest

from cl_app.services.dingtalk_notifications import DingTalkOutbox
from cl_app.services.screening_progress import _merge
from cl_app.services.screening_rollover import RollingDisplay
from cl_app.services.screening_workbench import _dashboard_projection
from tests.web.test_dingtalk_notifications import Transport
from tests.web.test_screening_rollover import A, B, row, run, merge


def test_two_prefixes_keep_period_commits_and_empty_symbol_records():
    first = {'processed_symbols': [{'market': 'a', 'code': 'SH.600000'}],
             'processed_periods': [{'market': 'a', 'code': 'SH.600000', 'frequency': '5m'}],
             'empty_result_symbols': [{'market': 'a', 'code': 'SH.600000'}],
             'results_through': 64}
    second = {'processed_symbols': [{'market': 'a', 'code': 'SH.600000'}],
              'processed_periods': [{'market': 'a', 'code': 'SH.600000', 'frequency': '30m'}],
              'empty_result_symbols': [{'market': 'a', 'code': 'SH.600001'}],
              'results_through': 128}
    original = deepcopy((first, second))
    combined = _merge(first, second)
    assert combined['processed_periods'] == [
        {'market': 'a', 'code': 'SH.600000', 'frequency': '5m'},
        {'market': 'a', 'code': 'SH.600000', 'frequency': '30m'}]
    assert [item['code'] for item in combined['empty_result_symbols']] == ['SH.600000', 'SH.600001']
    assert combined['results_through'] == 128
    assert (first, second) == original
    projection = _dashboard_projection({**combined, 'run_id': B, 'progress_unit': 'symbol_period'}, False)
    assert projection['_workbench_processed_periods'] == [('a', 'SH.600000', '5m'), ('a', 'SH.600000', '30m')]


@pytest.mark.parametrize('successful', [False, True])
def test_a_5m_commit_cannot_mark_untouched_1m_or_30m_history_limited(tmp_path, successful):
    view = RollingDisplay(tmp_path)
    merge(view, run(tmp_path, A, 'completed'), [row('SH.600000', f) for f in ['1m', '5m', '30m']])
    payload = run(tmp_path, B, completed=1, total=3, total_symbols=1, completed_symbols=0,
                  progress_unit='symbol_period',
                  processed_symbols=[{'market': 'a', 'code': 'SH.600000'}],
                  processed_periods=[{'market': 'a', 'code': 'SH.600000', 'frequency': '5m'}],
                  verified_periods=[{'market': 'a', 'code': 'SH.600000', 'frequency': '5m'}] if successful else [])
    projected = _dashboard_projection(payload, False)
    rows, count = merge(view, projected)
    states = {item['frequency']: item['refresh_state'] for item in rows}
    assert states == ({'1m': 'pending', '30m': 'pending'} if successful else
                      {'1m': 'pending', '5m': 'limited', '30m': 'pending'})
    assert count == (2 if successful else 3)
    # The serialized display cache retains the same period distinction after
    # an application restart; no later response needs to invent symbol status.
    restored, _ = merge(RollingDisplay(tmp_path), projected)
    assert {item['frequency']: item['refresh_state'] for item in restored} == states


def test_legacy_symbol_commit_still_marks_all_its_old_periods_attempted(tmp_path):
    view = RollingDisplay(tmp_path)
    merge(view, run(tmp_path, A, 'completed'), [row('SH.600000', f) for f in ['1m', '5m', '30m']])
    payload = run(tmp_path, B, completed=1, processed_symbols=[{'market': 'a', 'code': 'SH.600000'}])
    rows, count = merge(view, _dashboard_projection(payload, False))
    assert count == 3 and all(item['refresh_state'] == 'limited' for item in rows)


@pytest.mark.parametrize('period_contract', [False, True])
def test_completion_summary_counts_actual_symbols_and_deduplicates_period_failures(tmp_path, period_contract):
    transport = Transport()
    box = DingTalkOutbox(tmp_path / 'outbox.sqlite', transport=transport, now=lambda: 1000)
    result = {'run_id': A, 'completed': 2, 'total': 2, 'selected': [], 'observations': [],
              'errors': [{'market': 'a', 'code': 'SH.600000', 'frequency': f} for f in ('1m', '5m')],
              'finished_at': 1000, 'source_current': True}
    if period_contract:
        result.update(progress_unit='symbol_period', completed=6, total=6, completed_symbols=2, total_symbols=2)
    box.screening_completed(result, [])
    with box._connect() as db:
        content = db.execute("SELECT content FROM outbox WHERE kind='screening'").fetchone()[0]
    assert '检查标的：2 / 2' in content and '检查标的：6 / 6' not in content
    assert '数据异常：1 个标的' in content
    assert not transport.messages  # Inspect a local receipt without dispatch.
