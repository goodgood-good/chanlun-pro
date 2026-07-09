# -*- coding: utf-8 -*-
"""C4: get_tdx_klines 经 parquet 读回时, 若源 df 的 date 为 str(IB klines() 缓存即如此),
parquet 原样保留 str dtype → 下游 datetime.now()-date(script_ib_tasks 增量 diff_days)
必抛 TypeError, 每标的只能成功同步一次后链路永久失败。修复: get_tdx_klines 统一把 date
强转 datetime(CSV 路径本就 parse_dates)。
"""
import datetime

import pandas as pd

from chanlun.file_db_mixins import kline_cache
from chanlun.persistence.file_db import FileCacheDB


def _make_db(tmp_path, monkeypatch):
    db = FileCacheDB()
    db.klines_path = tmp_path
    # 关掉随机清理, 保证确定性
    monkeypatch.setattr(kline_cache.random, "randint", lambda a, b: 999)
    return db


def _str_date_df(n=120):
    dates = [
        (datetime.datetime(2024, 1, 1) + datetime.timedelta(days=i)).strftime(
            "%Y-%m-%d %H:%M:%S"
        )
        for i in range(n)
    ]
    return pd.DataFrame({
        "code": ["ib_AAPL"] * n,
        "date": dates,               # 故意存 str
        "open": [1.0] * n, "high": [1.0] * n,
        "low": [1.0] * n, "close": [1.0] * n, "volume": [1.0] * n,
    })


def test_str_date_parquet_read_back_as_datetime(tmp_path, monkeypatch):
    db = _make_db(tmp_path, monkeypatch)
    db.save_klines_parquet("us", "ib_AAPL", "1day", _str_date_df())
    got = db.get_tdx_klines("us", "ib_AAPL", "1day")
    assert got is not None and len(got) > 0
    assert pd.api.types.is_datetime64_any_dtype(got["date"])
    # 下游运算不再 TypeError
    diff = (datetime.datetime.now() - got.iloc[-1]["date"]).days
    assert isinstance(diff, int)


def test_datetime_date_unaffected(tmp_path, monkeypatch):
    db = _make_db(tmp_path, monkeypatch)
    df = _str_date_df()
    df["date"] = pd.to_datetime(df["date"])   # 本就 datetime
    db.save_klines_parquet("us", "ib_MSFT", "1day", df)
    got = db.get_tdx_klines("us", "ib_MSFT", "1day")
    assert pd.api.types.is_datetime64_any_dtype(got["date"])