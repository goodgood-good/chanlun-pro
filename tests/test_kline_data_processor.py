"""L12 回归:KlineDataProcessor._convert 对 NaN volume 的处理。

_preprocess 用 ``pd.to_numeric(errors='coerce')`` 把坏 volume 转成 NaN。
旧代码 ``a=float(row.get('volume') or 0.0)`` —— NaN 是 truthy,``nan or 0.0``
求值为 nan,Kline.a 会带着 NaN 经 cl_kline 合并 ``a=k1.a+k2.a`` 扩散。
"""

from __future__ import annotations

import datetime

import pandas as pd

from chanlun.core.kline_data_processor import KlineDataProcessor


def test_convert_nan_volume_becomes_zero():
    """volume 为 NaN 的行,转出的 Kline.a 必须是 0.0,不能是 NaN。"""
    df = pd.DataFrame({
        "date": [
            datetime.datetime(2024, 1, 1, 9, 30),
            datetime.datetime(2024, 1, 1, 9, 31),
        ],
        "high": [10.0, 11.0],
        "low": [9.0, 10.0],
        "open": [9.5, 10.5],
        "close": [9.8, 10.8],
        "volume": [float("nan"), 1000.0],
    })
    klines = KlineDataProcessor()._convert(df)
    assert klines[0].a == 0.0, f"NaN volume 应兜底为 0.0,实际 {klines[0].a}"
    assert klines[1].a == 1000.0


def test_convert_missing_volume_column_becomes_zero():
    """volume 列整列缺失时,Kline.a 兜底为 0.0,不崩。"""
    df = pd.DataFrame({
        "date": [datetime.datetime(2024, 1, 1, 9, 30)],
        "high": [10.0],
        "low": [9.0],
        "open": [9.5],
        "close": [9.8],
    })
    klines = KlineDataProcessor()._convert(df)
    assert klines[0].a == 0.0
