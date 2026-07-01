"""锁定 cq 拉取不完整时 web 层走短退避而非真空 5min 负缓存（C1+M3 协同 阶段 C）。

exchange_cq 缺段时返回空 DataFrame 且 attrs['fetch_incomplete']=True(阶段B)。web 层两处有
300s 负缓存的调用点(compute_and_cache_chart_data / sse_refresh.recompute_chart_data)必须区分:
- 异常空(fetch_incomplete) → _mark_negative_cache(ttl=30) 短退避,≤30s 自愈,保留旧完整缓存不写;
- 真空(普通空,新股/退市) → _mark_negative_cache() 300s(现状,防无限重拉)。
否则数据源暂时失败会被当真空 5min 抑制,SSE/轮询 5 分钟不自愈(M3,比带洞更糟)。
"""
import pathlib
import sys

_root = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_root / "src"))
sys.path.insert(0, str(_root / "web" / "chanlun_chart"))

import pandas as pd  # noqa: E402

import cl_app.services.chart_cache as cache_mod  # noqa: E402
import cl_app.services.chart_compute as cc_mod  # noqa: E402
import cl_app.services.sse_refresh as sr_mod  # noqa: E402


def _inc_df():
    df = pd.DataFrame()
    df.attrs["fetch_incomplete"] = True
    return df


# ---------------- helper 纯函数 ----------------

def test_helper_detects_incomplete():
    assert cache_mod._klines_fetch_incomplete(_inc_df()) is True


def test_helper_normal_empty_is_not_incomplete():
    assert cache_mod._klines_fetch_incomplete(pd.DataFrame()) is False


def test_helper_none_is_not_incomplete():
    assert cache_mod._klines_fetch_incomplete(None) is False


def test_helper_data_df_is_not_incomplete():
    assert cache_mod._klines_fetch_incomplete(pd.DataFrame({"a": [1]})) is False


# ---------------- compute_and_cache_chart_data 分流（模块级 import _mark_negative_cache）----------------

def _patch_compute(monkeypatch, ex_ret):
    spy = []
    monkeypatch.setattr(cc_mod, "_is_negatively_cached", lambda key: False)
    monkeypatch.setattr(
        cc_mod, "_mark_negative_cache",
        lambda key, ttl=cache_mod._NEGATIVE_CACHE_TTL_SECONDS: spy.append(ttl),
    )

    class _Ex:
        def klines(self, *a, **kw):
            return ex_ret

    monkeypatch.setattr(cc_mod, "get_exchange", lambda m: _Ex())
    return spy


def test_compute_incomplete_uses_short_backoff(monkeypatch):
    spy = _patch_compute(monkeypatch, _inc_df())
    cc_mod.compute_and_cache_chart_data("a", "SH.600519", "5m", {})
    assert spy == [cache_mod._TRANSIENT_NEGATIVE_TTL_SECONDS]  # 30s，不是 300


def test_compute_true_empty_uses_300s(monkeypatch):
    spy = _patch_compute(monkeypatch, pd.DataFrame())  # 真空
    cc_mod.compute_and_cache_chart_data("a", "SH.600519", "5m", {})
    assert spy == [cache_mod._NEGATIVE_CACHE_TTL_SECONDS]  # 300s 现状


# ---------------- sse_refresh.recompute_chart_data 分流（函数内局部 import）----------------

def _patch_sse(monkeypatch, ex_ret):
    spy = []
    # recompute_chart_data 内 `from .chart_cache import ...`，patch 源模块即被局部 import 取到。
    monkeypatch.setattr(cache_mod, "_is_negatively_cached", lambda key: False)
    monkeypatch.setattr(
        cache_mod, "_mark_negative_cache",
        lambda key, ttl=cache_mod._NEGATIVE_CACHE_TTL_SECONDS: spy.append(ttl),
    )

    class _Ex:
        def klines(self, *a, **kw):
            return ex_ret

    monkeypatch.setattr(sr_mod, "get_exchange", lambda m: _Ex())
    return spy


def test_sse_refresh_incomplete_short_backoff(monkeypatch):
    spy = _patch_sse(monkeypatch, _inc_df())
    sr_mod.recompute_chart_data("a", "SH.600519", "5m", {}, "cachekey_inc")
    assert spy == [cache_mod._TRANSIENT_NEGATIVE_TTL_SECONDS]  # 30s


def test_sse_refresh_true_empty_uses_300s(monkeypatch):
    spy = _patch_sse(monkeypatch, pd.DataFrame())  # 真空
    sr_mod.recompute_chart_data("a", "SH.600519", "5m", {}, "cachekey_empty")
    assert spy == [cache_mod._NEGATIVE_CACHE_TTL_SECONDS]  # 300s
