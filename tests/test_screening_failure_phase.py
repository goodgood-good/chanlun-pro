"""Data acquisition failures must not be reported as broken Chan algorithms."""
import pytest

from chanlun.exchange.errors import USmartAPIError, USmartHistoryPending, LongbridgeConnectionLimitError
from chanlun.screening import runner
from chanlun.screening.errors import MarketDataContractError


@pytest.mark.parametrize('error,reason',[
    (USmartAPIError('HTTP 503',status_code=503),'DATA_SOURCE_UNAVAILABLE'),
    (LongbridgeConnectionLimitError(),'DATA_SOURCE_UNAVAILABLE'),
    (TimeoutError('feed timed out'),'DATA_SOURCE_UNAVAILABLE'),
    (USmartHistoryPending(),'DATA_SOURCE_PREPARING'),
    (MarketDataContractError('SESSION_CALENDAR_UNAVAILABLE','fixture'),'SESSION_CALENDAR_UNAVAILABLE'),
])
def test_fetch_errors_remain_distinct_from_structure_failure(tmp_path,monkeypatch,error,reason):
    def fetch(*_):
        raise error
    monkeypatch.setattr(runner,'fetch_frame',fetch)
    result=runner.scan_symbol({'code':'MSFT.US','name':'fixture','market':'us'},
                              {'frequencies':['1m'],'strategy':'direct_structural'}, {'1m':{}},tmp_path)
    row=result['rows'][0]
    assert row['data_errors']==[reason]
    assert row['selected']==row['observations']==[]


def test_history_progress_is_projected_without_publishing_candidates(tmp_path,monkeypatch):
    pending=USmartHistoryPending()
    pending.preparation={'progress_token':'verified-1200', 'available_rows':1200,
                         'required_rows':15000, 'private_source_payload':'not public'}
    def fetch(*_):raise pending
    monkeypatch.setattr(runner,'fetch_frame',fetch)
    row=runner.scan_symbol({'code':'MSFT.US','name':'fixture','market':'us'},
        {'frequencies':['5m'],'strategy':'direct_structural'}, {'5m':{}},tmp_path)['rows'][0]
    assert row['data_errors']==['DATA_SOURCE_PREPARING']
    assert row['source_preparation']=={'progress_token':'verified-1200','available_rows':1200,'required_rows':15000}
    assert not row['selected']


def test_compute_exception_still_reports_engine_error(tmp_path,monkeypatch):
    monkeypatch.setattr(runner,'fetch_frame',lambda *_: object())
    def quality(*_):
        raise RuntimeError('broken implementation')
    monkeypatch.setattr(runner,'frame_quality',quality)
    row=runner.scan_symbol({'code':'MSFT.US','name':'fixture','market':'us'},
                           {'frequencies':['1m'],'strategy':'direct_structural'}, {'1m':{}},tmp_path)['rows'][0]
    assert row['data_errors']==['ENGINE_ERROR']
