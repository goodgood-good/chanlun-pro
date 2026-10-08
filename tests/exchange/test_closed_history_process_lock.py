"""Real processes cannot publish stale history over a completed forced refresh."""

from concurrent.futures import ThreadPoolExecutor
import multiprocessing
from pathlib import Path
from threading import Event

import pytest


def _frame(price):
    import pandas as pd
    result = pd.DataFrame({"date": pd.date_range("2026-09-11T19:57:00Z", periods=3, freq="min"),
                           "open": [price] * 3, "high": [price] * 3, "low": [price] * 3,
                           "close": [price] * 3, "volume": [100] * 3})
    result.attrs.update(price_basis_provider="longbridge", price_basis_adjustment="forward",
                        structure_price_quantum="0.01")
    return result


def _read(root, loader, *, force=False, code="QQQ.US"):
    import pandas as pd
    from chanlun.exchange.closed_history_cache import load_closed_us_history
    from chanlun.persistence.file_db import FileCacheDB
    storage = FileCacheDB.__new__(FileCacheDB)
    storage.klines_path = Path(root)
    observed = pd.Timestamp("2026-09-12T12:00:00+08:00")
    return load_closed_us_history(code=code, frequency="1m", observed=observed, end=observed,
                                  canonical=True, storage=storage, force=force, loader=loader)


def _child_reader(root, code, force, messages):
    import tests.conftest  # Isolate configuration before application imports in this process.
    from chanlun.persistence import file_lock
    original = file_lock._try_lock
    reported = False

    def acquire(handle):
        nonlocal reported
        locked = original(handle)
        if not locked and not reported:
            reported = True
            messages.put(("blocked", None))
        return locked

    file_lock._try_lock = acquire

    def loader():
        messages.put(("loader", None))
        return _frame(20.)

    try:
        value = _read(root, loader, force=force, code=code)
        messages.put(("result", float(value.close.iloc[-1])))
    except BaseException as exc:
        messages.put(("error", type(exc).__name__ + ": " + str(exc)))
        raise


@pytest.mark.parametrize("force", [False, True])
def test_processes_serialize_the_same_cache_and_forced_refresh_wins(tmp_path, force):
    context = multiprocessing.get_context("spawn")
    messages = context.Queue()
    entered, release = Event(), Event()

    def first_loader():
        entered.set()
        assert release.wait(15)
        return _frame(10.)

    child = context.Process(target=_child_reader, args=(str(tmp_path), "QQQ.US", force, messages))
    with ThreadPoolExecutor(max_workers=1) as executor:
        first = executor.submit(_read, tmp_path, first_loader)
        try:
            assert entered.wait(5)
            child.start()
            # The child reports either real lock contention (fixed code), or
            # entering its provider concurrently (old code). No timing guess.
            phase, detail = messages.get(timeout=10)
            assert phase in {"blocked", "loader"}, detail
            if phase == "loader":
                phase, detail = messages.get(timeout=10)
                assert phase == "result", detail
                child_result = detail
                release.set()
            else:
                release.set()
                phase, detail = messages.get(timeout=10)
                if phase == "loader":
                    phase, detail = messages.get(timeout=10)
                assert phase == "result", detail
                child_result = detail
            assert float(first.result(timeout=5).close.iloc[-1]) == 10.
            child.join(10)
            assert child.exitcode == 0
            expected = 20. if force else 10.
            assert child_result == expected
            saved = _read(tmp_path, lambda: (_ for _ in ()).throw(AssertionError("cache miss")))
            assert float(saved.close.iloc[-1]) == expected, "the earlier reader overwrote the newer refresh"
        finally:
            release.set()
            if child.pid is not None:
                child.join(5)
                if child.is_alive():
                    child.terminate()
                    child.join(5)
            messages.close()


def test_different_storage_roots_do_not_share_the_same_thread_lock(tmp_path):
    entered, release, second_entered = Event(), Event(), Event()

    def first_loader():
        entered.set()
        assert release.wait(10)
        return _frame(10.)

    def second_loader():
        second_entered.set()
        return _frame(20.)

    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(_read, tmp_path / "first", first_loader)
        try:
            assert entered.wait(5)
            second = executor.submit(_read, tmp_path / "second", second_loader)
            assert second_entered.wait(3), "an unrelated storage root waited on this cache"
            assert float(second.result(timeout=5).close.iloc[-1]) == 20.
        finally:
            release.set()
            first.result(timeout=5)
