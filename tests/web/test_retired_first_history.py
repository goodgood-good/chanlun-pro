"""A first and second point confirmed together retain an audited first history."""
from datetime import datetime
import json
from pathlib import Path

import pandas as pd

from chanlun.screening import runner
from chanlun.screening.rules import CN, POINT_TYPES
from cl_app.services.screening import ScreeningManager
from cl_app.services.screening_workbench import _current_rows
from cl_app.services.monitor_incremental import audited_members


def test_real_same_clock_first_and_second_remain_distinct_and_history_is_not_admitted(tmp_path, monkeypatch):
    frame = pd.read_parquet(Path('tests/fixtures/retired_first_SZ.000729_5m.parquet'))
    monkeypatch.setattr(runner, 'fetch_frame', lambda *_args, **_kwargs: frame.copy())
    settings = {'strategy': 'direct_structural', 'alert_mode': 'native_center_signals',
                'frequencies': ['5m'], 'point_types': list(POINT_TYPES), 'max_anchor_gain_pct': 10}
    context = runner._a_calendar_context(datetime(2026, 10, 13, 15, 0, tzinfo=CN), '5m', 5, 20)
    directory = tmp_path / ('c' * 32)
    directory.mkdir()
    raw = runner.scan_symbol({'market': 'a', 'code': 'SZ.000729', 'name': '燕京啤酒'}, settings,
                             {'5m': context}, directory)
    history, = raw['rows'][0]['historical_confirmed']
    assert history['point']['point_type'] == '1buy' and history['point']['status'] == 'confirmed'
    assert history['history']['simultaneous_confirmation']
    assert history['history']['successors'][0]['point_type'] == '2buy'
    assert all(history['audit'].values())
    assert all(c['point']['point_id'] != history['point']['point_id'] for c in raw['rows'][0]['selected'])
    (directory / 'results.jsonl').write_text(json.dumps(raw) + '\n', encoding='utf-8')
    result = ScreeningManager(tmp_path, warm_results=False)._read_results(directory, {'completed': 1, 'settings': settings})
    assert not result['errors'] and len(result['historical_confirmed']) == 1
    assert all(r['point']['point_type'] != '1buy' for r in _current_rows(result, directory.name))
    reviewed = _current_rows(result, directory.name, candidate_id=None, include_history=True)
    assert next(r for r in reviewed if r['point']['point_type'] == '1buy')['origin'] == 'history'
    empty_pool = {**result, 'selected': [], 'observations': []}
    assert audited_members(empty_pool)[0] == {}, 'historical points alone cannot seed live monitoring'
