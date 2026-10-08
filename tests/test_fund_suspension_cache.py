"""Optional cache persistence cannot invalidate verified fund-halt evidence."""
from datetime import datetime
from pathlib import Path
from unittest.mock import Mock
import json

import pytest
import requests

from chanlun.screening import fund_suspensions as funds
from chanlun.screening.sessions import CN, SCHEMA, _digest


@pytest.fixture
def request_data(monkeypatch):
    start=int(datetime(2026,4,9,9,30,tzinfo=CN).timestamp())
    cutoff=int(datetime(2026,4,9,15,tzinfo=CN).timestamp())
    row={'secCode':'513100','dateSource':'2','startStopType':'LSTP','stopTime':'915',
         'startStopDate':'20260409','endStopDate':'20260409'}
    fetch=Mock(return_value=[row])
    monkeypatch.setattr(funds,'_fetch',fetch)
    monkeypatch.setattr(funds.time,'time',lambda:cutoff+100)
    return start,cutoff,row,fetch


@pytest.mark.parametrize('fault',['mkdir','write','replace','cleanup'])
def test_valid_records_survive_optional_cache_failures(tmp_path,monkeypatch,request_data,fault):
    start,cutoff,row,fetch=request_data
    cache=tmp_path/'cache'
    def denied(*args,**kwargs):raise PermissionError('isolated optional cache denial')
    if fault=='mkdir':
        original=Path.mkdir
        monkeypatch.setattr(Path,'mkdir',lambda path,*args,**kwargs:denied() if path==cache else original(path,*args,**kwargs))
    elif fault=='write':
        original=Path.write_text
        monkeypatch.setattr(Path,'write_text',lambda path,*args,**kwargs:denied() if path.suffix=='.tmp' else original(path,*args,**kwargs))
    else:
        monkeypatch.setattr(funds.os,'replace',denied)
        if fault=='cleanup':
            original=Path.unlink
            monkeypatch.setattr(Path,'unlink',lambda path,*args,**kwargs:denied() if path.suffix=='.tmp' else original(path,*args,**kwargs))
    result=funds.suspension_evidence('SH.513100',start,cutoff,cache_root=cache)
    assert result['intervals']==funds.intervals_from_records('SH.513100',[row],start,cutoff)
    assert result['source_url']==funds.SOURCE_URL and result['checked_through']==cutoff
    assert fetch.call_count==1


def test_invalid_fetched_records_never_become_a_reusable_cache(tmp_path,request_data):
    start,cutoff,row,fetch=request_data
    fetch.return_value=[{**row,'secCode':'510300'}]
    with pytest.raises(ValueError,match='identity'):
        funds.suspension_evidence('SH.513100',start,cutoff,cache_root=tmp_path)
    assert not (tmp_path/'fund_suspensions_SH.513100.json').exists()
    fetch.return_value=[row]
    assert funds.suspension_evidence('SH.513100',start,cutoff,cache_root=tmp_path)['intervals']
    assert fetch.call_count==2


def test_bad_legacy_record_is_refetched_instead_of_poisoning_later_checks(tmp_path,request_data):
    start,cutoff,row,fetch=request_data
    data={'schema':SCHEMA,'source_url':funds.SOURCE_URL,'symbol':'SH.513100',
          'query_from':'2026-04-09','query_through':'2026-04-09','fetched_at':cutoff+100,
          'records':[{**row,'secCode':'510300'}]}
    path=tmp_path/'fund_suspensions_SH.513100.json'
    path.write_text(json.dumps({**data,'sha256':_digest(data)}),encoding='utf-8')
    result=funds.suspension_evidence('SH.513100',start,cutoff,cache_root=tmp_path)
    assert result['intervals'] and fetch.call_count==1
    again=funds.suspension_evidence('SH.513100',start,cutoff,cache_root=tmp_path)
    assert again==result and fetch.call_count==1


def test_expired_evidence_does_not_hide_an_unavailable_feed(tmp_path,request_data):
    start,cutoff,row,fetch=request_data
    data={'schema':SCHEMA,'source_url':funds.SOURCE_URL,'symbol':'SH.513100',
          'query_from':'2026-04-09','query_through':'2026-04-09','fetched_at':cutoff-7200,'records':[row]}
    path=tmp_path/'fund_suspensions_SH.513100.json'
    original=json.dumps({**data,'sha256':_digest(data)})
    path.write_text(original,encoding='utf-8')
    fetch.side_effect=requests.ConnectionError('isolated source unavailable')
    with pytest.raises(requests.ConnectionError):
        funds.suspension_evidence('SH.513100',start,cutoff,cache_root=tmp_path)
    assert path.read_text(encoding='utf-8')==original


def test_invalid_cached_record_and_failed_refresh_remain_unverified(tmp_path,request_data):
    start,cutoff,row,fetch=request_data
    data={'schema':SCHEMA,'source_url':funds.SOURCE_URL,'symbol':'SH.513100',
          'query_from':'2026-04-09','query_through':'2026-04-09','fetched_at':cutoff+100,
          'records':[{**row,'secCode':'510300'}]}
    path=tmp_path/'fund_suspensions_SH.513100.json'
    path.write_text(json.dumps({**data,'sha256':_digest(data)}),encoding='utf-8')
    fetch.side_effect=requests.ConnectionError('isolated refresh failed')
    with pytest.raises(requests.ConnectionError):
        funds.suspension_evidence('SH.513100',start,cutoff,cache_root=tmp_path)
