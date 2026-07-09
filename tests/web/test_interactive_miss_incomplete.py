"""D3-M2 回归: 交互 miss 路径(fetch_klines_and_compute_cl_data)拉取带洞不得误标 validated。

后台路径 compute_and_cache_chart_data 已判 _klines_fetch_incomplete(带洞->短退避不标 validated),
但交互 miss 路径对 klines is None or len==0 一律 _mark_chart_cache_validated。cq 带洞(空 DF +
attrs['fetch_incomplete']=True)被当"确认无数据"标 fresh -> 下次 firstDataRequest 把陈旧/带洞缠论
当新鲜下发, 抑制 C1/M3 的 30s 自愈与 force_refresh。修复: 交互路径镜像后台先判带洞、带洞则不标。
"""
import pandas as pd

import cl_app.services.chart_compute as cc_mod


def _inc_df():
    df = pd.DataFrame()
    df.attrs["fetch_incomplete"] = True
    return df


def _call(monkeypatch, ex_ret, cache_key):
    calls = []
    monkeypatch.setattr(cc_mod, "_mark_chart_cache_validated", lambda key: calls.append(key))

    class _Ex:
        def klines(self, *a, **kw):
            return ex_ret

    monkeypatch.setattr(cc_mod, "get_exchange", lambda m: _Ex())
    res = cc_mod.fetch_klines_and_compute_cl_data(
        "a", "SH.600519", "5m", {}, {"end_date": "2026-07-08 00:00:00"},
        False, "cache_empty", cache_key, 0,
    )
    return res, calls


def test_incomplete_does_not_mark_validated(monkeypatch):
    res, calls = _call(monkeypatch, _inc_df(), "k_inc")
    assert res is None
    assert calls == [], f"带洞(fetch_incomplete)被误标 validated, 污染新鲜度: {calls}"


def test_true_empty_does_not_mark_validated(monkeypatch):
    # R5-#4 supersedes D3-M2 的"真空标 validated": _mark_chart_cache_validated 仅在 entry
    # 存在时生效, 空拉取标记只会命中"重置既存陈旧快照 validated_at→旧缠论当 fresh"这一有害情形
    # (非cq源如 qmt 瞬时劣化返[]无 fetch_incomplete 标记)。新股/退市无 entry 时本就是 no-op,
    # 不标不影响;有 entry 时不标才能让 too_stale 判定继续生效自愈。
    res, calls = _call(monkeypatch, pd.DataFrame(), "k_empty")
    assert res is None
    assert calls == [], f"空拉取不得标 validated(会重置既存陈旧快照): {calls}"