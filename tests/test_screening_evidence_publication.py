from copy import deepcopy
import gzip
import json
import os

import pandas as pd
import pytest

from chanlun.screening import evidence_publication as publication
from chanlun.screening.evidence import evidence_health, evidence_paths
from chanlun.screening.runner import _save_evidence
from tests.test_screening_cache_inputs import frame


@pytest.fixture(autouse=True)
def clear_cache():
    publication.clear_publication_cache()
    yield
    publication.clear_publication_cache()


def save(directory, data=None, snapshot=None):
    return _save_evidence(directory, 'SH.600000', '5m', frame() if data is None else data,
                          {'symbol': 'SH.600000', 'source_frequency': '5m', 'levels': [], 'label': '证据'}
                          if snapshot is None else snapshot)


def test_identical_frozen_bytes_are_reused_without_reserializing_and_survive_old_run_removal(tmp_path, monkeypatch):
    first, second = tmp_path / 'first', tmp_path / 'second'
    original = save(first)
    monkeypatch.setattr(pd.DataFrame, 'to_parquet', lambda *_a, **_k: pytest.fail('unchanged evidence reserialized'))
    result = save(second)
    assert result == original
    for previous, current in zip(evidence_paths(first, 'SH.600000', '5m'), evidence_paths(second, 'SH.600000', '5m')):
        assert os.path.samefile(previous, current)
        previous.unlink()
        assert current.is_file()
    assert evidence_health(second, 'SH.600000', '5m', result) == {'complete': True, 'verified': True}


def test_identical_publication_to_the_same_run_keeps_valid_evidence(tmp_path):
    first = save(tmp_path)
    assert save(tmp_path) == first
    assert evidence_health(tmp_path, 'SH.600000', '5m', first)['verified']


def test_trade_confirmation_and_short_term_evidence_cannot_overwrite_each_other(tmp_path):
    from chanlun.screening.evidence import load_evidence, evidence_version
    data = frame()
    data['date'] = pd.date_range('2026-09-01 09:31', periods=4, freq='1min', tz='Asia/Shanghai')
    source = {'symbol': 'SH.600000', 'source_frequency': '1m', 'levels': [],
              'source_closed_at': int(data.date.iloc[-1].timestamp())}
    lower = {**source, 'screening_interval_signals': [{'proof': 'specific parent interval'}]}
    frozen = _save_evidence(tmp_path, 'SH.600000', '1m', data, lower, namespace='trade_5m')
    frozen_paths = evidence_paths(tmp_path, 'SH.600000', '1m', namespace='trade_5m')
    before = [path.read_bytes() for path in frozen_paths]
    short = _save_evidence(tmp_path, 'SH.600000', '1m', data, source)
    assert [path.read_bytes() for path in frozen_paths] == before
    assert evidence_health(tmp_path, 'SH.600000', '1m', frozen)['verified']
    assert evidence_health(tmp_path, 'SH.600000', '1m', short)['verified']
    assert load_evidence(tmp_path, 'SH.600000', '1m', frozen)[1] == lower
    assert load_evidence(tmp_path, 'SH.600000', '1m', short)[1] == source
    token = evidence_version(tmp_path)
    frozen_paths[1].unlink()
    assert evidence_version(tmp_path) != token
    assert not evidence_health(tmp_path, 'SH.600000', '1m', frozen)['complete']
    assert evidence_health(tmp_path, 'SH.600000', '1m', short)['verified']


@pytest.mark.parametrize('namespace', ['../elsewhere', 'unknown', '/absolute'])
def test_unknown_evidence_namespaces_are_rejected(tmp_path, namespace):
    with pytest.raises(ValueError):
        evidence_paths(tmp_path, 'SH.600000', '1m', namespace=namespace)


@pytest.mark.parametrize('changed', ['old_bar', 'snapshot', 'diagnostic_metadata', 'price_basis'])
def test_every_frozen_input_or_snapshot_change_gets_new_evidence(tmp_path, monkeypatch, changed):
    data = frame()
    snapshot = {'symbol': 'SH.600000', 'source_frequency': '5m', 'levels': []}
    save(tmp_path / 'first', data, snapshot)
    data = data.copy(deep=True)
    snapshot = deepcopy(snapshot)
    if changed == 'old_bar':
        data.loc[data.index[0], 'high'] += 1
    elif changed == 'snapshot':
        snapshot['new_result'] = True
    elif changed == 'diagnostic_metadata':
        data.attrs['_screening_new_diagnostic'] = 'must remain in frozen input'
    else:
        data.attrs['price_basis_revision'] = 'new basis'
    original = pd.DataFrame.to_parquet
    calls = []
    def recording(self, *args, **kwargs):
        calls.append(True)
        return original(self, *args, **kwargs)
    monkeypatch.setattr(pd.DataFrame, 'to_parquet', recording)
    result = save(tmp_path / 'second', data, snapshot)
    assert calls == [True]
    parquet, packed = evidence_paths(tmp_path / 'second', 'SH.600000', '5m')
    assert pd.read_parquet(parquet).attrs == data.attrs
    assert json.loads(gzip.decompress(packed.read_bytes())) == snapshot
    assert evidence_health(tmp_path / 'second', 'SH.600000', '5m', result)['verified']


@pytest.mark.parametrize('damage', ['deleted', 'parquet', 'snapshot', 'changed_manifest'])
def test_missing_or_corrupted_previous_bytes_rebuild_without_trusting_returned_metadata(tmp_path, damage):
    first = tmp_path / 'first'
    metadata = save(first)
    parquet, packed = evidence_paths(first, 'SH.600000', '5m')
    if damage == 'deleted':
        parquet.unlink()
    elif damage == 'parquet':
        parquet.write_bytes(b'broken')
    elif damage == 'snapshot':
        packed.write_bytes(b'broken')
    else:
        metadata['parquet_sha256'] = '0' * 64
    result = save(tmp_path / 'second')
    assert result['parquet_sha256'] != '0' * 64
    assert evidence_health(tmp_path / 'second', 'SH.600000', '5m', result)['verified']
    assert not list((tmp_path / 'second' / 'evidence').glob('*.tmp'))


def test_link_unavailable_falls_back_to_verified_copy_without_serialization(tmp_path, monkeypatch):
    first, second = tmp_path / 'first', tmp_path / 'second'
    result = save(first)
    monkeypatch.setattr(publication.os, 'link', lambda *_a: (_ for _ in ()).throw(OSError('unsupported')))
    monkeypatch.setattr(pd.DataFrame, 'to_parquet', lambda *_a, **_k: pytest.fail('should copy existing bytes'))
    assert save(second) == result
    assert not os.path.samefile(evidence_paths(first, 'SH.600000', '5m')[0], evidence_paths(second, 'SH.600000', '5m')[0])
    assert evidence_health(second, 'SH.600000', '5m', result)['verified']


def test_optional_index_has_a_hard_limit_and_does_not_own_run_retention(tmp_path, monkeypatch):
    monkeypatch.setattr(publication, 'MAX_REFERENCES', 2)
    for number in range(4):
        snapshot = {'symbol': f'SH.60000{number}', 'source_frequency': '5m', 'levels': []}
        _save_evidence(tmp_path / 'run', snapshot['symbol'], '5m', frame(), snapshot)
    assert len(publication._references) == 2
    assert len(list((tmp_path / 'run' / 'evidence').glob('*.parquet'))) == 4


def test_storage_namespaces_do_not_borrow_one_another_evidence(tmp_path, monkeypatch):
    save(tmp_path / 'one' / 'first')
    original = pd.DataFrame.to_parquet
    calls = []
    def recording(self, *args, **kwargs):
        calls.append(True)
        return original(self, *args, **kwargs)
    monkeypatch.setattr(pd.DataFrame, 'to_parquet', recording)
    save(tmp_path / 'two' / 'second')
    assert calls == [True]


def test_replacing_a_shared_run_preserves_the_other_runs_original_bytes(tmp_path):
    first, second = tmp_path / 'first', tmp_path / 'second'
    original = save(first)
    assert save(second) == original
    shared = evidence_paths(second, 'SH.600000', '5m')
    original_bytes = [path.read_bytes() for path in shared]
    changed = frame()
    changed.loc[changed.index[0], 'high'] += 1
    replacement = save(first, changed)
    assert replacement['parquet_sha256'] != original['parquet_sha256']
    assert [path.read_bytes() for path in shared] == original_bytes
    assert not os.path.samefile(evidence_paths(first, 'SH.600000', '5m')[0], shared[0])
    assert evidence_health(first, 'SH.600000', '5m', replacement)['verified']
    assert evidence_health(second, 'SH.600000', '5m', original)['verified']


@pytest.mark.parametrize('probe', ['is_symlink', 'is_file'])
def test_inaccessible_optional_cache_metadata_rebuilds_a_healthy_new_run(tmp_path, monkeypatch, probe):
    from pathlib import Path
    first, second = tmp_path / 'first', tmp_path / 'second'
    save(first)
    old_parquet, _ = evidence_paths(first, 'SH.600000', '5m')
    original = getattr(Path, probe)
    def denied(self):
        if self == old_parquet:
            raise PermissionError('old retained evidence metadata is temporarily inaccessible')
        return original(self)
    monkeypatch.setattr(Path, probe, denied)
    manifest = save(second)
    assert evidence_health(second, 'SH.600000', '5m', manifest)['verified']
    assert not list((second / 'evidence').glob('*.tmp'))
