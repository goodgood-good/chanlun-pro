"""A binary64 cache never certifies or overwrites legacy FLOAT history."""
from contextlib import contextmanager
from types import SimpleNamespace
import datetime
import multiprocessing
import os
import threading

import pandas as pd
import pytest
from sqlalchemy import Double, Float, create_engine, inspect as sa_inspect
from sqlalchemy.dialects import mysql
from sqlalchemy.engine import URL
from sqlalchemy.orm import sessionmaker, declarative_base
from sqlalchemy.schema import CreateTable

from chanlun.persistence import db as db_module
from chanlun.persistence.kline_precision_schema import KLINE_BINARY64_REVISION, precision_table_name

REV = KLINE_BINARY64_REVISION


def isolated(engine):
    obj = object.__new__(db_module.DB.__wrapped__)
    obj.engine = engine
    obj.Session = sessionmaker(bind=engine, expire_on_commit=False)
    obj._DB__cache_tables = {}
    obj._cache_tables_lock = threading.Lock()
    obj._last_dt_cache = {}
    obj._last_dt_cache_generation = {}
    obj._last_dt_cache_lock = threading.Lock()
    return obj


@pytest.fixture
def storage(monkeypatch):
    monkeypatch.setattr(db_module.config,'DB_TYPE','sqlite')
    monkeypatch.setattr(db_module,'Base',declarative_base())
    engine = create_engine('sqlite://')
    original = set(db_module.Base.metadata.tables)
    yield isolated(engine)
    engine.dispose()
    for name in set(db_module.Base.metadata.tables)-original:
        db_module.Base.metadata.remove(db_module.Base.metadata.tables[name])


def bar(date='2026-10-09 00:41:00', price=81421.91, volume=7.478025):
    return pd.DataFrame([{'date':pd.Timestamp(date), 'open':price, 'close':price-.12,
        'high':price+.81, 'low':price-1.81, 'volume':volume}])


@pytest.mark.parametrize('market',['currency_spot','currency'])
def test_new_models_compile_to_double_for_both_binance_markets(storage,market):
    table = storage.klines_tables(market,'BTC/USDT',precision_revision=REV)
    sql = str(CreateTable(table.__table__).compile(dialect=mysql.dialect()))
    for key in ('o','c','h','l','v'):
        assert isinstance(getattr(table,key).type,Double)
        assert key+' DOUBLE' in sql
    assert 'FLOAT' not in sql
    assert table.__tablename__==market+'_klines_btc_usdt_p64_v1'


@pytest.mark.parametrize('market',['currency_spot','currency'])
def test_old_rows_remain_unchanged_and_are_never_copied_into_new_generation(storage,market):
    old = bar(price=81421.9,volume=7.47803)
    exact = bar()
    storage.klines_insert(market,'BTC/USDT','1m',old)
    assert storage.klines_query(market,'BTC/USDT','1m',precision_revision=REV)==[]
    storage.klines_insert(market,'BTC/USDT','1m',exact,precision_revision=REV)
    legacy = storage.klines_query(market,'BTC/USDT','1m')[0]
    trusted = storage.klines_query(market,'BTC/USDT','1m',precision_revision=REV)[0]
    assert (legacy.o,legacy.v)==(81421.9,7.47803)
    assert (trusted.o,trusted.v)==(81421.91,7.478025)
    for column,field in (('open','o'),('high','h'),('low','l'),('close','c'),('volume','v')):
        assert getattr(trusted,field).hex()==float(exact[column].iloc[0]).hex()
    assert len(sa_inspect(storage.engine).get_table_names())>=2


def test_upsert_and_date_query_remain_inside_the_requested_generation(storage):
    storage.klines_insert('currency_spot','BTC/USDT','1m',bar('2026-01-01',price=12.3))
    assert storage.klines_last_datetime('currency_spot','BTC/USDT','1m')=='2026-01-01 00:00:00'
    assert storage.klines_last_datetime('currency_spot','BTC/USDT','1m',precision_revision=REV) is None
    storage.klines_insert('currency_spot','BTC/USDT','1m',bar('2026-10-09'),precision_revision=REV)
    storage.klines_insert('currency_spot','BTC/USDT','1m',bar('2026-10-09',price=81421.92),precision_revision=REV)
    assert storage.klines_last_datetime('currency_spot','BTC/USDT','1m',precision_revision=REV)=='2026-10-09 00:00:00'
    assert storage.klines_last_datetime('currency_spot','BTC/USDT','1m')=='2026-01-01 00:00:00'
    assert len(storage.klines_query('currency_spot','BTC/USDT','1m',precision_revision=REV))==1
    assert storage.klines_query('currency_spot','BTC/USDT','1m',precision_revision=REV)[0].o==81421.92
    assert storage.klines_query('currency_spot','BTC/USDT','1m')[0].o==12.3


@pytest.mark.parametrize('deleted_revision',[None,REV])
def test_explicit_delete_does_not_cross_generation(storage,deleted_revision):
    for revision in (None,REV):
        storage.klines_insert('currency_spot','BTC/USDT','1m',bar(),precision_revision=revision)
    storage.klines_delete('currency_spot','BTC/USDT',precision_revision=deleted_revision)
    assert not storage.klines_query('currency_spot','BTC/USDT','1m',precision_revision=deleted_revision)
    other = REV if deleted_revision is None else None
    assert len(storage.klines_query('currency_spot','BTC/USDT','1m',precision_revision=other))==1


def test_static_reader_and_other_markets_keep_original_table_identity(storage):
    table = storage.klines_tables('us','NVDA.US')
    assert table.__tablename__=='us_klines_nvda_us'
    assert all(isinstance(getattr(table,key).type,Double) for key in ('o','c','h','l','v'))
    futures = storage.klines_tables('futures','SHFE.au')
    assert isinstance(futures.p.type,Double)
    with pytest.raises(ValueError,match='Binance'):
        storage.klines_query('us','NVDA.US','1m',precision_revision=REV)


@pytest.mark.parametrize('revision',['v2','',True,'../legacy'])
def test_unknown_precision_markers_are_rejected_before_table_access(storage,revision):
    with pytest.raises(ValueError):
        storage.klines_query('currency_spot','BTC/USDT','1m',precision_revision=revision)
    with pytest.raises(ValueError):
        storage.klines_insert('currency_spot','BTC/USDT','1m',pd.DataFrame(),precision_revision=revision)


def test_long_table_names_are_stable_and_distinct():
    left = precision_table_name('currency_spot_klines_'+'a'*90,'currency_spot',REV)
    right = precision_table_name('currency_spot_klines_'+'a'*89+'b','currency_spot',REV)
    assert len(left)<=64 and len(right)<=64 and left!=right
    assert left.endswith('_p64_v1') and left==precision_table_name('currency_spot_klines_'+'a'*90,'currency_spot',REV)


def test_native_table_creation_never_creates_or_alters_other_registered_tables(storage,monkeypatch):
    monkeypatch.setattr(db_module.Base.metadata,'create_all',lambda *_a,**_k:pytest.fail('broad create_all'))
    table = storage.klines_tables('currency_spot','BTC/USDT',precision_revision=REV)
    assert sa_inspect(storage.engine).get_table_names()==[table.__tablename__]


def test_existing_mysql_native_name_with_float_is_not_trusted(monkeypatch):
    monkeypatch.setattr(db_module,'Base',declarative_base())
    engine = SimpleNamespace(dialect=mysql.dialect(),url=URL.create('mysql+pymysql',host='fixture.invalid',database='fixture'))
    obj = isolated(engine)
    from sqlalchemy import Table
    monkeypatch.setattr(Table,'create',lambda *_a,**_k:None)
    columns=[{'name':key,'type':Float()} for key in ('o','c','h','l','v')]
    monkeypatch.setattr(db_module,'inspect',lambda _engine:SimpleNamespace(get_columns=lambda _:columns))
    name='currency_spot_klines_precision_failure_p64_v1'
    try:
        with pytest.raises(RuntimeError,match='DOUBLE'):
            obj.klines_tables('currency_spot','PRECISION/FAILURE',precision_revision=REV)
        assert name not in obj._DB__cache_tables
        columns[:]=[{'name':key,'type':mysql.DOUBLE()} for key in ('o','c','h','l','v')]
        assert obj.klines_tables('currency_spot','PRECISION/FAILURE',precision_revision=REV).__tablename__==name
    finally:
        if name in db_module.Base.metadata.tables:
            db_module.Base.metadata.remove(db_module.Base.metadata.tables[name])


def _spawn_native_writer(database,barrier,queue,position):
    try:
        db_module.config.DB_TYPE='sqlite'
        db_module.config.DATA_PATH=os.environ['CHANLUN_TEST_DATA_PATH']
        engine=create_engine('sqlite:///'+database,connect_args={'timeout':10})
        obj=isolated(engine)
        barrier.wait(timeout=20)
        frame=bar('2026-10-09 00:4'+str(position))
        obj.klines_insert('currency_spot','BTC/USDT','1m',frame,precision_revision=REV)
        queue.put(None)
        engine.dispose()
    except Exception as exc:
        queue.put(type(exc).__name__+': '+str(exc))


def test_four_spawn_workers_cold_create_one_native_table_without_copying_legacy(tmp_path):
    context=multiprocessing.get_context('spawn')
    barrier,queue=context.Barrier(4),context.Queue()
    database=str(tmp_path/'parallel.sqlite')
    workers=[context.Process(target=_spawn_native_writer,args=(database,barrier,queue,i)) for i in range(4)]
    for worker in workers:worker.start()
    failures=[queue.get(timeout=40) for _ in workers]
    for worker in workers:
        worker.join(timeout=10)
        assert worker.exitcode==0
    assert failures==[None]*4
    engine=create_engine('sqlite:///'+database)
    obj=isolated(engine)
    rows=obj.klines_query('currency_spot','BTC/USDT','1m',precision_revision=REV)
    assert len(rows)==4 and all(row.o==81421.91 and row.v==7.478025 for row in rows)
    assert sa_inspect(engine).get_table_names()==['currency_spot_klines_btc_usdt_p64_v1']
    engine.dispose()
