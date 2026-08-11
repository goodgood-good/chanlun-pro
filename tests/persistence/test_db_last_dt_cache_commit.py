import threading
import datetime

import pandas as pd
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from chanlun.persistence import db as db_module
from chanlun.persistence.db import DB


def _isolated_db(monkeypatch):
    monkeypatch.setattr(db_module.config, "DB_TYPE", "sqlite")
    db_obj = object.__new__(DB.__wrapped__)
    db_obj.engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    db_obj.Session = sessionmaker(bind=db_obj.engine, expire_on_commit=False)
    db_obj._DB__cache_tables = {}
    db_obj._cache_tables_lock = threading.Lock()
    db_obj._last_dt_cache = {}
    db_obj._last_dt_cache_generation = {}
    db_obj._last_dt_cache_lock = threading.Lock()
    return db_obj


def _bar(date="2024-01-01 15:00:00"):
    return pd.DataFrame(
        [
            {
                "date": pd.Timestamp(date),
                "open": 10.0,
                "high": 11.0,
                "low": 9.0,
                "close": 10.5,
                "volume": 100.0,
            }
        ]
    )


def _force_last_dt_cache(db_obj, market, code, frequency, value):
    with db_obj._last_dt_cache_lock:
        key = (market, code, frequency)
        db_obj._last_dt_cache_generation.setdefault(key, 0)
        db_obj._last_dt_cache[key] = value


def _last_dt_cache_value(db_obj, market, code, frequency):
    with db_obj._last_dt_cache_lock:
        return db_obj._last_dt_cache.get((market, code, frequency))


def test_insert_invalidates_last_datetime_cache_after_commit(monkeypatch):
    db_obj = _isolated_db(monkeypatch)
    code = "SH.910001"
    _force_last_dt_cache(db_obj, "a", code, "d", "old")

    def repopulate_stale_cache(_session):
        _force_last_dt_cache(db_obj, "a", code, "d", "stale-during-commit")

    event.listen(db_obj.Session.class_, "after_commit", repopulate_stale_cache)
    db_obj.klines_insert("a", code, "d", _bar())

    assert _last_dt_cache_value(db_obj, "a", code, "d") is None


def test_delete_all_frequencies_invalidates_each_cache_key_after_commit(monkeypatch):
    db_obj = object.__new__(DB.__wrapped__)
    db_obj._last_dt_cache = {}
    db_obj._last_dt_cache_generation = {}
    db_obj._last_dt_cache_lock = threading.Lock()
    code = "SH.920002"
    _force_last_dt_cache(db_obj, "a", code, "d", "old-d")
    _force_last_dt_cache(db_obj, "a", code, "5m", "old-5m")

    class DeleteQuery:
        def filter(self, *_args):
            return self

        def delete(self):
            return 1

    class DeleteSession:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def query(self, *_args):
            return DeleteQuery()

        def commit(self):
            _force_last_dt_cache(db_obj, "a", code, "d", "stale-during-commit")

        def rollback(self):
            pass

    table = type(
        "FakeDeleteKlineTable",
        (),
        {"dt": _Field(), "code": _Field(), "f": _Field()},
    )
    db_obj.Session = DeleteSession
    db_obj.klines_tables = lambda _market, _code: table
    db_obj.klines_delete("a", code)

    assert _last_dt_cache_value(db_obj, "a", code, "d") is None
    assert _last_dt_cache_value(db_obj, "a", code, "5m") is None


class _Field:
    def __eq__(self, _other):
        return self

    def desc(self):
        return self


class _BlockingLastDateQuery:
    def __init__(self, query_started, allow_old_result):
        self.query_started = query_started
        self.allow_old_result = allow_old_result
        self.calls = 0

    def filter(self, *_args):
        return self

    def order_by(self, *_args):
        return self

    def first(self):
        self.calls += 1
        if self.calls == 1:
            self.query_started.set()
            assert self.allow_old_result.wait(timeout=5)
            return (datetime.datetime(2024, 1, 1, 15, 0),)
        return (datetime.datetime(2024, 1, 2, 15, 0),)


class _FakeSession:
    def __init__(self, query):
        self._query = query

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def query(self, *_args):
        return self._query


def test_reader_cannot_repopulate_stale_last_datetime_after_commit_invalidation():
    db_obj = object.__new__(DB.__wrapped__)
    db_obj._last_dt_cache = {}
    db_obj._last_dt_cache_generation = {}
    db_obj._last_dt_cache_lock = threading.Lock()
    query_started = threading.Event()
    allow_old_result = threading.Event()
    query = _BlockingLastDateQuery(query_started, allow_old_result)
    db_obj.Session = lambda: _FakeSession(query)
    table = type(
        "FakeKlineTable",
        (),
        {"dt": _Field(), "code": _Field(), "f": _Field()},
    )
    db_obj.klines_tables = lambda _market, _code: table
    result = []

    reader = threading.Thread(
        target=lambda: result.append(db_obj.klines_last_datetime("a", "SH.910003", "d"))
    )
    reader.start()
    assert query_started.wait(timeout=5)

    # 模拟写事务已 commit，随后完成失效；此时读线程仍握有提交前查到的旧值。
    db_obj._invalidate_last_dt_cache("a", "SH.910003", "d")
    allow_old_result.set()
    reader.join(timeout=5)

    assert not reader.is_alive()
    assert result == ["2024-01-02"]
    assert _last_dt_cache_value(db_obj, "a", "SH.910003", "d") == "2024-01-02"
    assert query.calls == 2
