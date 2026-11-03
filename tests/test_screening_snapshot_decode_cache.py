from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import gzip
import hashlib
import json

import pandas as pd
import pytest

from chanlun.screening import evidence
from chanlun.screening.runner import _save_evidence
from tests.test_screening_cache_inputs import frame


def raw_snapshot(**changes):
    value = {'symbol': 'SH.600000', 'source_frequency': '5m', 'levels': [],
             'metadata': {'provider': 'qmt', 'values': [1, 2]}}
    value.update(changes)
    return gzip.compress(json.dumps(value).encode())


@pytest.fixture(autouse=True)
def empty_cache():
    evidence._SNAPSHOT_DECODE_CACHE.clear()
    yield
    evidence._SNAPSHOT_DECODE_CACHE.clear()


def test_cache_reuses_validated_bytes_but_returns_independent_nested_objects(monkeypatch):
    raw = raw_snapshot()
    first = evidence._decode_snapshot(raw)
    expected = deepcopy(first)
    first['metadata']['values'].append('caller mutation')
    monkeypatch.setattr(evidence, '_parse_snapshot', lambda _raw: pytest.fail('same bytes decoded again'))
    second = evidence._decode_snapshot(raw)
    second['metadata']['provider'] = 'mutated provider'
    assert evidence._decode_snapshot(raw) == expected


def test_content_key_cannot_mix_different_symbols_frequencies_or_providers():
    cases = [raw_snapshot(), raw_snapshot(symbol='SZ.000001'),
             raw_snapshot(source_frequency='1m'),
             raw_snapshot(metadata={'provider': 'longbridge', 'values': [1, 2]})]
    for raw in cases * 2:
        assert evidence._decode_snapshot(raw) == json.loads(gzip.decompress(raw))


@pytest.mark.parametrize('raw', [b'not gzip', gzip.compress(b'[]'),
    gzip.compress(b'{"symbol":"x","source_frequency":"5m","levels":[{"points":[{}]}]}')])
def test_invalid_bytes_remain_rejected_after_valid_cache_warmup(raw):
    evidence._decode_snapshot(raw_snapshot())
    for _ in range(2):
        with pytest.raises(ValueError):
            evidence._decode_snapshot(raw)


def test_byte_and_entry_limits_evict_and_oversized_values_are_not_retained():
    cache = evidence._SnapshotDecodeCache(max_bytes=40, max_entries=2)
    calls = []
    def parse(raw):
        calls.append(raw)
        return raw.decode()
    for raw in (b'a', b'b', b'a', b'c'):
        assert cache.decode(raw, parse) == raw.decode()
    assert calls == [b'a', b'b', b'c']
    cache.decode(b'b', parse)
    assert calls[-1] == b'b'  # Least recently used entry had been evicted.
    for _ in range(2):
        cache.decode(b'x' * 100, parse)
    assert calls[-2:] == [b'x' * 100, b'x' * 100]
    assert cache._bytes <= 40 and len(cache._entries) <= 2
    cache = evidence._SnapshotDecodeCache(max_bytes=1000, max_entries=1)
    for raw in (b'a', b'b', b'a'):
        cache.decode(raw, parse)
    assert calls[-3:] == [b'a', b'b', b'a']


def test_concurrent_callers_cannot_change_each_others_snapshot():
    raw = raw_snapshot()
    evidence._decode_snapshot(raw)
    def read(value):
        snapshot = evidence._decode_snapshot(raw)
        snapshot['metadata']['values'].append(value)
        return snapshot
    with ThreadPoolExecutor(max_workers=8) as pool:
        copies = list(pool.map(read, range(32)))
    assert [item['metadata']['values'] for item in copies] == [[1, 2, i] for i in range(32)]
    assert evidence._decode_snapshot(raw)['metadata']['values'] == [1, 2]


def test_cache_serialization_failure_does_not_reject_valid_evidence(monkeypatch):
    raw = raw_snapshot()
    def unavailable(*args, **kwargs):
        raise RecursionError('cannot cache this shape')
    monkeypatch.setattr(evidence.pickle, 'dumps', unavailable)
    assert evidence._decode_snapshot(raw) == json.loads(gzip.decompress(raw))
    assert evidence._SNAPSHOT_DECODE_CACHE._bytes == 0


def save_valid(directory, *, provider='qmt', snapshot_changes=None):
    data = frame()
    data.attrs['price_basis_provider'] = provider
    snapshot = json.loads(gzip.decompress(raw_snapshot()))
    snapshot['source_closed_at'] = int(data.date.iloc[-1].timestamp())
    snapshot.update(snapshot_changes or {})
    manifest = _save_evidence(directory, 'SH.600000', '5m', data, snapshot)
    return data, snapshot, manifest


def test_load_still_hashes_inputs_and_returns_independent_dataframes_and_snapshots(tmp_path):
    data, snapshot, manifest = save_valid(tmp_path)
    first_frame, first_snapshot, _ = evidence.load_evidence(tmp_path, 'SH.600000', '5m', manifest)
    first_frame.loc[0, 'close'] = -999
    first_frame.attrs['price_basis_provider'] = 'other'
    first_snapshot['metadata']['values'].append('changed')
    second_frame, second_snapshot, health = evidence.load_evidence(tmp_path, 'SH.600000', '5m', manifest)
    pd.testing.assert_frame_equal(second_frame, data)
    assert second_frame.attrs == data.attrs and second_snapshot == snapshot
    with pytest.raises(ValueError, match='不一致'):
        evidence.load_evidence(tmp_path, 'SH.600000', '5m', {**manifest, 'input_fingerprint': 'bad'})
    assert health['input_fingerprint'] == manifest['input_fingerprint']


def test_actual_snapshot_bytes_are_checked_even_if_polling_health_is_stale(tmp_path, monkeypatch):
    _, _, manifest = save_valid(tmp_path)
    evidence.load_evidence(tmp_path, 'SH.600000', '5m', manifest)
    _, path = evidence.evidence_paths(tmp_path, 'SH.600000', '5m')
    raw = bytearray(path.read_bytes());raw[-1] ^= 1;path.write_bytes(raw)
    monkeypatch.setattr(evidence, 'evidence_health', lambda *args: {'complete': True, 'verified': True})
    with pytest.raises(ValueError, match='读取时发生变化'):
        evidence.load_evidence(tmp_path, 'SH.600000', '5m', manifest)


@pytest.mark.parametrize('change', [{'symbol': 'SZ.000001'}, {'source_frequency': '1m'}, {'source_closed_at': 1}])
def test_cached_decoding_does_not_bypass_identity_or_clock_checks(tmp_path, change):
    _, _, manifest = save_valid(tmp_path)
    evidence.load_evidence(tmp_path, 'SH.600000', '5m', manifest)
    _, path = evidence.evidence_paths(tmp_path, 'SH.600000', '5m')
    snapshot = evidence._decode_snapshot(path.read_bytes());snapshot.update(change)
    raw = gzip.compress(json.dumps(snapshot).encode());path.write_bytes(raw)
    altered = {**manifest, 'snapshot_bytes': len(raw), 'snapshot_sha256': hashlib.sha256(raw).hexdigest()}
    evidence._decode_snapshot(raw)  # Test the cached path, not just a parse miss.
    with pytest.raises(ValueError, match='不一致'):
        evidence.load_evidence(tmp_path, 'SH.600000', '5m', altered)


def test_same_geometry_does_not_substitute_a_different_provider_input(tmp_path):
    _, _, first = save_valid(tmp_path / 'first', provider='longbridge')
    _, _, second = save_valid(tmp_path / 'second', provider='usmart')
    evidence.load_evidence(tmp_path / 'first', 'SH.600000', '5m', first)
    data, _, health = evidence.load_evidence(tmp_path / 'second', 'SH.600000', '5m', second)
    assert data.attrs['price_basis_provider'] == 'usmart'
    assert first['input_fingerprint'] != health['input_fingerprint']
    with pytest.raises(ValueError, match='不一致'):
        evidence.load_evidence(tmp_path / 'second', 'SH.600000', '5m',
                               {**second, 'input_fingerprint': first['input_fingerprint']})
