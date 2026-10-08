"""Failed chart-cache reads cannot delete another writer's replacement."""

import builtins
from pathlib import Path
import pickle

import pytest

from chanlun.file_db_mixins import chart_data
from chanlun.persistence.file_db import FileCacheDB


@pytest.fixture
def saved_chart(tmp_path):
    storage = FileCacheDB.__new__(FileCacheDB)
    storage.chart_cache_path = tmp_path
    path = storage._chart_cache_path_for("recorded-chart")
    entry = {"data": {"t": [100, 200], "c": [10., 11.]}, "min_time": 100,
             "max_time": 200, "validated_at": 300.0, "is_full_snapshot": True}
    storage._atomic_write_pickle_blocking(path, entry)
    return storage, path, entry


def test_temporary_open_error_does_not_delete_the_valid_chart(saved_chart, monkeypatch):
    storage, path, entry = saved_chart
    before = path.read_bytes()
    with monkeypatch.context() as patch:
        patch.setattr(chart_data, "open", lambda *_: (_ for _ in ()).throw(PermissionError("read occupied")), raising=False)
        assert storage.get_chart_cache("recorded-chart") is None
    assert path.is_file() and path.read_bytes() == before
    assert storage.get_chart_cache("recorded-chart") == entry


@pytest.mark.parametrize("old_bytes", [b"not pickle data", pickle.dumps(["old unsupported root"])])
def test_late_read_failure_preserves_an_atomic_replacement(saved_chart, monkeypatch, old_bytes):
    storage, path, entry = saved_chart
    path.write_bytes(old_bytes)
    fresh = {**entry, "validated_at": 400.0, "data": {"t": [100, 200], "c": [10., 12.]}}

    class ReplacedAfterRead:
        def __enter__(self):
            self.stream = builtins.open(path, "rb")
            return self.stream

        def __exit__(self, *error):
            self.stream.close()
            storage._atomic_write_pickle_blocking(path, fresh)
            return False

    with monkeypatch.context() as patch:
        patch.setattr(chart_data, "open", lambda *_: ReplacedAfterRead(), raising=False)
        assert storage.get_chart_cache("recorded-chart") is None
    assert path.is_file(), "the old reader removed the newly published chart"
    assert storage.get_chart_cache("recorded-chart") == fresh


def test_chart_cache_metadata_error_is_a_non_destructive_miss(saved_chart, monkeypatch):
    storage, path, entry = saved_chart
    original = Path.is_file

    def denied(target):
        if target == path:
            raise PermissionError("metadata occupied")
        return original(target)

    with monkeypatch.context() as patch:
        patch.setattr(Path, "is_file", denied)
        assert storage.get_chart_cache("recorded-chart") is None
    assert storage.get_chart_cache("recorded-chart") == entry


_executed = []


def _forbidden_payload():
    _executed.append(True)
    return {}


class UnsafePayload:
    def __reduce__(self):
        return _forbidden_payload, ()


def test_unsafe_pickle_remains_rejected_without_running_its_function(saved_chart):
    storage, path, entry = saved_chart
    _executed.clear()
    packed = pickle.dumps(UnsafePayload())
    path.write_bytes(packed)
    assert storage.get_chart_cache("recorded-chart") is None
    assert _executed == []
    assert path.read_bytes() == packed
    storage._atomic_write_pickle_blocking(path, entry)
    assert storage.get_chart_cache("recorded-chart") == entry


def test_valid_chart_reads_are_independent_and_missing_files_stay_missing(saved_chart):
    storage, path, entry = saved_chart
    first = storage.get_chart_cache("recorded-chart")
    first["data"]["c"][0] = 99
    assert storage.get_chart_cache("recorded-chart") == entry
    assert storage.get_chart_cache("missing") is None
    assert not storage._chart_cache_path_for("missing").exists()
