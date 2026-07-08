"""cl_object_cache 增量复用对"中途历史根订正"的防护(对齐 kline_recompute 全前缀指纹)。

bug: _compute_kline_signature 只采样一根 ref K 线(ref_idx=min(50,n//4)),
_is_extending_signature 只查 ref 5 元组不变即判"末段追加"。上游订正非采样位置的历史 K 线
+ 追加新 bar 时被误判为增量,process_klines 只追加尾段、不更新被订正的中间根 → 产出与
全量重算分叉且无告警。姊妹机制 kline_recompute._cl_pool 已用全前缀指纹修复同类"H1"。
"""
import pandas as pd
import pytest

from cl_app.services.cl_object_cache import get_or_compute_cl, stats, clear_all


def _klines_df(ts, prices):
    return pd.DataFrame(
        {
            "date": pd.to_datetime(list(ts), unit="s", utc=True),
            "open": list(prices),
            "high": [p + 0.5 for p in prices],
            "low": [p - 0.5 for p in prices],
            "close": list(prices),
            "volume": [1000] * len(ts),
        }
    )


class _FakeCL:
    instances = []

    def __init__(self, *a, **k):
        _FakeCL.instances.append(self)
        self.klines = None

    def process_klines(self, klines):
        self.klines = klines


@pytest.fixture
def mock_cl(monkeypatch):
    _FakeCL.instances = []
    monkeypatch.setattr("chanlun.core.cl.CL", _FakeCL)
    clear_all()
    yield
    clear_all()


def test_midhistory_revision_forces_full_rebuild(mock_cl):
    """订正非采样位置的历史根(+追加新 bar)不应被判为末段追加,须全量重建。"""
    n = 60
    ts = [1000 + i * 60 for i in range(n)]
    prices = [10.0 + i * 0.1 for i in range(n)]
    get_or_compute_cl("us", "T", "5m", None, _klines_df(ts, prices))  # full #1

    # 第 30 根:ref_idx=min(50,60//4=15)=15 → 非采样点;末根=59 → 非末根。
    rev = list(prices)
    rev[30] = 999.0
    ts2 = ts + [1000 + n * 60, 1000 + (n + 1) * 60]
    prices2 = rev + [16.0, 16.1]
    get_or_compute_cl("us", "T", "5m", None, _klines_df(ts2, prices2))

    s = stats()
    assert s["incremental_extends"] == 0, f"中途订正被误判为末段追加: {s}"


def test_pure_append_still_incremental(mock_cl):
    """回归:纯追加新 bar(历史不变)仍走增量复用,不因新防护退化为全量。"""
    n = 60
    ts = [1000 + i * 60 for i in range(n)]
    prices = [10.0 + i * 0.1 for i in range(n)]
    get_or_compute_cl("us", "T", "5m", None, _klines_df(ts, prices))  # full #1
    ts2 = ts + [1000 + n * 60]
    get_or_compute_cl("us", "T", "5m", None, _klines_df(ts2, prices + [16.0]))

    s = stats()
    assert s["incremental_extends"] == 1, f"纯追加应走增量复用: {s}"


def test_invalidate_prefix_includes_source_fingerprint():
    """web-B3: invalidate(cl_config=None) 前缀须含 source_fingerprint 段, 否则与真实 key
    (VER|fingerprint|market|code|freq|hash)的 startswith 恒不匹配 -> 退化为死 no-op。"""
    from cl_app.services.cl_object_cache import (
        _build_cache_key,
        _cl_object_cache,
        invalidate,
    )

    clear_all()
    key = _build_cache_key("us", "AAPL.US", "30m", {"a": 1})
    _cl_object_cache[key] = object()
    try:
        dropped = invalidate("us", "AAPL.US", "30m", cl_config=None)
        assert dropped == 1, "前缀不匹配 -> invalidate 没清掉任何 entry(死 no-op)"
        assert key not in _cl_object_cache
    finally:
        clear_all()
