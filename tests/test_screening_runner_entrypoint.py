import json
import runpy
import sys
from unittest.mock import Mock
import pytest

from chanlun.screening import runner, tail_preparation


def test_module_entrypoint_uses_the_same_runner_module_as_its_helpers(tmp_path,monkeypatch):
    execute=Mock()
    monkeypatch.setattr(runner,'run_screening',execute)
    monkeypatch.setattr(sys,'argv',['runner','--run-dir',str(tmp_path)])
    with pytest.warns(RuntimeWarning,match='found in sys.modules'):
        runpy.run_module('chanlun.screening.runner',run_name='__main__')
    execute.assert_called_once_with(tmp_path.resolve())


def test_preparation_cancellation_is_cancelled_instead_of_failed(tmp_path,monkeypatch):
    request={'run_id':tmp_path.name,'observed_at':'2026-10-14T15:08:05+08:00',
             'verify_completed_inputs':True,'settings':{'scope':'symbols','frequencies':['5m'],
              'strategy':'direct_structural','recent_sessions':5,'max_anchor_sessions':20}}
    (tmp_path/'request.json').write_text(json.dumps(request),encoding='utf-8')
    catalog={'stocks':[{'market':'a','code':'SH.600000','name':'fixture'}],'excluded_requested_codes':[]}
    monkeypatch.setattr(runner,'bounded_catalog',lambda *_:iter([catalog]))
    monkeypatch.setattr(runner,'_a_calendar_context',lambda *_:{'cutoff':123,'frequency':'5m'})
    def cancelled(*args,**kwargs):
        raise runner.ScreeningCancelled()
    monkeypatch.setattr(tail_preparation,'prepare_inputs',cancelled)
    runner.run_screening(tmp_path)
    state=json.loads((tmp_path/'status.json').read_text(encoding='utf-8'))
    assert state['status']=='cancelled'
    assert 'error' not in state
