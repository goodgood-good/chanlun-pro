import datetime

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from chanlun.db_models.zixuan import TableByZixuan
from chanlun.db_models.zixuan_group import TableByZxGroup
from chanlun.persistence.db import DB


def _isolated_db():
    engine = create_engine("sqlite:///:memory:")
    TableByZxGroup.__table__.create(engine)
    TableByZixuan.__table__.create(engine)
    db_obj = object.__new__(DB.__wrapped__)
    db_obj.Session = sessionmaker(bind=engine, expire_on_commit=False)
    return db_obj


def _seed(db_obj, code="OLD"):
    with db_obj.Session() as session:
        session.add(
            TableByZixuan(
                market="a",
                zx_group="dst",
                stock_code=code,
                stock_name=code,
                position=0,
                add_datetime=datetime.datetime.now(),
                stock_color="",
                stock_memo="",
            )
        )
        session.commit()


def _codes(db_obj):
    with db_obj.Session() as session:
        return [
            row.stock_code
            for row in session.query(TableByZixuan)
            .filter_by(market="a", zx_group="dst")
            .order_by(TableByZixuan.position)
            .all()
        ]


def test_replace_group_stocks_commits_complete_snapshot_once():
    db_obj = _isolated_db()
    _seed(db_obj)

    db_obj.zx_replace_group_stocks(
        "a",
        "dst",
        [
            {"code": "NEW1", "name": "one"},
            {"code": "NEW2", "name": "two"},
        ],
    )

    assert _codes(db_obj) == ["NEW1", "NEW2"]


def test_replace_group_stocks_rolls_back_delete_when_new_snapshot_is_invalid():
    db_obj = _isolated_db()
    _seed(db_obj)

    with pytest.raises(KeyError):
        db_obj.zx_replace_group_stocks(
            "a",
            "dst",
            [{"code": "NEW1", "name": "one"}, {"name": "missing code"}],
        )

    assert _codes(db_obj) == ["OLD"]


def test_add_at_top_does_not_shift_same_named_group_in_other_market():
    db_obj = _isolated_db()
    now = datetime.datetime.now()
    with db_obj.Session() as session:
        for market, code, position in (
            ("a", "SH.600000", 0),
            ("a", "SZ.000001", 1),
            ("hk", "HK.00700", 0),
        ):
            session.add(
                TableByZixuan(
                    market=market,
                    zx_group="dst",
                    stock_code=code,
                    stock_name=code,
                    position=position,
                    add_datetime=now,
                    stock_color="",
                    stock_memo="",
                )
            )
        session.commit()

    db_obj.zx_add_group_stock(
        "a", "dst", "SH.600036", "招商银行", location="top"
    )

    with db_obj.Session() as session:
        hk_position = (
            session.query(TableByZixuan.position)
            .filter_by(market="hk", zx_group="dst", stock_code="HK.00700")
            .scalar()
        )
    assert hk_position == 0


def test_global_group_adapter_merges_definitions_and_preserves_member_markets():
    db_obj = _isolated_db()
    now = datetime.datetime.now()
    with db_obj.Session() as session:
        session.add_all(
            [
                TableByZxGroup(market="a", zx_group="跨市场", add_dt=now),
                TableByZxGroup(market="hk", zx_group="跨市场", add_dt=now),
                TableByZxGroup(market="__global__", zx_group="我的持仓", add_dt=now),
                TableByZixuan(
                    market="a",
                    zx_group="跨市场",
                    stock_code="SH.600000",
                    stock_name="浦发银行",
                    position=0,
                    add_datetime=now,
                    stock_color="",
                    stock_memo="",
                ),
                TableByZixuan(
                    market="hk",
                    zx_group="跨市场",
                    stock_code="HK.00700",
                    stock_name="腾讯控股",
                    position=0,
                    add_datetime=now,
                    stock_color="",
                    stock_memo="",
                ),
            ]
        )
        session.commit()

    assert [row.zx_group for row in db_obj.zx_get_global_groups()] == [
        "我的持仓",
        "跨市场",
    ]
    assert [
        (row.market, row.stock_code)
        for row in db_obj.zx_get_global_group_stocks("跨市场")
    ] == [("a", "SH.600000"), ("hk", "HK.00700")]
    assert db_obj.zx_add_global_group("跨市场") is False
    assert db_obj.zx_add_global_group("新分组") is True


def test_global_group_delete_removes_every_definition_and_member_atomically():
    db_obj = _isolated_db()
    now = datetime.datetime.now()
    with db_obj.Session() as session:
        session.add_all(
            [
                TableByZxGroup(market="a", zx_group="删除目标", add_dt=now),
                TableByZxGroup(market="hk", zx_group="删除目标", add_dt=now),
                TableByZixuan(
                    market="a",
                    zx_group="删除目标",
                    stock_code="SH.600000",
                    stock_name="浦发银行",
                    position=0,
                    add_datetime=now,
                    stock_color="",
                    stock_memo="",
                ),
                TableByZixuan(
                    market="hk",
                    zx_group="删除目标",
                    stock_code="HK.00700",
                    stock_name="腾讯控股",
                    position=0,
                    add_datetime=now,
                    stock_color="",
                    stock_memo="",
                ),
            ]
        )
        session.commit()

    assert db_obj.zx_del_global_group("删除目标") is True
    assert db_obj.zx_get_global_group_stocks("删除目标") == []
    assert all(
        row.zx_group != "删除目标" for row in db_obj.zx_get_global_groups()
    )
