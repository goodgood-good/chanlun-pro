"""R4-C(D4): tdx_us/fx/ny_futures 缓存读分支缺 len==0 判定。

get_tdx_klines 对恰 1 根缓存做 iloc[0:-1] 丢末根→返回空(非None)DataFrame;三处 klines()
原只判 `if X is None` 遂进 else 增量分支, `klines.iloc[-1]["date"]` 抛 IndexError→except
返 None→@retry 三次→RetryError, 该 symbol+周期图表持续无数据。修复=空 df 也走全拉分支
(镜像 exchange_tdx.py:222 正确写法), 全拉为空则干净返回空 df 而非崩。
本测试: 空缓存 + 空全拉 → 应干净返回空 DataFrame(非 None、不抛 RetryError)。
"""

import pandas as pd

import chanlun.exchange.exchange_tdx_us as us_mod
import chanlun.exchange.exchange_tdx_fx as fx_mod
import chanlun.exchange.exchange_tdx_ny_futures as ny_mod
from chanlun.exchange.exchange_tdx_us import ExchangeTDXUS
from chanlun.exchange.exchange_tdx_fx import ExchangeTDXFX
from chanlun.exchange.exchange_tdx_ny_futures import ExchangeTDXNYFutures
import chanlun.exchange.exchange_tdx_hk as hk_mod
from chanlun.exchange.exchange_tdx_hk import ExchangeTDXHK


class _EmptyConn:
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _EmptyPullApi:
    def __init__(self, *a, **kw):
        pass

    def connect(self, ip, port):
        return _EmptyConn()

    def get_instrument_bars(self, *a, **kw):
        return []

    def to_df(self, raw):
        return pd.DataFrame([])


class _EmptyCacheFdb:
    # 模拟 1 根缓存被 get_tdx_klines 丢末根后归零: 返回空但非 None
    def get_tdx_klines(self, market, code, freq):
        return pd.DataFrame([])

    def save_tdx_klines(self, *a, **kw):
        pass


def _real_cls(maybe_wrapped):
    return getattr(maybe_wrapped, "__wrapped__", maybe_wrapped)


def _mk(cls, mod, monkeypatch):
    monkeypatch.setattr(mod, "TdxExHq_API", _EmptyPullApi)
    ex = object.__new__(_real_cls(cls))
    ex.connect_info = {"ip": "127.0.0.1", "port": 7727}
    ex.to_tdx_code = lambda code: (33, "TESTCODE")
    ex.fdb = _EmptyCacheFdb()
    return ex


def test_tdx_us_empty_cache_returns_empty_not_crash(monkeypatch):
    ex = _mk(ExchangeTDXUS, us_mod, monkeypatch)
    r = ex.klines("AAPL.US", "5m", args={"pages": 1})
    assert r is not None  # 旧代码走 else→IndexError→None→RetryError
    assert isinstance(r, pd.DataFrame) and len(r) == 0


def test_tdx_fx_empty_cache_returns_empty_not_crash(monkeypatch):
    ex = _mk(ExchangeTDXFX, fx_mod, monkeypatch)
    r = ex.klines("EURUSD.FX", "5m", args={"pages": 1})
    assert r is not None
    assert isinstance(r, pd.DataFrame) and len(r) == 0


def test_tdx_ny_futures_empty_cache_returns_empty_not_crash(monkeypatch):
    ex = _mk(ExchangeTDXNYFutures, ny_mod, monkeypatch)
    r = ex.klines("CL.NYF", "5m", args={"pages": 1})
    assert r is not None
    assert isinstance(r, pd.DataFrame) and len(r) == 0


def test_tdx_hk_empty_cache_returns_empty_not_crash(monkeypatch):
    """R1-F3-2: hk 是六兄弟中唯一漏 R4-C 守卫的(仅判 is None)。"""
    ex = _mk(ExchangeTDXHK, hk_mod, monkeypatch)
    r = ex.klines("HK.00700", "5m", args={"pages": 1})
    assert r is not None  # 旧代码: 空df过 is None→增量分支 iloc[-1] IndexError→None→RetryError
    assert isinstance(r, pd.DataFrame) and len(r) == 0
