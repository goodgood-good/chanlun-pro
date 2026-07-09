"""R5-#4: 空拉取(非cq源瞬时劣化返[]无fetch_incomplete标记)不得标 validated。

原空分支调 _mark_chart_cache_validated→把既存 too_stale 全量快照的 validated_at 重置为
now→下一 firstDataRequest 在 300s 内把数小时前旧缠论(含悬空未完成笔)当 fresh 下发。
修复: 空拉取只写负缓存(compute_and_cache)/直接 return(serving), 保留旧 validated_at。
"""
import pathlib
import sys

_root = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_root / "src"))
sys.path.insert(0, str(_root / "web" / "chanlun_chart"))

import pandas as pd  # noqa: E402

import cl_app.services.chart_cache as cache_mod  # noqa: E402
import cl_app.services.chart_compute as cc_mod  # noqa: E402


def _spy(monkeypatch, ex_ret):
    marks = []
    monkeypatch.setattr(cc_mod, "_is_negatively_cached", lambda key: False)
    monkeypatch.setattr(
        cc_mod, "_mark_negative_cache",
        lambda key, ttl=cache_mod._NEGATIVE_CACHE_TTL_SECONDS: None,
    )
    monkeypatch.setattr(cc_mod, "_mark_chart_cache_validated", lambda key: marks.append(key))

    class _Ex:
        def klines(self, *a, **kw):
            return ex_ret

    monkeypatch.setattr(cc_mod, "get_exchange", lambda m: _Ex())
    return marks


def test_compute_and_cache_true_empty_does_not_mark_validated(monkeypatch):
    marks = _spy(monkeypatch, pd.DataFrame())  # 真空
    ok = cc_mod.compute_and_cache_chart_data("a", "SH.600519", "5m", {})
    assert ok is False
    assert marks == []  # 旧代码空分支会 _mark_chart_cache_validated → 重置陈旧快照


def test_fetch_serving_path_empty_does_not_mark_validated(monkeypatch):
    marks = _spy(monkeypatch, pd.DataFrame())  # 真空
    r = cc_mod.fetch_klines_and_compute_cl_data(
        "a", "SH.600519", "5m", {}, {}, False, "cache_miss", "cachekey_empty", 0
    )
    assert r is None
    assert marks == []