from datetime import datetime, timedelta
import gzip
import hashlib
import json
from pathlib import Path

import pandas as pd
import pytest

from chanlun.screening import cache, rolling_proofs, runner
from chanlun.screening.rules import CN
from chanlun.screening.timeframe_policy import POLICY


def frame():
    dates = pd.date_range('2026-09-01', periods=500, freq='5min', tz=CN)
    return pd.DataFrame({'code': 'SH.600000', 'date': dates, 'open': 10., 'high': 11., 'low': 9., 'close': 10., 'volume': 100.})


def save(path, data, revision='r'):
    value = {'schema': rolling_proofs.SCHEMA, 'calculation_revision': revision,
             'source_input': cache._save_source_input(path, data, 'SH.600000', '5m'),
             'window_start': int(data.date.iloc[0].timestamp()),
             'reviewed': {'formal': {name: True for name in cache.REPLAY_CHECKS},
                          'point-preview:unconfirmed': {name: True for name in cache.REPLAY_CHECKS}}}
    cache._write_calculation_payload(path, value)


def test_only_the_identical_new_window_prefix_reuses_verified_formal_proofs(tmp_path):
    old = frame()
    current = old.iloc[48:].reset_index(drop=True)
    path = rolling_proofs.proof_path(tmp_path / 'stock_5m.json.gz')
    save(path, current.iloc[:-24].copy())
    expected = {'formal': {name: True for name in cache.REPLAY_CHECKS}}
    assert cache.read_append_proofs(path, current, 'SH.600000', '5m', 'r') == expected
    assert cache.read_append_proofs(path, old, 'SH.600000', '5m', 'r') == {}
    assert cache.read_append_proofs(path, current, 'SH.600000', '5m', 'changed') == {}
    assert cache.read_calculation(path, 'any') is None  # Proof cache cannot impersonate a current selection.


@pytest.mark.parametrize('change', ['volume', 'price', 'start', 'basis', 'corrupt'])
def test_changed_or_untrusted_rolling_inputs_require_full_replay(tmp_path, change):
    current = frame().iloc[48:].reset_index(drop=True)
    path = rolling_proofs.proof_path(tmp_path / 'stock_5m.json.gz')
    save(path, current.iloc[:-24].copy())
    if change == 'volume':
        current.loc[0, 'volume'] += 1
    elif change == 'price':
        current.loc[5, 'close'] += .01
    elif change == 'start':
        current = current.iloc[1:].copy()
    elif change == 'basis':
        current.attrs['price_basis_revision'] = 'corrected'
    else:
        path.write_bytes(b'incomplete write')
    assert cache.read_append_proofs(path, current, 'SH.600000', '5m', 'r') == {}


def test_next_window_planning_is_after_close_only_and_uses_verified_session(monkeypatch):
    settings = {'timeframe_policy': POLICY, 'frequencies': ['5m'], 'recent_sessions': 5, 'max_anchor_sessions': 20}
    noon = datetime(2026, 10, 16, 11, 30, tzinfo=CN)
    assert rolling_proofs.window_targets(settings, {'5m': {'cutoff': int(noon.timestamp())}}) == {}
    close = noon.replace(hour=15, minute=0)
    def calendar(observed, *args):
        cutoff = close if observed.weekday() >= 5 else observed
        return {'cutoff': int(cutoff.timestamp()), 'history_from': '2026-09-01'}
    monkeypatch.setattr(runner, '_a_calendar_context', calendar)
    assert rolling_proofs.window_targets(settings, {'5m': {'cutoff': int(close.timestamp())}}) == {'5m': '2025-10-19'}
    monkeypatch.setattr(runner, '_a_calendar_context', lambda *args: {'calendar_error': 'unknown'})
    assert rolling_proofs.window_targets(settings, {'5m': {'cutoff': int(close.timestamp())}}) == {}


def test_failed_optional_preverification_never_changes_a_trade_result(tmp_path):
    row = {'code': 'SH.600000', 'market': 'a', 'rows': [{'frequency': '5m', 'input_fingerprint': 'missing'}], 'seconds': 1.}
    before = json.dumps(row)
    outcome = rolling_proofs.prepare({'code': 'SH.600000'}, {'frequencies': ['5m']}, {}, tmp_path / 'run', 'r', row, {'5m': '2026-09-02'})
    assert outcome['state'] == 'unavailable'
    assert json.dumps(row) == before
