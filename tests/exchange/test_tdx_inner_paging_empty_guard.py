"""Round10: tdx_fx/futures/ny_futures/us/hk 增量(else,有缓存)分页循环缺内层空页守卫。

有缓存→进 else 增量分支→`for i in range(1,pages+1)` 逐页向前拉;某页返回空 df 时
`_ks.loc[:,"date"]=pd.to_datetime(_ks["datetime"])`(空df无列→KeyError)或 `_ks.iloc[0]`(0行→
IndexError)崩溃→被 except 吞→return None→@retry×3→RetryError,该 symbol+周期图表持续无数据
不自愈(save 在崩溃点之后,缓存永不更新)。A股参照 exchange_tdx.py:255 有 `if len(_ks)==0: break`,
5 个 tdx 兄弟漏。区别于 test_tdx_cache_empty_guard(R4-C, 无缓存 outer 分支)——本项在 cache-exists 的
else 内层分页循环,独立触发。

本测试: 非空缓存 + 空页 → 应干净返回缓存(非 None、不抛 RetryError); 且 5 文件均含内层守卫。
"""
import re
from pathlib import Path

import pandas as pd
import pytz

import chanlun.exchange.exchange_tdx_fx as fx_mod
from chanlun.exchange.exchange_tdx_fx import ExchangeTDXFX


class _EmptyConn:
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _EmptyPagePullApi:
    """首页即返回空 df: 模拟分页越过可用历史 / 已到期合约 offset=0 返空。"""

    def __init__(self, *a, **kw):
        pass

    def connect(self, ip, port):
        return _EmptyConn()

    def get_instrument_bars(self, *a, **kw):
        return []

    def to_df(self, raw):
        return pd.DataFrame([])


class _NonEmptyCacheFdb:
    def __init__(self, df):
        self._df = df

    def get_tdx_klines(self, market, code, freq):
        return self._df.copy()

    def save_tdx_klines(self, *a, **kw):
        pass


def _real_cls(maybe_wrapped):
    return getattr(maybe_wrapped, "__wrapped__", maybe_wrapped)


def _fx_cache():
    dts = ["2026-01-05 09:30:00", "2026-01-05 09:35:00"]
    return pd.DataFrame(
        {
            "datetime": dts,
            "date": pd.to_datetime(dts),
            "open": [1.10, 1.11],
            "high": [1.12, 1.13],
            "low": [1.09, 1.10],
            "close": [1.11, 1.12],
            "trade": [100.0, 200.0],
        }
    )


def test_tdx_fx_empty_page_in_incremental_branch_no_crash(monkeypatch):
    monkeypatch.setattr(fx_mod, "TdxExHq_API", _EmptyPagePullApi)
    ex = object.__new__(_real_cls(ExchangeTDXFX))
    ex.connect_info = {"ip": "127.0.0.1", "port": 7727}
    ex.to_tdx_code = lambda code: (33, "TESTFX")
    ex.tz = pytz.timezone("Asia/Shanghai")
    ex.fdb = _NonEmptyCacheFdb(_fx_cache())
    # 非空缓存→进 else 增量分支;首页空→内层守卫 break→干净返回缓存(2 根)
    r = ex.klines("EURUSD.FX", "5m", args={"pages": 3})
    assert r is not None  # 旧代码: 空页→KeyError→None→@retry→RetryError
    assert isinstance(r, pd.DataFrame)
    assert len(r) == 2


def test_all_five_tdx_incremental_branches_have_empty_guard():
    files = [
        "exchange_tdx_fx",
        "exchange_tdx_futures",
        "exchange_tdx_ny_futures",
        "exchange_tdx_us",
        "exchange_tdx_hk",
    ]
    base = Path("src/chanlun/exchange")
    for name in files:
        src = (base / (name + ".py")).read_text(encoding="utf-8")
        assert re.search(r"if len\(_ks\) == 0:\s*\n\s*break", src), (
            name + " 缺内层空页守卫(镜像 exchange_tdx.py:255)"
        )


import chanlun.exchange.exchange_tdx_futures as fut_mod
from chanlun.exchange.exchange_tdx_futures import ExchangeTDXFutures


def _futures_cache():
    dts = ["2026-01-05 09:30:00", "2026-01-05 09:35:00"]
    return pd.DataFrame(
        {
            "datetime": dts,
            "date": pd.to_datetime(dts),
            "open": [550.0, 551.0],
            "high": [552.0, 553.0],
            "low": [549.0, 550.0],
            "close": [551.0, 552.0],
            "trade": [100.0, 200.0],
        }
    )


def test_tdx_futures_empty_page_guard_before_column_access(monkeypatch):
    """R1-F3-3: futures 守卫(R10)插在 fix_datetime 取列之后 → 空页先 KeyError,守卫不可达。"""
    monkeypatch.setattr(fut_mod, "TdxExHq_API", _EmptyPagePullApi)
    ex = object.__new__(_real_cls(ExchangeTDXFutures))
    ex.connect_info = {"ip": "127.0.0.1", "port": 7727}
    ex.to_tdx_code = lambda code: (30, "TESTFUT")
    ex.tz = pytz.timezone("Asia/Shanghai")
    ex.fdb = _NonEmptyCacheFdb(_futures_cache())
    r = ex.klines("SHFE.AU2412", "5m", args={"pages": 3})
    assert r is not None  # 旧代码: 空页 _ks["datetime"] KeyError→None→@retry→RetryError
    assert isinstance(r, pd.DataFrame)
    assert len(r) == 2
