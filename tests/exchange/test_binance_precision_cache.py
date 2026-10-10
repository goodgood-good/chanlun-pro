"""Native precision caches never certify or overwrite legacy rounded history."""
from copy import deepcopy
from types import SimpleNamespace

import pandas as pd
import pytest

from chanlun.exchange.exchange_binance import ExchangeBinance
from chanlun.exchange.exchange_binance_spot import ExchangeBinanceSpot
from chanlun.exchange.exchange_db import ExchangeDB
from chanlun.persistence.kline_precision_schema import KLINE_BINARY64_REVISION


def frame(value=81421.91, volume=11.53477):
    return pd.DataFrame({'code': ['BTC/USDT'], 'date': pd.to_datetime(['2026-10-09 00:41:00']),
                         'open': [value], 'high': [value], 'low': [value], 'close': [value], 'volume': [volume]})


class Cache:
    def __init__(self, rows, verified=False):
        self.rows = rows
        self.verified = verified
        self.reads, self.writes = [], []

    def klines(self, code, frequency, args=None):
        self.reads.append(args.copy())
        result = self.rows.copy(deep=True)
        if self.verified:
            result.attrs['kline_storage_precision_revision'] = KLINE_BINARY64_REVISION
        return result

    def insert_klines(self, code, frequency, rows, *, precision_revision=None):
        self.writes.append((precision_revision, rows.copy(deep=True)))


@pytest.mark.parametrize('wrapper', [ExchangeBinanceSpot, ExchangeBinance], ids=['spot', 'futures'])
def test_untrusted_rounded_cache_is_excluded_and_rebuilt_into_native_namespace(wrapper):
    instance = object.__new__(getattr(wrapper, '__wrapped__', wrapper))
    old = frame(81421.9, 11.5348)
    saved = deepcopy(old)
    instance.db_exchange = Cache(old)
    requests = []
    def native(code, frequency, start_date=None, args=None):
        requests.append(start_date)
        return frame()
    instance.increment_klines_by_online = native
    result = instance.klines('BTC/USDT', '1m')
    assert requests == [None]
    assert instance.db_exchange.reads == [{'limit': 10000, 'precision_revision': KLINE_BINARY64_REVISION}]
    assert instance.db_exchange.writes[0][0] == KLINE_BINARY64_REVISION
    assert result.open.iloc[0] == 81421.91 and result.volume.iloc[0] == 11.53477
    pd.testing.assert_frame_equal(instance.db_exchange.rows, saved)


@pytest.mark.parametrize('wrapper', [ExchangeBinanceSpot, ExchangeBinance], ids=['spot', 'futures'])
def test_native_failure_does_not_fall_back_to_legacy_float_rows(wrapper):
    instance = object.__new__(getattr(wrapper, '__wrapped__', wrapper))
    instance.db_exchange = Cache(frame(81421.9))
    instance.increment_klines_by_online = lambda *_, **__: None
    result = instance.klines('BTC/USDT', '1m')
    assert result.empty and instance.db_exchange.writes == []
    assert instance.db_exchange.rows.open.iloc[0] == 81421.9


@pytest.mark.parametrize('wrapper', [ExchangeBinanceSpot, ExchangeBinance], ids=['spot', 'futures'])
def test_verified_single_row_cache_preserves_exact_values_and_incremental_path(wrapper):
    instance = object.__new__(getattr(wrapper, '__wrapped__', wrapper))
    instance.db_exchange = Cache(frame(), verified=True)
    starts = []
    def native(code, frequency, start_date=None, args=None):
        starts.append(start_date); return None
    instance.increment_klines_by_online = native
    result = instance.klines('BTC/USDT', '1m')
    assert starts == ['2026-10-09 00:41:00']
    assert result.open.iloc[0] == 81421.91 and result.volume.iloc[0] == 11.53477
    assert instance.db_exchange.writes == []


@pytest.mark.parametrize('market', ['currency_spot', 'currency'])
def test_exchange_db_forwards_precision_and_preserves_default_legacy_access(monkeypatch, market):
    from chanlun.exchange import exchange_db
    queries, writes = [], []
    row = SimpleNamespace(code='BTC/USDT', dt=pd.Timestamp('2026-10-09 00:41:00'),
                          o=81421.91, h=81421.91, l=81421.91, c=81421.91, v=11.53477)
    def query(*args, **kwargs):
        queries.append((args, kwargs)); return [row]
    monkeypatch.setattr(exchange_db, 'db', SimpleNamespace(klines_query=query,
                      klines_insert=lambda *a, **kw: writes.append((a, kw))))
    store = ExchangeDB(market)
    exact = store.klines('BTC/USDT', '1m', args={'limit': 10000, 'precision_revision': KLINE_BINARY64_REVISION})
    assert queries[-1][1] == {'precision_revision': KLINE_BINARY64_REVISION}
    assert exact.attrs['kline_storage_precision_revision'] == KLINE_BINARY64_REVISION
    assert exact.open.iloc[0] == row.o and exact.volume.iloc[0] == row.v
    store.insert_klines('BTC/USDT', '1m', exact, precision_revision=KLINE_BINARY64_REVISION)
    assert writes[-1][1] == {'precision_revision': KLINE_BINARY64_REVISION}
    legacy = store.klines('BTC/USDT', '1m')
    store.insert_klines('BTC/USDT', '1m', legacy)
    assert queries[-1][1] == writes[-1][1] == {}
    assert 'kline_storage_precision_revision' not in legacy.attrs


@pytest.mark.parametrize('wrapper', [ExchangeBinanceSpot, ExchangeBinance], ids=['spot', 'futures'])
def test_cold_rebuild_keeps_ten_page_cap_without_overlapping_open_timestamps(wrapper):
    instance = object.__new__(getattr(wrapper, '__wrapped__', wrapper))
    instance.tz = 'UTC'
    opens = [1_700_000_000_000 + i * 60000 for i in range(12000)]
    calls = []
    def fetch(**kwargs):
        calls.append(kwargs)
        end = kwargs['params'].get('endTime', opens[-1])
        return [[stamp, 81421.91, 81421.92, 81421.9, 81421.91, 11.53477]
                for stamp in opens if stamp <= end][-kwargs['limit']:]
    instance.exchange = SimpleNamespace(fetch_ohlcv=fetch)
    result = instance.increment_klines_by_online('BTC/USDT', '1m')
    assert len(calls) == 10 and all(call['limit'] == 1000 for call in calls)
    assert len(result) == 10000 and result.date.is_unique
    assert result.open.eq(81421.91).all() and result.volume.eq(11.53477).all()


@pytest.mark.parametrize('market,wrapper', [('currency_spot', ExchangeBinanceSpot), ('currency', ExchangeBinance)])
def test_real_sqlite_namespace_roundtrip_leaves_legacy_table_intact(monkeypatch, tmp_path, market, wrapper):
    from sqlalchemy import create_engine
    from sqlalchemy.orm import declarative_base
    from chanlun.persistence import db as db_module
    from chanlun.exchange import exchange_db
    from tests.persistence.test_kline_precision_namespace import isolated

    engine = create_engine('sqlite:///' + str(tmp_path / 'native.sqlite'))
    monkeypatch.setattr(db_module, 'Base', declarative_base())
    storage = isolated(engine)
    original = set(db_module.Base.metadata.tables)
    monkeypatch.setattr(db_module.config, 'DB_TYPE', 'sqlite')
    monkeypatch.setattr(exchange_db, 'db', storage)
    try:
        storage.klines_insert(market, 'BTC/USDT', '1m', frame(81421.9, 11.5348))
        instance = object.__new__(getattr(wrapper, '__wrapped__', wrapper))
        instance.db_exchange = ExchangeDB(market)
        calls = []
        def native(code, frequency, start_date=None, args=None):
            calls.append(start_date)
            return frame() if len(calls) == 1 else None
        instance.increment_klines_by_online = native
        first = instance.klines('BTC/USDT', '1m')
        second = instance.klines('BTC/USDT', '1m')
        assert calls == [None, '2026-10-09 00:41:00']
        assert first.open.iloc[0].hex() == second.open.iloc[0].hex() == float(81421.91).hex()
        assert first.volume.iloc[0].hex() == second.volume.iloc[0].hex() == float(11.53477).hex()
        legacy = storage.klines_query(market, 'BTC/USDT', '1m')
        assert len(legacy) == 1 and (legacy[0].o, legacy[0].v) == (81421.9, 11.5348)
        trusted = storage.klines_query(market, 'BTC/USDT', '1m', precision_revision=KLINE_BINARY64_REVISION)
        assert len(trusted) == 1 and (trusted[0].o, trusted[0].v) == (81421.91, 11.53477)
    finally:
        engine.dispose()
        for name in set(db_module.Base.metadata.tables) - original:
            db_module.Base.metadata.remove(db_module.Base.metadata.tables[name])
