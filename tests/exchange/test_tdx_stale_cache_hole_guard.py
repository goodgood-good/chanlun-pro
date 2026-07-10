"""R9-tdx242: tdx 增量分页耗尽仍未衔接缓存 → concat(缓存+新页) 中间留洞并 save 永久落盘。

增量分支(有缓存)唯一衔接出口是 `if cache_end_dt >= new_start_dt: break`。当缓存陈旧超过
pages*700 根可达范围时, for 循环正常耗尽 pages 落出、无 bridged 标记 → concat(缓存+新页) 后
drop_duplicates+sort 不报错, (cache_end, 最旧新页) 区间 bar 全缺 → save_tdx_klines 把带洞 df
永久落盘。此后每次读回带洞缓存、增量只衔接新端, 中段洞永不修复; save 刷新 mtime 使 15 天清理
对活跃标的永不生效 → 洞永久。fx/futures/ny_futures 默认可达(web 注册 + 默认 EXCHANGE_*=tdx_*)。
修复=收集 fresh_pages 分离, 循环后判 bridged: 衔接则合并(happy-path 字节等价)/未衔接则弃陈旧
缓存只保留连续新页 + warning。6 文件同构。
"""
import pandas as pd
import pytz

import chanlun.exchange.exchange_tdx_fx as fx_mod
from chanlun.exchange.exchange_tdx_fx import ExchangeTDXFX


class _Conn:
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _StaleGapPullApi:
    """拉取页全是近期(2026-06-01)且页内+跨页连续 5min; 缓存远古(2026-01-05) →
    new_start 恒 > cache_end, pages 耗尽永不衔接 → 触发 un-bridged 留洞路径。"""
    _PAGES = {
        0: ["2026-06-01 09:50:00", "2026-06-01 09:55:00"],
        700: ["2026-06-01 09:40:00", "2026-06-01 09:45:00"],
        1400: ["2026-06-01 09:30:00", "2026-06-01 09:35:00"],
    }

    def __init__(self, *a, **kw):
        pass

    def connect(self, ip, port):
        return _Conn()

    def get_instrument_bars(self, freq, market, code, offset, count):
        return self._PAGES.get(offset, [])

    def to_df(self, raw):
        if not raw:
            return pd.DataFrame([])
        return pd.DataFrame({
            "datetime": raw, "open": 1.0, "high": 1.1, "low": 0.9, "close": 1.0, "trade": 100.0,
        })


class _StaleCacheFdb:
    def __init__(self, df):
        self._df = df
        self.saved = None

    def get_tdx_klines(self, market, code, freq):
        return self._df.copy()

    def save_tdx_klines(self, market, code, freq, klines):
        self.saved = klines.copy()


def _real_cls(w):
    return getattr(w, "__wrapped__", w)


def _stale_cache():
    dts = ["2026-01-05 09:30:00", "2026-01-05 09:35:00"]
    return pd.DataFrame({
        "datetime": dts, "date": pd.to_datetime(dts),
        "open": [1.10, 1.11], "high": [1.12, 1.13], "low": [1.09, 1.10],
        "close": [1.11, 1.12], "trade": [100.0, 200.0],
    })


def _max_gap_minutes(df):
    d = pd.to_datetime(df["date"]).sort_values()
    if len(d) < 2:
        return 0.0
    return d.diff().dropna().max().total_seconds() / 60.0


def _mk_fx():
    ex = object.__new__(_real_cls(ExchangeTDXFX))
    ex.connect_info = {"ip": "127.0.0.1", "port": 7727}
    ex.to_tdx_code = lambda code: (33, "TESTFX")
    ex.tz = pytz.timezone("Asia/Shanghai")
    ex.fdb = _StaleCacheFdb(_stale_cache())
    return ex


def test_tdx_fx_stale_cache_unbridged_no_hole(monkeypatch):
    monkeypatch.setattr(fx_mod, "TdxExHq_API", _StaleGapPullApi)
    ex = _mk_fx()
    r = ex.klines("EURUSD.FX", "5m", args={"pages": 3})
    assert r is not None and len(r) >= 2
    # 修复前: 缓存(1月)+新页(6月) concat 留洞, max_gap ~5 个月; 修复后弃陈旧缓存 → 连续 5min
    assert _max_gap_minutes(r) <= 5.0, (
        f"返回结果有洞 max_gap={_max_gap_minutes(r)}min (陈旧缓存未被丢弃)"
    )
    assert ex.fdb.saved is not None
    assert _max_gap_minutes(ex.fdb.saved) <= 5.0, "save 落盘 df 有洞 → 永久污染缓存"
    saved_days = set(pd.to_datetime(ex.fdb.saved["date"]).dt.strftime("%Y-%m-%d"))
    assert "2026-01-05" not in saved_days, "陈旧缓存日期仍在落盘 df 中(留洞)"


def test_tdx_fx_bridged_happy_path_merges_cache(monkeypatch):
    """回归保护: 衔接成功(新页起点 <= 缓存末尾)时仍合并缓存+新页(happy-path 不变)。"""
    class _BridgeApi(_StaleGapPullApi):
        _PAGES = {
            0: ["2026-01-05 09:35:00", "2026-01-05 09:40:00", "2026-01-05 09:45:00"],
        }

    monkeypatch.setattr(fx_mod, "TdxExHq_API", _BridgeApi)
    ex = _mk_fx()
    r = ex.klines("EURUSD.FX", "5m", args={"pages": 3})
    assert r is not None
    saved_days = set(pd.to_datetime(ex.fdb.saved["date"]).dt.strftime("%Y-%m-%d"))
    assert saved_days == {"2026-01-05"}, "衔接 happy-path 应保留缓存合并"
    assert len(ex.fdb.saved) == 4  # 缓存 09:30/09:35 + 新页 09:40/09:45 (09:35 dedup)
    assert _max_gap_minutes(ex.fdb.saved) <= 5.0

import chanlun.exchange.exchange_tdx_futures as fut_mod  # noqa: E402
from chanlun.exchange.exchange_tdx_futures import ExchangeTDXFutures  # noqa: E402
import chanlun.exchange.exchange_tdx_ny_futures as ny_mod  # noqa: E402
from chanlun.exchange.exchange_tdx_ny_futures import ExchangeTDXNYFutures  # noqa: E402


def test_tdx_futures_stale_cache_unbridged_no_hole(monkeypatch):
    monkeypatch.setattr(fut_mod, "TdxExHq_API", _StaleGapPullApi)
    ex = object.__new__(_real_cls(ExchangeTDXFutures))
    ex.connect_info = {"ip": "127.0.0.1", "port": 7727}
    ex.to_tdx_code = lambda code: (30, "TESTFUT")
    ex.tz = pytz.timezone("Asia/Shanghai")
    ex.fix_yp_date = lambda code, dt: dt
    ex.fdb = _StaleCacheFdb(_stale_cache())
    r = ex.klines("SHFE.AU2412", "5m", args={"pages": 3})
    assert r is not None
    assert ex.fdb.saved is not None
    assert _max_gap_minutes(ex.fdb.saved) <= 5.0, "futures save 有洞(陈旧缓存未弃)"
    assert "2026-01-05" not in set(
        pd.to_datetime(ex.fdb.saved["date"]).dt.strftime("%Y-%m-%d")
    )


def test_tdx_ny_futures_stale_cache_unbridged_no_hole(monkeypatch):
    monkeypatch.setattr(ny_mod, "TdxExHq_API", _StaleGapPullApi)
    ex = object.__new__(_real_cls(ExchangeTDXNYFutures))
    ex.connect_info = {"ip": "127.0.0.1", "port": 7727}
    ex.to_tdx_code = lambda code: (30, "TESTNY")
    ex.tz = pytz.timezone("Asia/Shanghai")
    ex.fix_yp_date = lambda code, dt: dt
    ex.fdb = _StaleCacheFdb(_stale_cache())
    r = ex.klines("CME.ES", "5m", args={"pages": 3})
    assert r is not None
    assert ex.fdb.saved is not None
    assert _max_gap_minutes(ex.fdb.saved) <= 5.0, "ny_futures save 有洞(陈旧缓存未弃)"
    assert "2026-01-05" not in set(
        pd.to_datetime(ex.fdb.saved["date"]).dt.strftime("%Y-%m-%d")
    )

def test_all_six_tdx_files_have_unbridged_hole_guard():
    """R9-tdx242: 6 个 tdx 源(fx/futures/ny/tdx/hk/us)增量分支均须有 un-bridged 留洞守卫
    (_fresh_pages 分离 + _bridged 判定 + 弃陈旧缓存分支), 且保留 R10 空页 break 守卫。"""
    import re
    import pathlib
    base = pathlib.Path("src/chanlun/exchange")
    files = ["exchange_tdx_fx", "exchange_tdx_futures", "exchange_tdx_ny_futures",
             "exchange_tdx", "exchange_tdx_hk", "exchange_tdx_us"]
    for name in files:
        src = (base / (name + ".py")).read_text(encoding="utf-8")
        assert "_fresh_pages" in src and "_bridged" in src, name + " 缺 un-bridged 留洞守卫"
        assert "增量分页耗尽未衔接缓存" in src, name + " 缺弃陈旧缓存告警"
        assert re.search(r"if len\(_ks\) == 0:\s*\n\s*break", src), name + " 丢了 R10 空页守卫"