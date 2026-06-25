"""Task5: decide_push 指纹变化判定(纯逻辑)。"""
from cl_app.services.sse_refresh import decide_push


def test_decide_push_first_time():
    push, sig = decide_push(None, {"t": [1]})
    assert push is True
    assert isinstance(sig, str)


def test_decide_push_unchanged():
    cd = {"t": [1, 2], "bis": []}
    _, s1 = decide_push(None, cd)
    push, s2 = decide_push(s1, cd)
    assert push is False
    assert s2 == s1


def test_decide_push_changed():
    _, s1 = decide_push(None, {"t": [1]})
    push, s2 = decide_push(s1, {"t": [1, 2]})
    assert push is True
    assert s2 != s1


def test_recompute_skips_low_to_high(monkeypatch):
    """low-to-high 合成标的:SSE 不推(返回 None,在 ex.klines 之前就跳过),交给前端 fallback
    轮询走 tv_history 的正确口径——避免 SSE 用高周期重算的缠论与首屏(低周期算)分歧。"""
    import chanlun.cl_utils as _clu
    from cl_app.services import sse_refresh
    # 模拟该 market+frequency 命中"低周期合成高周期"映射(返回非 None 的目标周期)
    monkeypatch.setattr(_clu, "kcharts_frequency_h_l_map", lambda m, f: ("5m", "30m"))
    # 记录 get_exchange 是否被触达:recompute 的 except 会吞异常,故不能靠抛错,改查"根本没调"。
    called = []
    monkeypatch.setattr(sse_refresh, "get_exchange", lambda *a, **k: called.append(1))
    out = sse_refresh.recompute_chart_data(
        "us", "AAPL.US", "30m", {"enable_kchart_low_to_high": "1"}, "ck"
    )
    assert out is None
    assert called == []  # 真的在 ex.klines 之前就跳过了(否则说明 low-to-high 没被拦)


def test_recompute_skips_negatively_cached(monkeypatch):
    """L1: 已负缓存(退市/空数据)标的,SSE 在 ex.klines 之前返回 None,不反复拉数据源。"""
    import chanlun.cl_utils as _clu
    from cl_app.services import sse_refresh, chart_cache
    monkeypatch.setattr(_clu, "kcharts_frequency_h_l_map", lambda m, f: (None, None))
    monkeypatch.setattr(chart_cache, "_is_negatively_cached", lambda k: True)
    called = []
    monkeypatch.setattr(sse_refresh, "get_exchange", lambda *a, **k: called.append(1))
    out = sse_refresh.recompute_chart_data("a", "SZ.000001", "5m", {}, "ck")
    assert out is None
    assert called == []  # 负缓存命中 → 未触达 ex.klines
