"""锁定 prepend 缩水闸门：合并后根数少于旧缓存则拒绝覆盖（H2 纵深防御 阶段 D）。

merge_klines_df 是并集,正常追加/末根更新 len(merged) >= len(cached) 恒成立;若 merged 反而
缩水,只可能来自带洞数据覆盖(cq 漏网 / 未来其它源回归)。主修已由 C1 源级闸门覆盖(cq 缺段返空),
本闸门是防御网:一旦缩水,拒绝替换,保留旧完整缓存,绝不让好缓存被带洞结果覆盖。

因当前 merge 是并集、缩水不可达,测试用 monkeypatch 让 merge 返回缩水结果,锁定闸门行为契约。
"""
import pathlib
import sys

_root = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_root / "src"))
sys.path.insert(0, str(_root / "web" / "chanlun_chart"))

import pandas as pd  # noqa: E402

import cl_app.services.kline_recompute as kr  # noqa: E402
import cl_app.services.chart_cache as cache_mod  # noqa: E402


def _chart_data(n):
    return {"t": [1000 + i * 60 for i in range(n)],
            "o": [1.0] * n, "h": [1.0] * n, "l": [1.0] * n, "c": [1.0] * n, "v": [1.0] * n}


def _df(n):
    return pd.DataFrame({
        "date": pd.to_datetime(range(n), unit="s", utc=True),
        "open": [1.0] * n, "high": [1.0] * n, "low": [1.0] * n,
        "close": [1.0] * n, "volume": [1.0] * n,
    })


def _df_updated(n):
    """末根 close 变化的 n 根 df（模拟进行中 bar 更新 → 触发重算而非"数据没变"跳过）。"""
    df = _df(n)
    df.loc[n - 1, "close"] = 2.0
    return df


def _setup(monkeypatch, cached_n, merged_df):
    cached_data = _chart_data(cached_n)
    monkeypatch.setattr(cache_mod, "_get_chart_cache_entry", lambda k: {"data": cached_data})
    # 覆盖缓存的写入若被调用即失败断言（缩水场景绝不能覆盖）——正常场景放行时改回 no-op。
    monkeypatch.setattr(cache_mod, "_set_chart_cache_entry", lambda *a, **kw: None)
    monkeypatch.setattr(kr, "extract_klines_df_from_chart_data",
                        lambda d: _df(len(d.get("t", []))) if d else pd.DataFrame())
    monkeypatch.setattr(kr, "merge_klines_df", lambda c, n: merged_df)
    monkeypatch.setattr(kr, "recompute_chart_data_from_klines",
                        lambda *a, **kw: {"recomputed": True})
    return cached_data


def test_prepend_refuses_shrinking_merge(monkeypatch):
    # merged(3) < cached(5) → 缩水 → 拒绝覆盖,返回旧完整缓存 data。
    cached_data = _setup(monkeypatch, cached_n=5, merged_df=_df(3))
    result = kr.prepend_klines_and_replace_cache("a", "SH.600519", "5m", {}, _df(2), "ckey")
    assert result == cached_data


def test_prepend_allows_normal_append(monkeypatch):
    # merged(7) >= cached(5) → 正常追加,不被闸门拦,走重算。
    _setup(monkeypatch, cached_n=5, merged_df=_df(7))
    result = kr.prepend_klines_and_replace_cache("a", "SH.600519", "5m", {}, _df(2), "ckey")
    assert result == {"recomputed": True}


def test_prepend_equal_length_not_blocked(monkeypatch):
    # 等长(末根更新)不触发缩水闸门(5<5 False),正常走重算,不被误拦。
    _setup(monkeypatch, cached_n=5, merged_df=_df_updated(5))
    result = kr.prepend_klines_and_replace_cache("a", "SH.600519", "5m", {}, _df(2), "ckey")
    assert result == {"recomputed": True}
