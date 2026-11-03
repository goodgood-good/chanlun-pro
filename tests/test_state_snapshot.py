import json

import pytest

from chanlun.persistence.state_snapshot import compact_state,expand_state,SCHEMA
from chanlun.screening.state_io import read_state_json
from chanlun.screening.runner import write_json


def large_state():
    return {'enabled':True,'settings':{},'seed':None,'events':[
        {'id':str(i),'name':'区间确认','price':12.34,'facts':list(range(100))} for i in range(1500)]}


def test_large_state_round_trip_and_small_legacy_files(tmp_path):
    value=large_state();packed=compact_state(value)
    assert packed['schema']==SCHEMA
    assert len(json.dumps(packed))<len(json.dumps(value))//4
    write_json(tmp_path/'state.json',packed,durable=True)
    assert read_state_json(tmp_path/'state.json')==value
    assert compact_state({'enabled':True})=={'enabled':True}
    assert expand_state({'legacy':True})=={'legacy':True}


@pytest.mark.parametrize('change',[{'sha256':'bad'},{'bytes':1},{'payload':'not base64'},
                                   {'bytes':2**40},{'extra':True}])
def test_corrupt_compact_state_is_rejected(change):
    with pytest.raises(ValueError):expand_state({**compact_state(large_state()),**change})


def test_nonfinite_values_are_not_silently_replaced():
    with pytest.raises(ValueError):compact_state({'event':{'price':float('nan')}})


def test_large_monitor_state_can_restore_from_its_independent_backup(tmp_path):
    from cl_app.services.signal_monitor import SignalMonitor
    state={**SignalMonitor(tmp_path/'defaults')._load(),'enabled':True,'events':large_state()['events']}
    write_json(tmp_path/'state.json',compact_state(state))
    write_json(tmp_path/'state.backup.json',compact_state(state))
    (tmp_path/'state.json').write_text('{"schema":"chanlun-state-snapshot-v1","payload":"broken"}')
    service=SignalMonitor(tmp_path)
    assert service._load()['enabled'] is True
    assert len(service._load()['events'])==1500
    assert read_state_json(tmp_path/'state.json')['enabled'] is True
    assert list(tmp_path.glob('state.corrupt.*.json'))
