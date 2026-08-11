"""R1-C5: binance/binance_spot K线缓存库恰1根时 iloc[-2] 越界致永久 RetryError。

新上市合约首查入库1根后, 第二次调用 db_klines.iloc[-2] IndexError → except
吞掉返回 None → tenacity 重试3次同崩 → RetryError; 且增量拉取+insert 在崩点
之后永远执行不到, 本地库恒1根不可自愈(需手工删库)。
修复=恰1根时退用 iloc[-1] 作增量起点。ccxt 未装, 注入 stub 后 object.__new__
绕过网络构造(同 test_binance_order_normalize 模式)。
"""
import sys
import types

import pandas as pd
import pytest

if "ccxt" not in sys.modules:
    sys.modules["ccxt"] = types.ModuleType("ccxt")

from chanlun.exchange.exchange_binance import ExchangeBinance
from chanlun.exchange.exchange_binance_spot import ExchangeBinanceSpot


def _real_cls(w):
    # @fun.singleton 用 @wraps(cls) 包装, 真类挂在 __wrapped__
    return getattr(w, "__wrapped__", w)


def _df(dates):
    return pd.DataFrame({
        "code": ["BTC/USDT"] * len(dates),
        "date": pd.to_datetime(dates),
        "open": 1.0, "close": 1.0, "high": 1.0, "low": 1.0, "volume": 1.0,
    })


class _FakeDB:
    def __init__(self, df):
        self._df = df
        self.inserted = None

    def klines(self, code, frequency, args=None):
        return self._df.copy()

    def insert_klines(self, code, frequency, klines):
        self.inserted = klines


@pytest.mark.parametrize(
    "cls", [ExchangeBinance, ExchangeBinanceSpot], ids=["futures", "spot"]
)
def test_single_row_db_cache_does_not_crash_and_still_increments(cls):
    ex = object.__new__(_real_cls(cls))
    ex.db_exchange = _FakeDB(_df(["2026-07-01 00:00:00"]))
    ex.increment_klines_by_online = (
        lambda code, frequency, start_date=None, args=None: _df(
            ["2026-07-01 00:00:00", "2026-07-02 00:00:00", "2026-07-03 00:00:00"]
        )
    )
    out = ex.klines("BTC/USDT", "d")
    assert out is not None
    assert len(out) == 3
    assert ex.db_exchange.inserted is not None


@pytest.mark.parametrize(
    "cls", [ExchangeBinance, ExchangeBinanceSpot], ids=["futures", "spot"]
)
def test_two_rows_db_cache_increment_start_unchanged(cls):
    # 回归保护: >=2 根时增量起点仍为倒数第二根(未收盘末根可被覆盖更新)
    ex = object.__new__(_real_cls(cls))
    ex.db_exchange = _FakeDB(_df(["2026-07-01 00:00:00", "2026-07-02 00:00:00"]))
    seen = {}

    def _fake_inc(code, frequency, start_date=None, args=None):
        seen["start_date"] = start_date
        return _df(["2026-07-02 00:00:00", "2026-07-03 00:00:00"])

    ex.increment_klines_by_online = _fake_inc
    out = ex.klines("BTC/USDT", "d")
    assert out is not None
    assert seen["start_date"] == "2026-07-01 00:00:00"
