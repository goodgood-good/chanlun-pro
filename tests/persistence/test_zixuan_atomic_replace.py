import datetime

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from chanlun.db_models.zixuan import TableByZixuan
from chanlun.persistence.db import DB


def _isolated_db():
    engine = create_engine("sqlite:///:memory:")
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
