"""R9-binance270: increment_klines_by_online 无数据返回 None 被直接喂 insert_klines。

exchange_binance.py:270-271 在 len(all_klines)==0 时 return None; 两处调用方
klines() L138(冷路径 db 空)与 L149(增量路径 db 有历史)不判 None 直接
insert_klines(None)→db.klines_insert 首行 klines.empty 对 None 抛 AttributeError
→被 klines() 的 except 吞成 print+return None→@retry(retry_if_result is None)×3
全败→tenacity RetryError 抛给调用方。触发态: DB 有历史行但 binance 已不再返回该
交易对数据(合约退市/下架/永续定期退市)——增量 startTime 起零 bar; 或冷路径遇全新
无数据 symbol。web 默认 UI currency 市场默认源 EXCHANGE_CURRENCY='binance', 用户
自选组含已退市交易对时每次打开该标的图表→RetryError。exchange_binance_spot.py 同构。
修复=两处对 None/空 df 守卫: 冷路径返回空 DataFrame, 增量路径用库中现有数据兜底,
与 tdx 家族'如实返回现有数据'口径一致。
"""
import sys
import types

import pandas as pd
import pytest

if "ccxt" not in sys.modules:
    sys.modules["ccxt"] = types.ModuleType("ccxt")

from chanlun.exchange.exchange_binance import ExchangeBinance  # noqa: E402
from chanlun.exchange.exchange_binance_spot import ExchangeBinanceSpot  # noqa: E402


def _real_cls(w):
    return getattr(w, "__wrapped__", w)


_COLS = ["code", "date", "open", "close", "high", "low", "volume"]


def _df(dates):
    return pd.DataFrame({
        "code": ["BTC/USDT"] * len(dates),
        "date": pd.to_datetime(dates),
        "open": 1.0, "close": 1.0, "high": 1.0, "low": 1.0, "volume": 1.0,
    })


def _empty_df():
    return pd.DataFrame(columns=_COLS)


class _FakeDB:
    def __init__(self, df):
        self._df = df

    def klines(self, code, frequency, args=None):
        return self._df.copy()

    def insert_klines(self, code, frequency, klines):
        # 复刻生产 db.klines_insert 首行 klines.empty: 若守卫失效被喂 None 则 AttributeError
        # (证明修复真的挡住了 None, 而非测试恰好不触发)
        _ = klines.empty


@pytest.mark.parametrize("cls", [ExchangeBinance, ExchangeBinanceSpot], ids=["futures", "spot"])
def test_empty_online_cold_path_returns_empty_df(cls):
    # db 空(冷路径) + 在线无数据 None → 优雅返回空 DataFrame, 不 None(→RetryError)不崩
    ex = object.__new__(_real_cls(cls))
    ex.db_exchange = _FakeDB(_empty_df())
    ex.increment_klines_by_online = (
        lambda code, frequency, start_date=None, args=None: None
    )
    out = ex.klines("DEAD/USDT", "d")
    assert out is not None, "退市交易对冷路径应返回空DataFrame而非None(None→RetryError)"
    assert isinstance(out, pd.DataFrame)
    assert len(out) == 0


@pytest.mark.parametrize("cls", [ExchangeBinance, ExchangeBinanceSpot], ids=["futures", "spot"])
def test_empty_online_incremental_returns_existing(cls):
    # db 有历史 + 增量无新数据 None → 用库中现有数据兜底, 不喂 None 给 insert
    ex = object.__new__(_real_cls(cls))
    ex.db_exchange = _FakeDB(_df(["2026-07-01 00:00:00", "2026-07-02 00:00:00"]))
    ex.increment_klines_by_online = (
        lambda code, frequency, start_date=None, args=None: None
    )
    out = ex.klines("DEAD/USDT", "d")
    assert out is not None, "退市交易对增量路径应返回库中现有数据而非None"
    assert isinstance(out, pd.DataFrame)
    assert len(out) == 2


@pytest.mark.parametrize("cls", [ExchangeBinance, ExchangeBinanceSpot], ids=["futures", "spot"])
def test_normal_online_still_inserts_and_returns(cls):
    # 回归保护: 有新数据时仍正常入库+返回合并结果
    ex = object.__new__(_real_cls(cls))
    ex.db_exchange = _FakeDB(_df(["2026-07-01 00:00:00", "2026-07-02 00:00:00"]))
    ex.increment_klines_by_online = (
        lambda code, frequency, start_date=None, args=None: _df(
            ["2026-07-02 00:00:00", "2026-07-03 00:00:00"]
        )
    )
    out = ex.klines("BTC/USDT", "d")
    assert out is not None
    assert len(out) == 3