import pickle
import pytest
import threading

import chanlun.persistence.file_db as file_db_module
from chanlun.persistence.file_db import FileCacheDB


def test_same_path_write_queue_coalesces_when_capacity_is_reached(tmp_path, monkeypatch):
    file_db = object.__new__(FileCacheDB)
    target = tmp_path / "bounded.pkl"
    entered = threading.Event()
    release = threading.Event()
    real_write = file_db._atomic_write_pickle_blocking

    def controlled_write(path, obj):
        if obj["sequence"] == 1:
            entered.set()
            release.wait(timeout=2)
        real_write(path, obj)

    monkeypatch.setattr(file_db, "_atomic_write_pickle_blocking", controlled_write)
    monkeypatch.setattr(file_db_module, "_PICKLE_WRITE_MAX_PENDING_PER_PATH", 2)

    first = file_db._atomic_write_pickle(target, {"sequence": 1})
    assert entered.wait(timeout=1)
    second = file_db._atomic_write_pickle(target, {"sequence": 2})
    third = file_db._atomic_write_pickle(target, {"sequence": 3})
    fourth = file_db._atomic_write_pickle(target, {"sequence": 4})

    path_key = file_db_module.os.path.normcase(
        file_db_module.os.path.abspath(file_db_module.os.fspath(target))
    )
    with file_db_module._PICKLE_WRITE_QUEUE_LOCK:
        assert len(file_db_module._PICKLE_WRITE_QUEUES[path_key].items) == 2

    release.set()
    for future in (first, second, third, fourth):
        assert future.result(timeout=5) is None

    with open(target, "rb") as fp:
        assert pickle.load(fp) == {"sequence": 4}

def test_new_path_is_rejected_when_active_path_capacity_is_exhausted(
    tmp_path, monkeypatch
):
    file_db = object.__new__(FileCacheDB)
    first_target = tmp_path / "first.pkl"
    rejected_target = tmp_path / "rejected.pkl"
    entered = threading.Event()
    release = threading.Event()
    real_write = file_db._atomic_write_pickle_blocking

    def controlled_write(path, obj):
        if path == first_target:
            entered.set()
            release.wait(timeout=2)
        real_write(path, obj)

    monkeypatch.setattr(file_db, "_atomic_write_pickle_blocking", controlled_write)
    monkeypatch.setattr(file_db_module, "_PICKLE_WRITE_MAX_PATHS", 1)

    first = file_db._atomic_write_pickle(first_target, {"sequence": 1})
    assert entered.wait(timeout=1)
    rejected = file_db._atomic_write_pickle(
        rejected_target,
        {"sequence": 2},
        suppress_errors=False,
    )

    with pytest.raises(BufferError, match="active path limit"):
        rejected.result(timeout=0.2)
    assert rejected_target.exists() is False

    release.set()
    assert first.result(timeout=2) is None