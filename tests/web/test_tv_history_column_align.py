"""锁定 tv_history 响应的「按 bar index 数值列」长度对齐（审查 MED-3）。

前端按 index 取 c/o/h/l/v[i] 以及 macd_*[i]/higher_macd_*[i]（上界 = t.length）。任一数值列
短于 t → 越界处取到 undefined → 静默 NaN（K 线缺口 / MACD 面板空洞，无异常无日志，最难排查）。
正常计算路径恒等长，但不完整磁盘缓存经 slice / 合并后可能错位。

此前只有 OHLCV 5 列有对齐守卫，macd_*/higher_macd_* 7 列原样直塞、无校验（MED-3）。
本测试锁定：**所有** 数值列都对齐到 len(t)，且形态对象数组（fxs/bis/...，本就 != t 长度）不被误伤。
"""
import pathlib
import sys

_root = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_root / "src"))
sys.path.insert(0, str(_root / "web" / "chanlun_chart"))

from cl_app.blueprints import tv as tv_mod  # noqa: E402


def _chart(n_bars=3):
    """一个各数值列都恰好 n_bars 长的合法 cl_chart_data 骨架。"""
    seq = list(range(1, n_bars + 1))
    d = {"t": [1000 + i for i in range(n_bars)]}
    for k in ("c", "o", "h", "l", "v",
              "macd_dif", "macd_dea", "macd_hist", "macd_area",
              "higher_macd_dif", "higher_macd_dea", "higher_macd_hist"):
        d[k] = list(seq)
    return d


def test_macd_column_longer_than_t_is_truncated():
    d = _chart(3)
    d["macd_dif"] = [1, 2, 3, 4, 5]  # 比 t(3) 长 2
    tv_mod._align_value_columns_to_t(d)
    assert d["macd_dif"] == [1, 2, 3]


def test_macd_column_shorter_than_t_is_right_padded():
    d = _chart(4)
    d["macd_hist"] = [1, 2]  # 比 t(4) 短 2
    tv_mod._align_value_columns_to_t(d)
    assert len(d["macd_hist"]) == 4
    assert d["macd_hist"] == [1, 2, None, None]


def test_higher_macd_column_is_aligned():
    d = _chart(3)
    d["higher_macd_dif"] = [0.1, 0.2, 0.3, 0.4]  # 比 t(3) 长 1
    d["higher_macd_hist"] = [0.5]  # 比 t(3) 短 2
    tv_mod._align_value_columns_to_t(d)
    assert d["higher_macd_dif"] == [0.1, 0.2, 0.3]
    assert d["higher_macd_hist"] == [0.5, None, None]


def test_ohlcv_columns_still_aligned():
    # 保护既有 OHLCV 对齐行为（合并进统一函数后不得回退）。
    d = _chart(3)
    d["c"] = [1, 2, 3, 4]
    d["v"] = [9]
    tv_mod._align_value_columns_to_t(d)
    assert d["c"] == [1, 2, 3]
    assert d["v"] == [9, None, None]


def test_equal_length_columns_unchanged():
    # 正常路径（恒等长）：无副作用，列对象值不变。
    d = _chart(3)
    before = {k: list(v) for k, v in d.items()}
    tv_mod._align_value_columns_to_t(d)
    for k, v in before.items():
        assert d[k] == v


def test_formation_arrays_not_touched():
    # 基础形态数组长度本就 != bar 数，绝不能被当数值列截断或补齐。
    d = _chart(3)
    d["bis"] = [{"a": 1}, {"a": 2}]
    d["xds"] = [{"x": 1}]
    d["fxs"] = [{"f": i} for i in range(7)]
    tv_mod._align_value_columns_to_t(d)
    assert d["bis"] == [{"a": 1}, {"a": 2}]
    assert d["xds"] == [{"x": 1}]
    assert len(d["fxs"]) == 7


def test_empty_or_missing_columns_are_safe():
    # 缺列 / 空列不抛异常（get 返回 [] 或 None）。
    d = {"t": [1000, 1001, 1002]}
    d["c"] = []           # 空
    d["macd_dif"] = None  # 缺
    tv_mod._align_value_columns_to_t(d)  # 不应抛
    # 空/缺列不被强行 pad（保持"无数据"语义，与既有 OHLCV 守卫一致：仅当 _col 非空才对齐）
    assert d["c"] == []
