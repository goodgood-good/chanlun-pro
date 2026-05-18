# -*- coding: utf-8 -*-
"""kline_recompute 服务单测:验证反构建/合并/重算的等价性。

核心契约:对同一组 K 线,无论是"一次性全量计算"还是"两段拼接后再重算",
得到的缠论 XD/笔/中枢序列必须完全相等。这是修复"向左滚动 XD 跳变"的核心断言。
"""
import pandas as pd
import pytest


def _make_klines(start: str, periods: int, freq: str = "1min", base_price: float = 100.0):
    """造一段 toy K 线:用正弦波 + 漂移让缠论能识别出多组分型/笔/段。

    date 列带 UTC tz——与 ex.klines() 真实返回形态一致(alpaca 直接 UTC、cq 转
    Asia/Shanghai 都是 tz-aware)。早期 toy 数据是 naive,使得 merge 路径的 tz
    bug 在单测里测不到,直到 2026-05-13 才在生产暴露 ``Cannot compare tz-naive
    and tz-aware timestamps``。
    """
    import numpy as np
    dates = pd.date_range(start=start, periods=periods, freq=freq, tz="UTC")
    t = np.arange(periods)
    closes = base_price + 5 * np.sin(t / 6.0) + t * 0.05
    highs = closes + 0.6
    lows = closes - 0.6
    opens = closes - 0.05 * np.sin(t / 3.0)
    volumes = 1000 + (t % 7) * 50
    return pd.DataFrame({
        "date": dates,
        "open": opens,
        "high": highs,
        "low": lows,
        "close": closes,
        "volume": volumes,
    })


def test_extract_klines_df_roundtrip():
    """从 chart_data 反构建出来的 K 线 DataFrame,应当与原始 K 线在 OHLCV 上一一对应。"""
    from cl_app.services.kline_recompute import extract_klines_df_from_chart_data

    src = _make_klines("2024-01-01 09:30", 30)
    chart_data = {
        "t": [int(d.timestamp()) for d in src["date"]],
        "o": src["open"].tolist(),
        "h": src["high"].tolist(),
        "l": src["low"].tolist(),
        "c": src["close"].tolist(),
        "v": src["volume"].tolist(),
    }

    df = extract_klines_df_from_chart_data(chart_data)
    assert len(df) == len(src)
    assert (df["date"].astype("int64") // 10**9 == pd.Series(chart_data["t"])).all()
    assert df["high"].tolist() == src["high"].tolist()
    assert df["low"].tolist() == src["low"].tolist()
    assert df["close"].tolist() == src["close"].tolist()


def test_merge_klines_df_left_extend_no_overlap():
    """场景:新 K 线完全在缓存 K 线左侧(向左滚动加载更早历史)。"""
    from cl_app.services.kline_recompute import merge_klines_df

    cached = _make_klines("2024-01-02 09:30", 30)  # 第二天
    new = _make_klines("2024-01-01 09:30", 30, base_price=95.0)  # 第一天

    merged = merge_klines_df(cached, new)

    # 长度 = 拼接后总数(无重叠)
    assert len(merged) == 60
    # 时间严格升序
    assert merged["date"].is_monotonic_increasing
    # 头来自 new,尾来自 cached
    assert merged["date"].iloc[0] == new["date"].iloc[0]
    assert merged["date"].iloc[-1] == cached["date"].iloc[-1]


def test_merge_klines_df_overlap_dedup_prefers_new():
    """场景:新 K 线与缓存 K 线在边界重叠(典型于 ex.klines 返回闭区间)。
    重叠位置必须取 ``new``——``new`` 是刚从交易所拉取的、更完整/更新的数据;
    ``cached`` 那根往往是分钟刚开始时被缓存下来的"进行中" bar,会塌缩。"""
    from cl_app.services.kline_recompute import merge_klines_df

    cached = _make_klines("2024-01-01 10:00", 10)
    # new 与 cached 的最前 3 根重叠,close 故意改成 999.0 用于断言"被 new 覆盖"
    new = _make_klines("2024-01-01 09:53", 10).copy()
    overlap_dates = cached["date"].iloc[:3].tolist()
    new.loc[new["date"].isin(overlap_dates), "close"] = 999.0

    merged = merge_klines_df(cached, new)

    # 长度 = cached(10) + new 中独有的(7) = 17
    assert len(merged) == 17
    # 重叠 3 根的 close 应来自 new(999.0),而非 cached
    for d in overlap_dates:
        merged_close = merged.loc[merged["date"] == d, "close"].iloc[0]
        assert merged_close == 999.0, f"{d} 的重叠 K 线未取 new 值"


def test_merge_klines_df_overlap_updates_frozen_inprogress_bar():
    """回归:重叠 bar 必须用 ``new`` 刷新,否则"进行中" bar 会被永久冻结。

    真实场景:某分钟刚开始(第 1~2 秒)轮询就把那根 bar 算进缓存,此时
    QMT 只有第一笔成交 → o=h=l=c=开盘价、量极小。之后同一/下一根轮询里
    ``new`` 已是该 bar 更完整的状态,merge 必须让 ``new`` 覆盖 ``cached``,
    否则每根 K 线都永远停在开盘瞬间快照(web 上 o=h=l=c 全塌缩)。
    """
    from cl_app.services.kline_recompute import merge_klines_df

    # cached:两根都是"进行中"快照,OHLC 全塌缩成开盘价、量极小
    cached = pd.DataFrame({
        "date": pd.to_datetime([1700000000, 1700000060], unit="s", utc=True),
        "open": [10.0, 20.0], "high": [10.0, 20.0], "low": [10.0, 20.0],
        "close": [10.0, 20.0], "volume": [5, 8],
    })
    # new:同样两根 date,已是完整收盘 bar
    new = pd.DataFrame({
        "date": pd.to_datetime([1700000000, 1700000060], unit="s", utc=True),
        "open": [10.0, 20.0], "high": [12.0, 23.0], "low": [9.0, 19.0],
        "close": [11.0, 21.0], "volume": [5000, 8000],
    })

    merged = merge_klines_df(cached, new)

    assert len(merged) == 2
    assert merged["high"].tolist() == [12.0, 23.0], "high 未被 new 刷新,bar 仍冻结"
    assert merged["low"].tolist() == [9.0, 19.0], "low 未被 new 刷新,bar 仍冻结"
    assert merged["close"].tolist() == [11.0, 21.0], "close 未被 new 刷新"
    assert merged["volume"].tolist() == [5000, 8000], "volume 未被 new 刷新"


def test_merge_klines_df_empty_cached_returns_new():
    """缓存为空时,应直接返回新 K 线(排序后)。"""
    from cl_app.services.kline_recompute import merge_klines_df

    new = _make_klines("2024-01-01 09:30", 5)
    merged = merge_klines_df(pd.DataFrame(), new)
    assert len(merged) == 5
    pd.testing.assert_frame_equal(
        merged.reset_index(drop=True),
        new.sort_values("date").reset_index(drop=True),
    )


@pytest.fixture
def cl_config_min():
    """精简 cl_config,只填必需字段,其余由 cl.CL._init_default_config 兜底。"""
    return {
        "chart_show_fx": "1",
        "chart_show_bi": "1",
        "chart_show_xd": "1",
        "chart_show_bi_zs": "1",
        "chart_show_xd_zs": "1",
        "chart_show_bi_mmd": "1",
        "chart_show_xd_mmd": "1",
        "chart_show_bi_bc": "1",
        "chart_show_xd_bc": "1",
        "zs_bi_type": ["zs_type_bz"],
        "zs_xd_type": ["zs_type_bz"],
        "idx_macd_fast": 12,
        "idx_macd_slow": 26,
        "idx_macd_signal": 9,
    }


def test_recompute_chart_data_from_klines_returns_full_xd(cl_config_min):
    """全量重算应输出非空的 K 线时间序列 t,以及形如 list 的 xds 数组。"""
    from cl_app.services.kline_recompute import recompute_chart_data_from_klines

    klines = _make_klines("2024-01-01 09:30", 200)
    chart_data = recompute_chart_data_from_klines("a", "TEST.001", "1m", cl_config_min, klines)
    assert chart_data is not None
    assert isinstance(chart_data.get("t"), list) and len(chart_data["t"]) == 200
    assert isinstance(chart_data.get("xds"), list)


def test_recompute_split_then_merge_equals_full(cl_config_min):
    """**核心断言**:把 K 线分两段(模拟向左滚动)后合并重算 vs 一次性全量计算,
    生成的 xds / bis / fxs 数组必须 1:1 相等。这是本次修复要保证的不变量。"""
    from cl_app.services.kline_recompute import (
        merge_klines_df,
        recompute_chart_data_from_klines,
    )

    full = _make_klines("2024-01-01 09:30", 400)

    # 1) 一次性全量
    expected = recompute_chart_data_from_klines(
        "a", "TEST.002", "1m", cl_config_min, full
    )

    # 2) 模拟用户:先看后半段,再向左滚动加载前半段
    later_half = full.iloc[200:].reset_index(drop=True)
    earlier_half = full.iloc[:200].reset_index(drop=True)
    merged = merge_klines_df(later_half, earlier_half)
    actual = recompute_chart_data_from_klines(
        "a", "TEST.002", "1m", cl_config_min, merged
    )

    assert actual["t"] == expected["t"], "K 线时间序列不一致"

    def _shape_signature(shape):
        # 用 (起点, 终点) 作为身份;linestyle 允许差异(end pending 可能不同)
        pts = shape.get("points")
        if isinstance(pts, list) and len(pts) >= 2:
            return (pts[0]["time"], pts[0]["price"], pts[-1]["time"], pts[-1]["price"])
        if isinstance(pts, dict):
            return (pts["time"], pts["price"])
        return None

    for key in ("xds", "bis", "fxs"):
        a = sorted(filter(None, (_shape_signature(s) for s in actual.get(key, []))))
        e = sorted(filter(None, (_shape_signature(s) for s in expected.get(key, []))))
        assert a == e, f"{key} 在分段重算 vs 全量 间不一致"


def test_prepend_klines_and_replace_cache_writes_full(cl_config_min, monkeypatch):
    """高层入口:给定旧 chart_data + 新 K 线,应当返回基于"完整 K 线"重算后的 chart_data,
    且向 chart_data_cache 写入的 entry.is_full_snapshot=True、min/max_time 覆盖完整范围。"""
    from cl_app.services import chart_cache, kline_recompute

    # 构造"已缓存的 chart_data"(模拟用户首次进入时算出的近 100 根)
    early = _make_klines("2024-01-01 09:30", 100)
    cached_chart_data = kline_recompute.recompute_chart_data_from_klines(
        "a", "TEST.003", "1m", cl_config_min, early
    )

    cache_key = chart_cache._build_cache_key("a", "TEST.003", "1m", cl_config_min)
    chart_cache._set_chart_cache_entry(cache_key, cached_chart_data, is_full_snapshot=True)

    # 用户向左滚动,新拉取了"更早 100 根"
    earlier = _make_klines("2024-01-01 07:50", 100)
    new_chart_data = kline_recompute.prepend_klines_and_replace_cache(
        "a", "TEST.003", "1m", cl_config_min, earlier, cache_key=cache_key
    )

    assert new_chart_data is not None
    assert len(new_chart_data["t"]) == 200, "完整 K 线应是 100(旧) + 100(新)"

    # 缓存项已被整体替换
    entry = chart_cache._get_chart_cache_entry(cache_key)
    assert entry["is_full_snapshot"] is True
    assert entry["min_time"] == new_chart_data["t"][0]
    assert entry["max_time"] == new_chart_data["t"][-1]


def test_prepend_klines_no_cache_fallbacks_to_new_only(cl_config_min):
    """无既有缓存时,prepend 入口应当退化为"以 new 为唯一 K 线源"重算。"""
    from cl_app.services.kline_recompute import prepend_klines_and_replace_cache

    klines = _make_klines("2024-01-01 09:30", 50)
    chart_data = prepend_klines_and_replace_cache(
        "a", "TEST.004", "1m", cl_config_min, klines, cache_key="non-existent-key"
    )
    assert chart_data is not None
    assert len(chart_data["t"]) == 50


# =============================================================================
# Regression: tz-naive vs tz-aware merge crash (2026-05-13)
# =============================================================================
#
# 报错栈:tv_history → prepend_klines_and_replace_cache → merge_klines_df →
#   sort_values("date") → TypeError: Cannot compare tz-naive and tz-aware timestamps
# 根因:extract_klines_df_from_chart_data 用 pd.to_datetime(ts, unit="s") 返回 naive,
# 而 ex.klines() 返回带 UTC tz 的 datetime。两边 concat 后 sort_values 直接抛。
# 修复:extract 用 utc=True;merge_klines_df 加 _ensure_tz_aware 兜底。

def test_extract_klines_df_date_is_tz_aware_utc():
    """反构建出的 date 列必须是 tz-aware UTC,与 ex.klines() 输出兼容。"""
    from cl_app.services.kline_recompute import extract_klines_df_from_chart_data

    chart_data = {
        "t": [1700000000, 1700000060, 1700000120],
        "o": [1.0, 2.0, 3.0],
        "h": [1.1, 2.1, 3.1],
        "l": [0.9, 1.9, 2.9],
        "c": [1.05, 2.05, 3.05],
        "v": [100, 200, 300],
    }
    df = extract_klines_df_from_chart_data(chart_data)
    assert df["date"].dt.tz is not None, "date 必须 tz-aware,否则 merge 会抛 TypeError"
    assert str(df["date"].dt.tz) == "UTC"


def test_merge_klines_df_naive_cached_aware_new_does_not_crash():
    """回归测试:cached 是 naive (老格式),new 是 tz-aware,merge 不再抛 TypeError。"""
    from cl_app.services.kline_recompute import merge_klines_df

    cached = pd.DataFrame({
        "date": pd.to_datetime([1700000000, 1700000060], unit="s"),  # naive
        "open": [1.0, 2.0], "high": [1.1, 2.1], "low": [0.9, 1.9],
        "close": [1.05, 2.05], "volume": [100, 200],
    })
    new = pd.DataFrame({
        "date": pd.to_datetime([1700000120, 1700000180], unit="s", utc=True),  # UTC tz-aware
        "open": [3.0, 4.0], "high": [3.1, 4.1], "low": [2.9, 3.9],
        "close": [3.05, 4.05], "volume": [300, 400],
    })

    # 修复前会抛 TypeError: Cannot compare tz-naive and tz-aware timestamps
    merged = merge_klines_df(cached, new)
    assert len(merged) == 4
    assert merged["date"].is_monotonic_increasing
    # 合并后所有 date 都应是 tz-aware
    assert merged["date"].dt.tz is not None


def test_merge_klines_df_aware_cached_naive_new_does_not_crash():
    """对称回归:cached tz-aware,new naive,也应能 merge。"""
    from cl_app.services.kline_recompute import merge_klines_df

    cached = pd.DataFrame({
        "date": pd.to_datetime([1700000000, 1700000060], unit="s", utc=True),
        "open": [1.0, 2.0], "high": [1.1, 2.1], "low": [0.9, 1.9],
        "close": [1.05, 2.05], "volume": [100, 200],
    })
    new = pd.DataFrame({
        "date": pd.to_datetime([1700000120, 1700000180], unit="s"),  # naive
        "open": [3.0, 4.0], "high": [3.1, 4.1], "low": [2.9, 3.9],
        "close": [3.05, 4.05], "volume": [300, 400],
    })
    merged = merge_klines_df(cached, new)
    assert len(merged) == 4
    assert merged["date"].is_monotonic_increasing


def test_merge_klines_df_cross_tz_aware_alpaca_vs_cq():
    """alpaca 返回 UTC tz,cq 返回 Asia/Shanghai tz——两边都 tz-aware,跨 tz 比较 pandas
    会内部对齐到 UTC,不需要在我们这边强转,merge 仍正确。"""
    from cl_app.services.kline_recompute import merge_klines_df

    cached = pd.DataFrame({
        "date": pd.to_datetime([1700000000, 1700000060], unit="s", utc=True),
        "open": [1.0, 2.0], "high": [1.1, 2.1], "low": [0.9, 1.9],
        "close": [1.05, 2.05], "volume": [100, 200],
    })
    new = pd.DataFrame({
        "date": pd.to_datetime([1700000120, 1700000180], unit="s", utc=True).tz_convert("Asia/Shanghai"),
        "open": [3.0, 4.0], "high": [3.1, 4.1], "low": [2.9, 3.9],
        "close": [3.05, 4.05], "volume": [300, 400],
    })
    merged = merge_klines_df(cached, new)
    assert len(merged) == 4
    assert merged["date"].is_monotonic_increasing


# =============================================================================
# Regression: cross-tz merge degrades to object dtype, crashes _preprocess (2026-05-15)
# =============================================================================
#
# 报错栈:tv_history → prepend_klines_and_replace_cache → recompute_chart_data_from_klines
#   → cd.process_klines(merged) → KlineDataProcessor._preprocess line 75 调用
#   pd.to_datetime(klines['date']) →
#   ValueError: Tz-aware datetime.datetime cannot be converted to datetime64 unless utc=True, at position N
#
# 根因:pd.concat 两个 *不同* tz 的 datetime64 列会降级为 object dtype。
# 之前的 _ensure_tz_aware 只处理 naive→UTC,没处理"两边都 aware 但 tz 不同"。
# 在用户配置 EXCHANGE_US='cq' 且 QQQ.US 走 cq 长桥分支(返回 Asia/Shanghai tz)、
# 同时 extract_klines_df_from_chart_data 返回 UTC 时复现。

def test_merge_klines_df_cross_tz_keeps_datetime64_dtype():
    """合并后 date 列必须保持 datetime64 dtype。

    KlineDataProcessor._preprocess 用 ``is_datetime64_any_dtype`` 判断是否要走
    ``pd.to_datetime`` fallback;一旦降级到 object dtype 且元素带不同 tz,
    无 ``utc=True`` 的 ``pd.to_datetime`` 立刻抛 ValueError。
    """
    from cl_app.services.kline_recompute import merge_klines_df

    cached = pd.DataFrame({
        "date": pd.to_datetime([1700000000, 1700000060], unit="s", utc=True),
        "open": [1.0, 2.0], "high": [1.1, 2.1], "low": [0.9, 1.9],
        "close": [1.05, 2.05], "volume": [100, 200],
    })
    new = pd.DataFrame({
        "date": pd.to_datetime([1700000120, 1700000180], unit="s", utc=True).tz_convert("Asia/Shanghai"),
        "open": [3.0, 4.0], "high": [3.1, 4.1], "low": [2.9, 3.9],
        "close": [3.05, 4.05], "volume": [300, 400],
    })
    merged = merge_klines_df(cached, new)
    assert pd.api.types.is_datetime64_any_dtype(merged["date"]), (
        f"merged date dtype 必须是 datetime64,实际 {merged['date'].dtype}"
        " — 下游 _preprocess 会调用 pd.to_datetime(无 utc=True) 抛 ValueError"
    )


def test_recompute_chart_data_with_cross_tz_cached_new(cl_config_min):
    """端到端回归:cached UTC + new Asia/Shanghai 合并后 recompute 不抛。"""
    from cl_app.services.kline_recompute import (
        merge_klines_df,
        recompute_chart_data_from_klines,
    )

    ts_cached = list(range(1700000000, 1700000000 + 100 * 60, 60))
    cached = pd.DataFrame({
        "date": pd.to_datetime(ts_cached, unit="s", utc=True),
        "open": [100.0] * 100, "high": [100.5] * 100, "low": [99.5] * 100,
        "close": [100.0] * 100, "volume": [1000] * 100,
    })
    ts_new = list(range(1700000000 + 100 * 60, 1700000000 + 120 * 60, 60))
    new = pd.DataFrame({
        "date": pd.to_datetime(ts_new, unit="s", utc=True).tz_convert("Asia/Shanghai"),
        "open": [101.0] * 20, "high": [101.5] * 20, "low": [100.5] * 20,
        "close": [101.0] * 20, "volume": [1100] * 20,
    })

    merged = merge_klines_df(cached, new)
    chart_data = recompute_chart_data_from_klines(
        "us", "QQQ.US", "1m", cl_config_min, merged
    )
    assert chart_data is not None
    assert len(chart_data["t"]) == 120


def test_recompute_handles_object_dtype_with_mixed_tz(cl_config_min):
    """回归: 当传入 klines 的 'date' 列是 object dtype 且元素混合 tz-aware/naive
    (长桥 SDK 边界场景导致 pd.concat 后 dtype 退化), recompute 应能透明归一化
    为 datetime64[ns, UTC] 并完成计算, 而不是在下游 _preprocess L75 的
    pd.to_datetime(不传 utc=True) 抛 ValueError。
    """
    import datetime
    from cl_app.services.kline_recompute import recompute_chart_data_from_klines

    # 造 200 根 toy 数据, 前 100 根是 tz-naive Python datetime, 后 100 根是
    # tz-aware Asia/Shanghai Python datetime — 模拟 pd.concat 后 dtype 退化的
    # object 列。
    rows = []
    base = datetime.datetime(2026, 5, 14, 9, 30)
    tz = pd.Timestamp("2026-05-14", tz="Asia/Shanghai").tz
    for i in range(100):
        rows.append({
            "date": base + datetime.timedelta(minutes=i),  # tz-naive
            "open": 100.0 + i * 0.05, "high": 100.5 + i * 0.05,
            "low": 99.5 + i * 0.05, "close": 100.0 + i * 0.05, "volume": 1000,
        })
    for i in range(100):
        rows.append({
            "date": (base + datetime.timedelta(minutes=100 + i)).replace(tzinfo=tz),
            "open": 100.0 + i * 0.05, "high": 100.5 + i * 0.05,
            "low": 99.5 + i * 0.05, "close": 100.0 + i * 0.05, "volume": 1000,
        })
    klines = pd.DataFrame(rows)
    # 验证我们造的 'date' 列确实是 object dtype(混合 tz)
    assert klines["date"].dtype == object

    # 调用 recompute, 应当不抛
    chart_data = recompute_chart_data_from_klines(
        "us", "TEST.001", "1m", cl_config_min, klines
    )
    assert chart_data is not None
    assert len(chart_data["t"]) > 0
