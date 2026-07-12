from chanlun.persistence import file_db as file_db_module
from chanlun.persistence.file_db import FileCacheDB


def test_pickle_writer_shutdown_and_restart(tmp_path):
    file_db_module.shutdown_pickle_writes(wait=True)
    file_db_module.start_pickle_writes()

    file_db = object.__new__(FileCacheDB)
    target = tmp_path / "restart.pkl"
    future = file_db._atomic_write_pickle(target, {"ready": True})

    assert future.result(timeout=2) is None
    assert target.is_file()


def test_late_pickle_write_does_not_restart_writer_after_shutdown(tmp_path):
    from chanlun.persistence import file_db as module

    module.shutdown_pickle_writes(wait=False, cancel_pending=True)
    file_db = object.__new__(FileCacheDB)
    target = tmp_path / "late.pkl"

    future = file_db._atomic_write_pickle(target, {"late": True})

    assert future.result(timeout=1) is None
    assert target.exists() is False
    assert module._PICKLE_WRITE_EXECUTOR is None
