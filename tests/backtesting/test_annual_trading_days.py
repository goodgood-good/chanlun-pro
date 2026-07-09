# -*- coding: utf-8 -*-
"""C10: backtest.result() 的 annual_days 市场列表隐式字符串拼接笔误
["a","us","hk" "futures"] → "hkfutures", 致 hk/futures 年化错用 365(应 240)。
抽出的 _annual_trading_days 钉死正确口径。
"""
import pytest

pytest.importorskip("pyecharts")  # backtest 模块 import pyecharts, 未装则跳过

from chanlun.backtesting.backtest import _annual_trading_days


@pytest.mark.parametrize("market", ["a", "us", "hk", "futures"])
def test_stock_and_futures_use_240(market):
    assert _annual_trading_days(market) == 240


@pytest.mark.parametrize("market", ["currency", "spot", "", "unknown"])
def test_others_use_365(market):
    assert _annual_trading_days(market) == 365


def test_hk_and_futures_not_concatenated():
    # 回归防线: 若列表退化为 "hkfutures", 下面两条会各自落 365 而非 240
    assert _annual_trading_days("hk") == 240
    assert _annual_trading_days("futures") == 240
    assert _annual_trading_days("hkfutures") == 365