import csv
import datetime
import io
from concurrent.futures import ThreadPoolExecutor

from flask import Flask
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from chanlun import zixuan as model
from chanlun.db_models.zixuan import TableByZixuan
from chanlun.db_models.zixuan_group import TableByZxGroup
from chanlun.persistence.db import DB
from cl_app.blueprints import zixuan as routes
from tests.persistence.test_zixuan_atomic_replace import _isolated_db


@pytest.fixture
def store(monkeypatch):
    db = _isolated_db()
    with db.Session() as session:
        session.add_all([TableByZxGroup(market='__global__', zx_group=name, add_dt=datetime.datetime.now())
                         for name in ('我的关注', '我的持仓', 'target')])
        session.commit()
    monkeypatch.setattr(model, 'db', db)

    class Exchange:
        stock_info_query_scope = 'SINGLE_SYMBOL_STOCK_INFO'

        def stock_info(self, code):
            return {'code': code, 'name': code}

        def all_stocks(self):
            raise AssertionError('imports must not enumerate catalogs')

    monkeypatch.setattr(routes, 'get_exchange', lambda _market: Exchange())
    app = Flask(__name__)
    app.config['LOGIN_DISABLED'] = True
    app.register_blueprint(routes.zixuan_bp)
    return db, app.test_client()


def upload(client, content, group='target'):
    return client.post('/zixuan_opt_import', data={
        'market': 'a', 'zx_group': group, 'file': (io.BytesIO(content.encode('utf-8')), 'symbols.csv'),
    }, content_type='multipart/form-data')


def test_cross_market_csv_roundtrip_quotes_commas_and_newlines(store):
    db, client = store
    rows = [{'market': 'a', 'code': 'SH.600519', 'name': 'A, "sample"'},
            {'market': 'us', 'code': 'AAPL.US', 'name': 'US\nname'},
            {'market': 'hk', 'code': '00700.HK', 'name': 'HK sample'}]
    db.zx_merge_group_stocks('我的关注', rows)
    response = client.get('/zixuan_opt_export?market=a&zx_group=我的关注')
    assert response.status_code == 200 and response.mimetype == 'text/csv'
    content = response.data.decode('utf-8-sig')
    assert list(csv.reader(io.StringIO(content)))[0] == ['market', 'code', 'name']
    body = upload(client, content).get_json()
    assert body['ok'] and body['added'] == 3 and body['updated'] == body['skipped'] == 0
    assert {(r.market, r.stock_code, r.stock_name) for r in db.zx_get_global_group_stocks('target')} == {
        (row['market'], row['code'], row['name']) for row in rows}


def test_import_merges_markets_preserving_existing_members_and_metadata(store):
    db, client = store
    db.zx_merge_group_stocks('target', [{'market': 'a', 'code': 'SH.600519', 'name': 'old'},
                                     {'market': 'us', 'code': 'MSFT.US', 'name': 'untouched'}])
    with db.Session() as session:
        row = session.query(TableByZixuan).filter_by(stock_code='SH.600519').one()
        row.stock_color, row.stock_memo, row.position = '#ff5722', 'retain', 17
        stamp = row.add_datetime
        session.commit()
    body = upload(client, 'market,code,name\na,600519,new\nhk,00700.HK,Tencent\n').get_json()
    assert body['added'] == body['updated'] == 1 and body['imported'] == 2
    rows = {row.stock_code: row for row in db.zx_get_global_group_stocks('target')}
    assert set(rows) == {'SH.600519', 'MSFT.US', '00700.HK'}
    row = rows['SH.600519']
    assert (row.stock_name, row.stock_color, row.stock_memo, row.position, row.add_datetime) == ('new', '#ff5722', 'retain', 17, stamp)


def test_import_reports_invalid_duplicate_and_failed_identity_rows(store, monkeypatch):
    db, client = store
    monkeypatch.setattr(routes, 'get_exchange', lambda market: (_ for _ in ()).throw(OSError('fixture')) if market.value == 'us' else object())
    body = upload(client, 'market,code,name\na,600519,first\nxx,code,bad market\na,bad,bad code\na,600519,last\nus,AAPL.US,lookup fails\n').get_json()
    assert body['imported'] == 1 and body['skipped'] == 4
    assert [row['line'] for row in body['issues']] == [3, 4, 5, 6]
    # Duplicate input is separately reported, alongside invalid and unavailable rows.
    assert body['skipped'] == len(body['issues'])
    assert db.zx_get_global_group_stocks('target')[0].stock_name == 'last'


def test_all_invalid_file_reports_no_write_and_malformed_csv_is_rejected(store):
    db, client = store
    body = upload(client, 'market,code,name\na,not-a-code,bad\n').get_json()
    assert body['ok'] is False and body['imported'] == 0 and body['issues']
    response = upload(client, 'market,code,name\na,600519,"unclosed\n')
    assert response.status_code == 400
    assert db.zx_get_global_group_stocks('target') == []


def test_atomic_merge_rolls_back_prior_changes_and_preserves_other_market(store):
    db, _ = store
    db.zx_merge_group_stocks('target', [{'market': 'us', 'code': 'MSFT.US', 'name': 'original'}])
    with pytest.raises(KeyError):
        db.zx_merge_group_stocks('target', [{'market': 'us', 'code': 'MSFT.US', 'name': 'changed'},
                                          {'market': 'a', 'code': 'SH.600519'}])
    assert [(row.market, row.stock_code, row.stock_name) for row in db.zx_get_global_group_stocks('target')] == [('us', 'MSFT.US', 'original')]


def test_merge_rejects_a_group_deleted_before_commit(store):
    db, _ = store
    with pytest.raises(ValueError, match='不存在'):
        db.zx_merge_group_stocks('deleted', [{'market': 'a', 'code': 'SH.600519', 'name': 'sample'}])


def test_legacy_txt_retains_unquoted_commas_in_names(store):
    db, client = store
    body = upload(client, '600519,legacy, name\n000001,"quoted, name"\n').get_json()
    assert body['imported'] == 2 and body['skipped'] == 0
    assert {row.stock_name for row in db.zx_get_global_group_stocks('target')} == {'legacy, name', 'quoted, name'}


@pytest.mark.parametrize('content', ['920030,Beijing\n', 'market,code,name\na,920030,Beijing\n'])
def test_beijing_920_code_is_never_imported_as_shanghai(store, monkeypatch, content):
    db, client = store
    looked_up = []

    class Exchange:
        stock_info_query_scope = 'SINGLE_SYMBOL_STOCK_INFO'

        def stock_info(self, code):
            looked_up.append(code)
            return None

    monkeypatch.setattr(routes, 'get_exchange', lambda _market: Exchange())
    body = upload(client, content).get_json()
    assert body['ok'] and body['issues'] == []
    assert looked_up == ['BJ.920030']
    assert [row.stock_code for row in db.zx_get_global_group_stocks('target')] == ['BJ.920030']


def test_concurrent_imports_merge_without_duplicates_or_losing_unrelated_members(tmp_path):
    engine = create_engine('sqlite:///' + str(tmp_path / 'imports.sqlite'))
    TableByZxGroup.__table__.create(engine)
    TableByZixuan.__table__.create(engine)
    db = object.__new__(DB.__wrapped__)
    db.Session = sessionmaker(bind=engine, expire_on_commit=False)
    try:
        with db.Session() as session:
            session.add(TableByZxGroup(market='__global__', zx_group='target', add_dt=datetime.datetime.now()))
            session.commit()
        db.zx_merge_group_stocks('target', [{'market': 'hk', 'code': '00700.HK', 'name': 'keep'}])
        with ThreadPoolExecutor(max_workers=2) as workers:
            futures = [workers.submit(db.zx_merge_group_stocks, 'target', [
                {'market': 'a', 'code': 'SH.600519', 'name': 'same'},
                {'market': 'us', 'code': code, 'name': code}]) for code in ('AAPL.US', 'MSFT.US')]
            counts = [future.result(timeout=5) for future in futures]
        assert sum(item['added'] for item in counts) == 3
        assert sum(item['updated'] for item in counts) == 1
        rows = db.zx_get_global_group_stocks('target')
        assert len(rows) == 4 and {row.stock_code for row in rows} == {'SH.600519', 'AAPL.US', 'MSFT.US', '00700.HK'}
    finally:
        engine.dispose()
