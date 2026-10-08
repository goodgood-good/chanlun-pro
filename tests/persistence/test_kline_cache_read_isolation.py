"""An unsuccessful cache reader must never remove a concurrent writer's file."""

from pathlib import Path

import pandas as pd
import pytest

from chanlun.persistence.file_db import FileCacheDB


@pytest.fixture
def saved_cache(tmp_path):
    storage = FileCacheDB.__new__(FileCacheDB)
    storage.klines_path = tmp_path
    frame = pd.DataFrame({
        "date": pd.date_range("2026-09-21 10:00:00", periods=3, freq="5min", tz="Asia/Shanghai"),
        "open": [10., 11., 12.], "high": [11., 12., 13.], "low": [9., 10., 11.],
        "close": [10.5, 11.5, 12.5], "volume": [100, 200, 300],
    })
    frame.attrs["source"] = "isolated-cache-fixture"
    assert storage.save_klines_parquet("us", "EXAMPLE.US", "5m", frame)
    return storage, storage._kline_parquet_path("us", "EXAMPLE.US", "5m"), frame


@pytest.mark.parametrize("failure", [PermissionError, OSError, ValueError])
def test_transient_read_error_is_a_miss_without_deleting_valid_data(saved_cache, monkeypatch, failure):
    storage, path, frame = saved_cache
    original = path.read_bytes()
    with monkeypatch.context() as patch:
        patch.setattr(pd, "read_parquet", lambda *args, **kwargs: (_ for _ in ()).throw(failure("read unavailable")))
        assert storage.load_klines_parquet("us", "EXAMPLE.US", "5m") is None
    assert path.is_file(), "a read error deleted a valid shared cache"
    assert path.read_bytes() == original
    pd.testing.assert_frame_equal(storage.load_klines_parquet("us", "EXAMPLE.US", "5m"), frame)


def test_failed_old_read_does_not_delete_a_newly_published_file(saved_cache, monkeypatch):
    storage, path, frame = saved_cache
    fresh = frame.copy(deep=True)
    fresh.loc[2, "close"] = 12.75

    def old_read_failed(*args, **kwargs):
        assert storage.save_klines_parquet("us", "EXAMPLE.US", "5m", fresh)
        raise ValueError("the earlier read failed after the atomic replacement")

    with monkeypatch.context() as patch:
        patch.setattr(pd, "read_parquet", old_read_failed)
        assert storage.load_klines_parquet("us", "EXAMPLE.US", "5m") is None
    assert path.is_file(), "late read failure removed the writer's replacement"
    pd.testing.assert_frame_equal(storage.load_klines_parquet("us", "EXAMPLE.US", "5m"), fresh)


def test_cache_metadata_permission_error_does_not_escape_or_remove_data(saved_cache, monkeypatch):
    storage, path, frame = saved_cache
    original = Path.is_file

    def unavailable(target):
        if target == path:
            raise PermissionError("metadata temporarily unavailable")
        return original(target)

    with monkeypatch.context() as patch:
        patch.setattr(Path, "is_file", unavailable)
        assert storage.load_klines_parquet("us", "EXAMPLE.US", "5m") is None
    pd.testing.assert_frame_equal(storage.load_klines_parquet("us", "EXAMPLE.US", "5m"), frame)


def test_corrupt_bytes_can_be_replaced_without_reader_deletion(saved_cache):
    storage, path, frame = saved_cache
    path.write_bytes(b"not a parquet file")
    assert storage.load_klines_parquet("us", "EXAMPLE.US", "5m") is None
    assert path.read_bytes() == b"not a parquet file"
    assert storage.save_klines_parquet("us", "EXAMPLE.US", "5m", frame)
    restored = storage.load_klines_parquet("us", "EXAMPLE.US", "5m")
    pd.testing.assert_frame_equal(restored, frame)
    assert restored.attrs == frame.attrs


def test_missing_cache_remains_a_miss_without_creating_files(tmp_path):
    storage = FileCacheDB.__new__(FileCacheDB)
    storage.klines_path = tmp_path
    assert storage.load_klines_parquet("us", "EXAMPLE.US", "5m") is None
    assert list(tmp_path.iterdir()) == []
