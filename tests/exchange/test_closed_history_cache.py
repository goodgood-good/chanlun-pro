from concurrent.futures import ThreadPoolExecutor
from threading import Event

import pandas as pd
import pytest

from chanlun.exchange import closed_history_cache as cache
from chanlun.persistence.file_db import FileCacheDB


@pytest.fixture
def history(tmp_path):
    storage = FileCacheDB.__new__(FileCacheDB)
    storage.klines_path = tmp_path
    frame = pd.DataFrame({
        "date": pd.date_range("2026-09-11T19:57:00Z", periods=3, freq="min"),
        "open": [10., 11., 12.], "high": [11., 12., 13.], "low": [9., 10., 11.],
        "close": [10.5, 11.5, 12.5], "volume": [100., 120., 130.],
    })
    frame.attrs.update(price_basis_provider="longbridge", price_basis_adjustment="forward",
                       price_basis_revision="test-forward", structure_price_quantum="0.01")
    observed = pd.Timestamp("2026-09-12T12:00:00+08:00")
    calls = []

    def loader():
        calls.append(True)
        return frame.copy(deep=True)

    def read(at=observed, **kwargs):
        return cache.load_closed_us_history(
            code="QQQ.US", frequency="1m", canonical=True, observed=at,
            end=kwargs.pop("end", at), storage=storage,
            loader=kwargs.pop("loader", loader), **kwargs,
        )

    return frame, observed, calls, read, storage


def test_closed_history_survives_a_reader_restart_without_resetting_validation_age(history):
    frame, observed, calls, read, storage = history
    assert read().equals(frame)
    cache._locks.clear()  # Nothing except the persisted frame remains.
    second = read(observed + pd.Timedelta(minutes=10))
    assert second.equals(frame)
    assert second.attrs == {**frame.attrs, "_history_source_valid_until": observed.timestamp() + 3600}
    assert len(calls) == 1
    assert len(list(storage.klines_path.rglob("*.parquet"))) == 1
    read(observed + pd.Timedelta(hours=1))
    assert len(calls) == 2


def test_modified_history_or_provider_revision_is_revalidated(history, monkeypatch):
    frame, observed, calls, read, storage = history
    read()
    path = next(storage.klines_path.rglob("*.parquet"))
    damaged = pd.read_parquet(path)
    damaged.loc[0, "close"] += 1
    damaged.to_parquet(path, index=False)
    assert read(observed + pd.Timedelta(minutes=1)).equals(frame)
    assert len(calls) == 2
    monkeypatch.setattr(cache, "_source_revision", lambda: "changed-provider-contract")
    read(observed + pd.Timedelta(minutes=2))
    assert len(calls) == 3
    assert len(list(storage.klines_path.rglob("*.parquet"))) == 1


@pytest.mark.parametrize("at", ["2026-09-14T09:25:00-04:00", "2026-09-14T15:59:00-04:00",
                                "2026-09-14T16:20:00-04:00", "2026-12-14T09:25:00-05:00"])
def test_open_session_and_publication_grace_always_fetch(history, at):
    _frame, _observed, calls, read, storage = history
    read(pd.Timestamp(at))
    read(pd.Timestamp(at))
    assert len(calls) == 2
    assert not list(storage.klines_path.rglob("*.parquet"))


def test_historical_query_and_clock_regression_do_not_reuse(history):
    _frame, observed, calls, read, _storage = history
    read()
    read(observed - pd.Timedelta(minutes=1))
    read(observed, end=observed - pd.Timedelta(days=1))
    assert len(calls) == 3


def test_incomplete_response_is_never_persisted(history):
    _frame, _observed, _calls, read, storage = history
    incomplete = pd.DataFrame()
    incomplete.attrs["fetch_incomplete"] = True
    assert read(loader=lambda: incomplete) is incomplete
    assert not list(storage.klines_path.rglob("*.parquet"))


def test_concurrent_chart_configs_share_one_full_history_read(history):
    frame, _observed, calls, read, _storage = history
    entered, release = Event(), Event()

    def loader():
        calls.append(True)
        entered.set()
        assert release.wait(2)
        return frame.copy(deep=True)

    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(read, loader=loader)
        assert entered.wait(1)
        second = executor.submit(read, loader=loader)
        release.set()
        assert first.result(timeout=2).equals(second.result(timeout=2))
    assert len(calls) == 1


def test_forced_or_range_queries_bypass_adapter_cache(history, monkeypatch):
    from chanlun.exchange.exchange_cq import ExchangeChangQiao
    frame, *_ = history
    exchange = object.__new__(ExchangeChangQiao.__wrapped__)
    forced = []
    def load(**kwargs):
        assert kwargs["force"] is True
        forced.append(True)
        return kwargs["loader"]()
    monkeypatch.setattr(cache, "load_closed_us_history", load)
    monkeypatch.setattr(exchange, "_fetch_klines", lambda *args: frame)
    assert exchange.klines("QQQ.US", "1m", args={"_bypass_history_cache": True}) is frame
    assert exchange.klines("QQQ.US", "1m", start_date="2026-01-01") is frame
    assert forced == [True]


def test_manual_refresh_replaces_old_prices_and_a_failed_refresh_cannot_resurrect_them(history):
    frame, observed, calls, read, storage = history
    read()
    frame.loc[0, "close"] += .1
    assert read(observed + pd.Timedelta(minutes=1), force=True).equals(frame)
    assert read(observed + pd.Timedelta(minutes=2)).equals(frame)
    assert len(calls) == 2
    read(observed + pd.Timedelta(minutes=3), force=True, loader=lambda: pd.DataFrame())
    assert pd.read_parquet(next(storage.klines_path.rglob("*.parquet"))).empty
    read(observed + pd.Timedelta(minutes=4))
    assert len(calls) == 3
