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

import datetime

import pytest

from chanlun.core.cl_interface import Kline
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


# =============================================================================
# process_macd mode C — 一次更新同时"重绘旧末根 + 追加新根"的等价性
# =============================================================================


def _mk_klines(closes: list[float]) -> list[Kline]:
    """用 close 列表构造最小 Kline 列表(MACD 计算只读 .c)。"""
    base = datetime.datetime(2024, 1, 1, 9, 30)
    return [
        Kline(
            index=i,
            date=base + datetime.timedelta(minutes=i),
            h=c + 1.0,
            l=c - 1.0,
            o=c,
            c=c,
            a=1000.0,
        )
        for i, c in enumerate(closes)
    ]


def test_process_macd_mode_c_recomputes_repainted_last_bar():
    """mode C:一次更新同时重绘旧末根 + 追加新根时,旧末根必须被重算。

    polling 在 bar 边界很常见:上一根 bar 收定(close 变化)+ 新 bar 开启,
    一次 process_macd 调用里既"更新末根"又"追加新根"。process_macd 仅凭
    len(klines) 判断模式,mode C 若只算新追加的 bar,被重绘的旧末根 close
    变化不会反映 → EMA 基准被污染,后续所有 bar 的 dif/dea/hist 都偏移。
    """
    closes_v1 = [
        10.0, 11.0, 12.0, 11.5, 12.5, 13.0, 12.0, 11.0,
        10.5, 11.0, 12.0, 13.0, 14.0, 13.5, 12.0,
    ]
    # v2:前 14 根不变,第 15 根(idx 14)被重绘 12.0 → 15.0,再追加 1 根新 bar。
    closes_v2 = closes_v1[:-1] + [15.0, 16.0]

    m = MACD()
    m.process_macd(_mk_klines(closes_v1))   # 首次:全量(mode A)
    m.process_macd(_mk_klines(closes_v2))   # mode C:count 15 → 16

    fresh = MACD()
    fresh.process_macd(_mk_klines(closes_v2))  # 权威全量

    assert m.dif == pytest.approx(fresh.dif), "mode C dif 与全量不一致"
    assert m.dea == pytest.approx(fresh.dea), "mode C dea 与全量不一致"
    assert m.hist == pytest.approx(fresh.hist), "mode C hist 与全量不一致"
