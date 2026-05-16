"""tests/core/test_macd_incremental.py — MACD 柱面积增量 vs 全量等价性。

``MACD._calculate_hist_area_incremental`` 的循环把 ``|hist| < 1e-9`` 的柱子
当作 0 处理(只 carry 面积、不改变方向)。但增量分支重建 ``direction`` 时
读的是未经此钳制的原始 ``hist[prev_idx]``:当该值是接近 0、且符号与主趋势
相反的微小值(或精确 0)时,全量路径把它当 0、方向延续,增量重建却据原始
符号判出相反方向 → 同向柱被错误重置面积,增量结果 != 全量。

本测试直接对 ``_calculate_hist_area_incremental`` 比对"一次性全量"与
"逐根增量"两种调用方式的结果,二者必须完全一致。
"""

from __future__ import annotations

import pytest

from chanlun.core.macd import MACD


def _full_area(hist: list[float]) -> list[float]:
    """一次性全量计算柱面积。"""
    m = MACD()
    m.hist_area = []
    m._calculate_hist_area_incremental(list(hist), 0)
    return list(m.hist_area)


def _incremental_area(hist: list[float]) -> list[float]:
    """逐根增量计算柱面积(模拟 process_macd 模式 C 逐根追加)。"""
    m = MACD()
    m.hist_area = []
    for i in range(1, len(hist) + 1):
        sub = hist[:i]
        if i == 1:
            m._calculate_hist_area_incremental(sub, 0)
        else:
            m._calculate_hist_area_incremental(sub, start_index=i - 1)
    return list(m.hist_area)


@pytest.mark.parametrize(
    "hist, desc",
    [
        ([1.0, 1.0, -5e-12, 1.0, 1.0], "正向 run 中夹一个反号微小近零值"),
        ([-1.0, -2.0, 5e-12, -1.0, -2.0], "负向 run 中夹一个反号微小近零值"),
        ([1.0, 1.0, 0.0, 1.0], "正向 run 中夹一个精确 0"),
        ([-1.0, -1.0, 0.0, 0.0, -1.0], "负向 run 中夹连续两个 0"),
        ([2.0, 3.0, -1.0, -4.0, 5.0], "正常的方向切换(无近零值)"),
        ([1.0, -1.0, 1.0, -1.0], "每根都切换方向"),
    ],
)
def test_hist_area_incremental_equals_full(hist, desc):
    """逐根增量计算的柱面积必须与一次性全量计算完全一致。"""
    full = _full_area(hist)
    inc = _incremental_area(hist)
    assert inc == full, f"{desc}: 增量 {inc} != 全量 {full}"
