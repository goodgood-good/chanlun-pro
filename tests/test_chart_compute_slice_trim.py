"""tests/test_chart_compute_slice_trim.py — P5 second step 单元测试。

slice_chart_data_to_window + trim_future_bars 从 tv_history 抽出, 测试
覆盖所有切片/裁剪分支, 不依赖完整 Flask 集成。
"""

from __future__ import annotations

import pytest

from cl_app.services.chart_compute import (
    filter_shapes_in_window,
    slice_chart_data_to_window,
    trim_future_bars,
)


def _mk_chart_data(times: list, with_shapes: bool = True) -> dict:
    """构造一份 chart_data dict 用于测试。"""
    n = len(times)
    data: dict = {
        "t": list(times),
        "o": [100.0 + i for i in range(n)],
        "h": [101.0 + i for i in range(n)],
        "l": [99.0 + i for i in range(n)],
        "c": [100.5 + i for i in range(n)],
        "v": [1000.0 + i * 10 for i in range(n)],
        "macd_dif": [0.1 * i for i in range(n)],
        "macd_dea": [0.05 * i for i in range(n)],
        "macd_hist": [0.05 * i for i in range(n)],
        "macd_area": [0.05 * i for i in range(n)],
        "higher_macd_dif": [],
        "higher_macd_dea": [],
        "higher_macd_hist": [],
    }
    if with_shapes:
        data["fxs"] = [
            {"id": "fx_early", "points": {"time": times[0]}},
            {"id": "fx_mid", "points": {"time": times[n // 2]}} if n > 2 else None,
            {"id": "fx_late", "points": {"time": times[-1]}},
        ]
        data["fxs"] = [f for f in data["fxs"] if f is not None]
        # 多点形态 (笔): 跨度
        data["bis"] = [
            {"id": "bi_cross", "points": [{"time": times[0]}, {"time": times[-1]}]},
        ]
        data["xds"] = []
        data["bi_zss"] = []
        data["xd_zss"] = []
        data["bcs"] = []
        data["mmds"] = []
    return data


# === slice_chart_data_to_window ===

def test_slice_within_full_range():
    """from/to 覆盖全部时间, 切片结果与原 dict 数组等价 (除 shapes 过滤)。"""
    times = [100, 200, 300, 400, 500]
    data = _mk_chart_data(times)
    sliced = slice_chart_data_to_window(data, from_ts=100, to_ts=501)
    assert sliced["t"] == times
    assert len(sliced["o"]) == len(times)
    assert sliced["fxs"][0]["id"] == "fx_early"


def test_slice_left_window():
    """切左半段 [100, 300)。"""
    times = [100, 200, 300, 400, 500]
    data = _mk_chart_data(times)
    sliced = slice_chart_data_to_window(data, from_ts=100, to_ts=300)
    # bisect_left(times, 300) = 2 → t[0:2] = [100, 200]
    assert sliced["t"] == [100, 200]
    assert sliced["macd_dif"] == [0.0, 0.1]
    # fx_late time=500 不在 [100, 300), 应被过滤
    fx_ids = [f["id"] for f in sliced["fxs"]]
    assert "fx_early" in fx_ids
    assert "fx_late" not in fx_ids


def test_slice_to_zero_means_no_upper_bound():
    """to_ts=0 → 取到末尾。"""
    times = [100, 200, 300]
    data = _mk_chart_data(times)
    sliced = slice_chart_data_to_window(data, from_ts=100, to_ts=0)
    assert sliced["t"] == times


def test_slice_empty_input():
    """空 chart_data → 浅拷贝返回。"""
    data: dict = {}
    sliced = slice_chart_data_to_window(data, from_ts=100, to_ts=300)
    # 空 t → 浅拷贝(empty dict 或仅 _ARRAY_FIELDS 全空, 但 t 字段不存在原 dict 也不应崩)
    assert sliced.get("t", []) == []


def test_slice_no_data_in_window():
    """请求窗口在数据之外 → 切片结果为空 (但 dict 结构完整)。"""
    times = [100, 200, 300]
    data = _mk_chart_data(times)
    sliced = slice_chart_data_to_window(data, from_ts=500, to_ts=1000)
    assert sliced["t"] == []
    assert sliced["o"] == []
    assert sliced["fxs"] == []  # 所有 fx time<500 都被过滤


def test_slice_preserves_higher_macd_when_empty():
    """higher_macd_dif 等若为空, 切片后仍为空 (不报错)。"""
    times = [100, 200, 300]
    data = _mk_chart_data(times)
    assert data["higher_macd_dif"] == []
    sliced = slice_chart_data_to_window(data, from_ts=100, to_ts=300)
    assert sliced["higher_macd_dif"] == []


# === trim_future_bars ===

def test_trim_no_future_bars():
    """没有超出 to_ts 的 bar → 原样返回 (浅拷贝)。"""
    times = [100, 200, 300]
    data = _mk_chart_data(times)
    trimmed = trim_future_bars(data, to_ts=300)
    assert trimmed["t"] == times


def test_trim_future_bars_dropped():
    """末尾有超出 to_ts 的 bar → 裁掉。"""
    times = [100, 200, 300, 400, 500]
    data = _mk_chart_data(times)
    trimmed = trim_future_bars(data, to_ts=350)
    # bisect_right(times, 350) = 3 → 保留 [100, 200, 300]
    assert trimmed["t"] == [100, 200, 300]
    assert len(trimmed["c"]) == 3
    assert len(trimmed["macd_dif"]) == 3


def test_trim_to_ts_zero_no_op():
    """to_ts=0 表示无上界, 不裁。"""
    times = [100, 200, 300]
    data = _mk_chart_data(times)
    trimmed = trim_future_bars(data, to_ts=0)
    assert trimmed["t"] == times


def test_trim_keeps_shapes_intact():
    """裁未来 bar 时形态字段 (fxs/bis/...) 保持不变, 即使形态 end.time > to_ts。

    业务理由: 笔/段/中枢的 end 可能时间戳 > to_ts 但起点在窗口内,
    切片时已由 filter_shapes_in_window 决定保留, trim 阶段不动。
    """
    times = [100, 200, 300, 400, 500]
    data = _mk_chart_data(times)
    # bi_cross 起点=100, 终点=500
    trimmed = trim_future_bars(data, to_ts=350)
    assert len(trimmed["bis"]) == 1
    assert trimmed["bis"][0]["id"] == "bi_cross"


def test_trim_empty_times_no_op():
    data: dict = {"t": []}
    trimmed = trim_future_bars(data, to_ts=100)
    assert trimmed.get("t", []) == []


# === filter_shapes_in_window ===

def test_filter_single_point_inside():
    shapes = [{"id": "a", "points": {"time": 150}}]
    res = filter_shapes_in_window(shapes, from_ts=100, to_ts=200)
    assert len(res) == 1


def test_filter_single_point_outside():
    shapes = [
        {"id": "before", "points": {"time": 50}},
        {"id": "after", "points": {"time": 250}},
    ]
    res = filter_shapes_in_window(shapes, from_ts=100, to_ts=200)
    assert res == []


def test_filter_multi_point_overlap_kept():
    """笔的 start 早于 to_ts 且 end 晚于 from_ts → 保留。"""
    shapes = [{"id": "cross", "points": [{"time": 50}, {"time": 250}]}]
    res = filter_shapes_in_window(shapes, from_ts=100, to_ts=200)
    assert len(res) == 1


def test_filter_to_zero_no_upper_bound():
    """to_ts=0 → 上界无限。"""
    shapes = [{"id": "x", "points": {"time": 9999999}}]
    res = filter_shapes_in_window(shapes, from_ts=100, to_ts=0)
    assert len(res) == 1


def test_filter_skips_malformed_shapes():
    """非 dict / 缺 points 的 shape 静默跳过。"""
    shapes = [None, "garbage", {"id": "no_points"}, {"id": "valid", "points": {"time": 150}}]
    res = filter_shapes_in_window(shapes, from_ts=100, to_ts=200)
    assert len(res) == 1
    assert res[0]["id"] == "valid"
