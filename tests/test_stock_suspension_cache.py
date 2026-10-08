"""Malformed stock-halt records cannot pin later checks to a poisoned cache."""
from datetime import datetime
from unittest.mock import Mock
import json

import pytest
import requests

from chanlun.screening import sessions


@pytest.fixture
def request_data(monkeypatch):
    start = int(datetime(2026, 1, 13, 9, 30, tzinfo=sessions.CN).timestamp())
    cutoff = int(datetime(2026, 2, 27, 15, tzinfo=sessions.CN).timestamp())
    row = {'SECUCODE': '601615.SH', 'SUSPEND_START_TIME': '2026-01-13 09:30:00',
           'SUSPEND_END_TIME': '2026-01-22 15:00:00'}
    fetch = Mock(return_value=[row])
    monkeypatch.setattr(sessions, '_fetch_records', fetch)
    monkeypatch.setattr(sessions.time, 'time', lambda: cutoff + 100)
    return start, cutoff, row, fetch


def cached(tmp_path, cutoff, records, *, age=0):
    data = {'schema': sessions.SCHEMA, 'source_url': sessions.SOURCE_URL,
            'query_from': '2026-01-13', 'fetched_at': cutoff + 100 - age, 'records': records}
    path = tmp_path/'suspensions.json'
    path.write_text(json.dumps({**data, 'sha256': sessions._digest(data)}), encoding='utf-8')
    return path


@pytest.mark.parametrize('changed', [
    {'SUSPEND_START_TIME': 'invalid-date'},
    {'SUSPEND_END_TIME': 'invalid-date'},
    {'SUSPEND_END_TIME': '2026-01-12 15:00:00'},
])
def test_invalid_fetched_interval_is_never_published(tmp_path, request_data, changed):
    start, cutoff, row, fetch = request_data
    fetch.return_value = [{**row, **changed}]
    with pytest.raises(ValueError):
        sessions.suspension_evidence('SH.601615', start, cutoff, cache_root=tmp_path)
    assert not (tmp_path/'suspensions.json').exists()
    fetch.return_value = [row]
    assert sessions.suspension_evidence('SH.601615', start, cutoff, cache_root=tmp_path)['intervals']
    assert fetch.call_count == 2


def test_bad_cached_symbol_is_refreshed_without_blocking_unrelated_stocks(tmp_path, request_data):
    start, cutoff, row, fetch = request_data
    other = {**row, 'SECUCODE': '000001.SZ'}
    cached(tmp_path, cutoff, [{**row, 'SUSPEND_START_TIME': 'invalid-date'}, other])
    assert sessions.suspension_evidence('SZ.000001', start, cutoff, cache_root=tmp_path)['intervals']
    fetch.assert_not_called()
    fetch.return_value = [row, other]
    repaired = sessions.suspension_evidence('SH.601615', start, cutoff, cache_root=tmp_path)
    assert repaired['intervals'] and fetch.call_count == 1
    assert sessions.suspension_evidence('SH.601615', start, cutoff, cache_root=tmp_path) == repaired
    assert fetch.call_count == 1


def test_invalid_cache_and_failed_refresh_remain_unverified(tmp_path, request_data):
    start, cutoff, row, fetch = request_data
    path = cached(tmp_path, cutoff, [{**row, 'SUSPEND_END_TIME': 'invalid-date'}])
    original = path.read_bytes()
    fetch.side_effect = requests.ConnectionError('isolated source outage')
    with pytest.raises(requests.ConnectionError):
        sessions.suspension_evidence('SH.601615', start, cutoff, cache_root=tmp_path)
    assert path.read_bytes() == original


def test_invalid_refresh_cannot_replace_an_existing_shared_cache(tmp_path, request_data):
    start, cutoff, row, fetch = request_data
    path = cached(tmp_path, cutoff, [row], age=7200)
    original = path.read_bytes()
    fetch.return_value = [{**row, 'SUSPEND_START_TIME': 'invalid-date'}]
    with pytest.raises(ValueError):
        sessions.suspension_evidence('SH.601615', start, cutoff, cache_root=tmp_path)
    assert path.read_bytes() == original


def test_expired_cache_cannot_stand_in_for_an_unavailable_source(tmp_path, request_data):
    start, cutoff, row, fetch = request_data
    cached(tmp_path, cutoff, [row], age=7200)
    fetch.side_effect = requests.ConnectionError('isolated source outage')
    with pytest.raises(requests.ConnectionError):
        sessions.suspension_evidence('SH.601615', start, cutoff, cache_root=tmp_path)


def test_verified_interval_survives_optional_cache_failure(tmp_path, monkeypatch, request_data):
    start, cutoff, row, fetch = request_data
    def denied(*args, **kwargs):
        raise PermissionError('isolated optional cache denial')
    monkeypatch.setattr(sessions.os, 'replace', denied)
    result = sessions.suspension_evidence('SH.601615', start, cutoff, cache_root=tmp_path)
    assert result['intervals'] == [[start, int(datetime(2026, 1, 22, 15, tzinfo=sessions.CN).timestamp())]]
    assert not (tmp_path/'suspensions.json').exists()
