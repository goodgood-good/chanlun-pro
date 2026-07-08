"""Round11 前端BUG3: prepend 在源头把值列对齐到 len(t)(过长截断/过短右pad None),
防 SSE 推送/缓存/tv_history tail_gap 下游拿到短值列 → 前端 feedRealtimeBar/建bars 越界
取 undefined → NaN 蜡烛。与 tv._align_value_columns_to_t 同款守卫, 只是搬到源头。"""

from cl_app.services.kline_recompute import _align_value_columns_to_t


def test_align_pads_short_column():
    d = {"t": [1, 2, 3], "c": [10, 11]}
    _align_value_columns_to_t(d)
    assert d["c"] == [10, 11, None]


def test_align_truncates_long_column():
    d = {"t": [1, 2, 3], "macd_dif": [1, 2, 3, 4, 5]}
    _align_value_columns_to_t(d)
    assert d["macd_dif"] == [1, 2, 3]


def test_align_noop_when_equal():
    d = {"t": [1, 2, 3], "c": [10, 11, 12], "o": [1, 2, 3]}
    _align_value_columns_to_t(d)
    assert d["c"] == [10, 11, 12]
    assert d["o"] == [1, 2, 3]


def test_align_skips_empty_and_missing_column():
    d = {"t": [1, 2, 3], "v": []}
    _align_value_columns_to_t(d)
    assert d["v"] == []  # 空列保持"无数据"语义, 不 pad
    assert "macd_dea" not in d  # 缺列不新建