"""tests/core/test_xd_standard_seq.py — 特征序列标准包含处理回归测试。

``_build_standard_seq`` 把特征序列元素当 K 线做非包含处理(缠论第七节)。
``_try_end`` 用它的末元素作为特征序列分型的「第一元素」。

历史 bug(#2):first_elem 曾取未经包含处理的原始末笔——原段末两个特征
元素若是「前者吞没后者」,标准特征序列里它们必须合并,合并后的 high(上升
段)/low(下降段)会与原始末笔不同;用原始末笔会让分型判据偏松,导致线段
被系统性提前终结。本测试锁定标准包含处理的口径。
"""

from __future__ import annotations

from chanlun.core.xd_calculator import _build_standard_seq


def _elem(high, low):
    """构造一个特征序列元素(与 _bi_to_cs_elem 的 dict 形状一致)。"""
    return {"bi": None, "high": float(high), "low": float(low)}


def test_no_inclusion_keeps_all_elements():
    """相邻元素无包含关系时,全部原样保留。"""
    elems = [_elem(10, 5), _elem(12, 7), _elem(14, 9)]
    std = _build_standard_seq(elems, "up")
    assert [(e["high"], e["low"]) for e in std] == [(10, 5), (12, 7), (14, 9)]


def test_up_direction_merges_engulfed_element():
    """上升方向:后元素被前元素吞没 → 合并,取高高低高(low 取较高者)。"""
    elems = [_elem(120, 100), _elem(118, 105)]  # (118,105) 被 (120,100) 吞没
    std = _build_standard_seq(elems, "up")
    assert len(std) == 1
    assert std[0]["high"] == 120
    assert std[0]["low"] == 105


def test_down_direction_merges_engulfed_element():
    """下降方向:合并取低低高低(high 取较低者)。"""
    elems = [_elem(120, 100), _elem(118, 105)]
    std = _build_standard_seq(elems, "down")
    assert len(std) == 1
    assert std[0]["high"] == 118
    assert std[0]["low"] == 100


def test_last_element_merged_when_engulfed():
    """#2 核心:末元素被吞没时,标准序列末元素必须是合并值,不是原始末笔。"""
    elems = [_elem(10, 1), _elem(20, 8), _elem(18, 12)]  # (18,12) 被 (20,8) 吞没
    std = _build_standard_seq(elems, "up")
    assert len(std) == 2
    assert std[-1]["high"] == 20  # 合并后取高高,非原始末笔的 18
    assert std[-1]["low"] == 12
    assert std[-1]["high"] != elems[-1]["high"]


def test_empty_and_single():
    """边界:空序列 / 单元素序列。"""
    assert _build_standard_seq([], "up") == []
    single = _build_standard_seq([_elem(5, 3)], "up")
    assert len(single) == 1 and single[0]["high"] == 5
