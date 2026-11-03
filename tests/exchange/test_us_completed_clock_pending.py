"""Fresh incomplete windows wait; invalid or historical data remain errors."""
from copy import deepcopy
from threading import Event
from types import SimpleNamespace
import pickle

import pandas as pd
import pytest

from chanlun.exchange.errors import SourcePreparationPending, USmartAPIError
from chanlun.exchange.quote_capacity import QuoteConnectionCancelled, quote_request_scope
from chanlun.exchange.us_dual_history import DualUSHistory, USDualSourcePending

END = pd.Timestamp('2026-10-27T20:00:00+00:00')


def fixture(tmp_path, frequency='5m', *, age=10, lagging='longbridge'):
    step = int(frequency[:-1])
    calls, values = [], {}
    for provider in ('longbridge','usmart'):
        dates = pd.date_range(end=END, periods=3, freq=pd.Timedelta(minutes=step))
        if provider == lagging:
            dates -= pd.Timedelta(minutes=step)
        if provider == 'longbridge':
            dates -= pd.Timedelta(minutes=step)
        value = pd.DataFrame({'date':dates, 'code':'QQQ.US','frequency':frequency,
            'open':10.,'high':12.,'low':9.,'close':11.,'volume':100. if provider=='longbridge' else 200.})
        value.attrs = {'price_basis_provider':provider,'price_basis_revision':provider+'-basis',
                       'price_basis_adjustment':'forward'}
        values[provider] = value
    def source(provider):
        def load(*args, **kwargs):
            calls.append(provider)
            value = values[provider]
            if isinstance(value, Exception):
                raise value
            return deepcopy(value)
        return SimpleNamespace(klines=load, kline_time_label='start' if provider=='longbridge' else 'end')
    route = DualUSHistory(source('usmart'), lambda:source('longbridge'), root=tmp_path,
        primary=lagging, now=lambda:END.timestamp()+age)
    return route, values, calls


@pytest.mark.parametrize('frequency', ['1m','5m','30m'])
@pytest.mark.parametrize('provider', ['longbridge','usmart'])
def test_lagging_current_source_preserves_full_alternate_window(tmp_path, frequency, provider):
    route, values, calls = fixture(tmp_path, frequency, lagging=provider)
    actual = route.klines('QQQ.US', frequency, end_date=END.isoformat())
    alternate = 'usmart' if provider=='longbridge' else 'longbridge'
    assert calls == [provider,alternate]
    assert actual.attrs['price_basis_provider'] == alternate
    assert actual.attrs['_screening_us_route']['provider'] == alternate
    assert actual.volume.eq(200 if alternate=='usmart' else 100).all()
    assert actual.date.iloc[-1] == END


@pytest.mark.parametrize('frequency', ['1m','5m','30m'])
@pytest.mark.parametrize('position', ['just_closed','within_period','last_instant'])
def test_unavailable_alternate_waits_without_returning_old_primary(tmp_path, frequency, position):
    seconds = int(frequency[:-1])*60
    age = {'just_closed':0,'within_period':seconds/2,'last_instant':seconds-.01}[position]
    route, values, calls = fixture(tmp_path, frequency, age=age)
    values['usmart'] = USmartAPIError('native unavailable', code='fixture_error')
    with pytest.raises(USDualSourcePending) as caught:
        route.klines('QQQ.US', frequency, end_date=END.isoformat())
    assert calls == ['longbridge','usmart']
    assert caught.value.reasons['longbridge']['code'] == 'us_completed_clock_pending'
    assert caught.value.reasons['longbridge']['progress_token']
    assert caught.value.preparation['retry_after'] == 3
    assert pickle.loads(pickle.dumps(caught.value)).reasons == caught.value.reasons


@pytest.mark.parametrize('age', [-1,300,301,86400])
def test_noncurrent_or_expired_missing_bar_remains_explicit_failure(tmp_path, age):
    route, values, calls = fixture(tmp_path, age=age)
    values['usmart'] = ValueError('invalid other source')
    with pytest.raises(USmartAPIError) as caught:
        route.klines('QQQ.US','5m',end_date=END.isoformat())
    assert type(caught.value) is USmartAPIError
    assert caught.value.code == 'us_dual_sources_unavailable'


@pytest.mark.parametrize('damage', ['identity','geometry','nan','duplicate','basis','incomplete'])
def test_bad_data_is_not_softened_merely_because_tail_lags(tmp_path, damage):
    route, values, calls = fixture(tmp_path)
    value = values['longbridge']
    if damage=='identity': value['code']='OTHER.US'
    elif damage=='geometry': value.loc[0,'low']=13.
    elif damage=='nan': value.loc[0,'close']=float('nan')
    elif damage=='duplicate': value['date']=value.date.iloc[0]
    elif damage=='basis': value.attrs['price_basis_provider']='other'
    else: value.attrs['fetch_incomplete']=True
    values['usmart'] = ValueError('invalid other source')
    with pytest.raises(USmartAPIError) as caught:
        route.klines('QQQ.US','5m',end_date=END.isoformat())
    assert type(caught.value) is USmartAPIError


def test_clock_ahead_of_market_cutoff_remains_invalid(tmp_path):
    route, values, calls = fixture(tmp_path, age=30)
    values['longbridge']['date'] += pd.Timedelta(minutes=5,seconds=15)
    values['usmart'] = ValueError('invalid other source')
    with pytest.raises(USmartAPIError) as caught:
        route.klines('QQQ.US','5m',end_date=(END+pd.Timedelta(seconds=30)).isoformat())
    assert type(caught.value) is USmartAPIError


def test_pinned_wait_is_serializable_and_does_not_open_alternate(tmp_path):
    route, values, calls = fixture(tmp_path)
    with pytest.raises(USDualSourcePending):
        route.klines('QQQ.US','5m',end_date=END.isoformat(),args={'_history_provider':'longbridge'})
    assert calls == ['longbridge']
    with pytest.raises(SourcePreparationPending) as caught:
        route._read('longbridge','QQQ.US','5m',None,END.isoformat(),{})
    pending = caught.value
    assert pending.preparation['observed_cutoff'] == int(END.timestamp())-300
    assert pending.preparation['expected_cutoff'] == int(END.timestamp())
    assert pickle.loads(pickle.dumps(pending)).preparation == pending.preparation


def test_next_complete_response_recovers_and_cancel_remains_dominant(tmp_path):
    route, values, calls = fixture(tmp_path)
    values['usmart'] = ValueError('invalid other source')
    with pytest.raises(USDualSourcePending):
        route.klines('QQQ.US','5m',end_date=END.isoformat())
    values['longbridge']['date'] += pd.Timedelta(minutes=5)
    actual = route.klines('QQQ.US','5m',end_date=END.isoformat())
    assert actual.date.iloc[-1] == END and actual.attrs['price_basis_provider']=='longbridge'
    cancel = Event()
    def cancelled(*args, **kwargs):
        cancel.set()
        raise SourcePreparationPending({},code='us_completed_clock_pending')
    route.longbridge.klines = cancelled
    before = len(calls)
    with quote_request_scope(cancel), pytest.raises(QuoteConnectionCancelled):
        route.klines('QQQ.US','5m',end_date=END.isoformat())
    assert len(calls) == before
