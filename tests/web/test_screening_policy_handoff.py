"""A requested policy change remains distinct from a failed calculation."""
import json
import time

import pytest

from cl_app.services.screening import ScreeningManager


@pytest.mark.parametrize('damage',[None,'identity','revision','future'])
def test_cancelled_run_exposes_only_a_matching_policy_handoff(tmp_path,damage):
    run='a'*32
    directory=tmp_path/run;directory.mkdir()
    stamp=time.time()
    state={'run_id':run,'status':'cancelled','source_revision':'b'*64,'started_at':stamp-60}
    (tmp_path/'latest.json').write_text(json.dumps({'run_id':run}),encoding='utf-8')
    (directory/'status.json').write_text(json.dumps(state),encoding='utf-8')
    marker={'schema':'screening-policy-handoff-v1','run_id':run,'source_revision':'b'*64,'requested_at':stamp}
    if damage=='identity':marker['run_id']='c'*32
    if damage=='revision':marker['source_revision']='c'*64
    if damage=='future':marker['requested_at']=stamp+3600
    (directory/'policy_handoff.json').write_text(json.dumps(marker),encoding='utf-8')
    status=ScreeningManager(tmp_path).status()
    assert (status.get('stop_reason')=='policy_update') is (damage is None)
    assert json.loads((directory/'status.json').read_text())==state
