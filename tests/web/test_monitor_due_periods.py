from copy import deepcopy
from datetime import timedelta

import pytest

from cl_app.services.signal_monitor import _signal
from tests.web.test_signal_monitor import setup, _seed, signal
from chanlun.screening.markets import closed_bar_cutoff


@pytest.mark.parametrize('failed',[False,True])
def test_one_minute_only_recheck_does_not_remove_a_five_minute_signal(setup,failed):
    service,manual,live,clock,cutoff=_seed(setup,[signal()])
    service.tick();live.rows=[{**signal(),'source_closed_at':cutoff}];live.finish(cutoff)
    service._consume_monitor()
    state=service._load();key='a:SH.600088';identity=_signal(signal())['id']
    previous=deepcopy(state['signals'][identity]);check=deepcopy(state['selection_checks'][key+':5m'])
    settings={**state['seed']['settings'],'frequencies':['1m','5m'],'symbols':[
        {'market':'a','code':'SH.600088','name':'sample','_monitor_periods':['1m']}]}
    live.start(settings)
    state.update(active_monitor_run=live.state['run_id'],active_seed=state['seed']['run_id'],
                 active_symbols=[key],active_consumed_symbols=[],active_cutoffs={key:cutoff+60})
    live.rows=[];live.finish(cutoff+60)
    if failed:live.state.update(status='failed',error='isolated one-minute failure')
    service._consume_monitor()
    assert state['signals'][identity]==previous
    assert state['selection_checks'][key+':5m']==check
    assert not any(e.get('frequency')=='5m' for e in state['errors'])
    assert not any(e['id']==identity and e['change']=='left' for e in state['events'])


def test_scheduler_omits_a_current_five_minute_period_but_keeps_new_one_minute_check(setup):
    service,manual,live,clock,cutoff=_seed(setup,[signal()])
    service.tick();live.rows=[signal()];live.finish(cutoff);service._consume_monitor()
    state=service._load();key='a:SH.600088';revision='same-algorithm'
    clock[0]=clock[0].replace(hour=14,minute=56,second=10)
    state['seed']['settings']['frequencies']=['1m','5m']
    state['monitor_frequencies']=['1m','5m']
    state.update(force=False,force_pending_symbols=[],pending_symbols_count=0,next_check_at=0)
    state['attempted_cutoffs'][key]=int(clock[0].timestamp())-120
    live.state.update(source_revision=revision,current_source_revision=revision)
    for frequency in ['1m','5m']:
        expected=closed_bar_cutoff('a','SH.600088',clock[0],frequency)
        state['selection_checks'][key+':'+frequency]={'revision':revision,'cutoff':expected-(60 if frequency=='1m' else 0),'error':False}
    service._schedule_monitor(clock[0],None)
    assert live.started[-1]['symbols'][0]['_monitor_periods']==['1m']
