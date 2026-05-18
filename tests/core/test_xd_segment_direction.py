"""tests/core/test_xd_segment_direction.py — 线段方向不变量回归测试。

缠论原著第七节:「从向上一笔开始的线段……其顶 gi 一定大于第一笔的底 d1,
故该线段是向上的;同理从向下一笔开始的线段,其方向也是向下的。」

即结构不变量:
  - up 线段:  start.val < end.val (终点高于起点)
  - down 线段: start.val > end.val (终点低于起点)

历史 bug:当线段内出现一根巨幅反向笔(跳空/暴涨暴跌)使净走向反转时,
``XdCalculator`` 的 ``_try_end`` / ``_emit_segment`` / ``_emit_pending`` 在确定
线段终点时只校验「终点笔方向、笔数」,不校验终点价相对起点价的方向,
于是会输出方向与净走向矛盾的线段(例:TSLA 日线截断到 2024-12 时 XD[2]
被标为 down 段,却从 270.80 涨到 348.22)。

本测试锁定该不变量,防止回归。
"""

from __future__ import annotations

import datetime
import os
import random

import pandas as pd

from chanlun.core.cl import CL
from chanlun.core.cl_interface import BI, CLKline, FX
from chanlun.core.xd_calculator import XdCalculator

FIX_DIR = os.path.join(os.path.dirname(__file__), "..", "fixtures", "klines")


# ---------------------------------------------------------------------------
# 不变量检查
# ---------------------------------------------------------------------------
def direction_violations(xds):
    """返回方向与净走向矛盾的线段列表。空 = 合法。"""
    bad = []
    for i, x in enumerate(xds):
        if x.type == "up" and not (x.start.val < x.end.val):
            bad.append((i, "up", round(x.start.val, 3), round(x.end.val, 3)))
        elif x.type == "down" and not (x.start.val > x.end.val):
            bad.append((i, "down", round(x.start.val, 3), round(x.end.val, 3)))
    return bad


def structural_violations(xds):
    """返回线段结构不变量(≥3笔/方向交替/笔索引连续/端点同向)违反列表。"""
    bad = []
    for i, x in enumerate(xds):
        nb = x.end_line.index - x.start_line.index + 1
        if nb < 3:
            bad.append(f"XD{i} 笔数{nb}<3")
        if x.start_line.type != x.type or x.end_line.type != x.type:
            bad.append(f"XD{i} 端点笔方向≠段方向")
        if i + 1 < len(xds):
            if x.type == xds[i + 1].type:
                bad.append(f"XD{i}/{i + 1} 方向未交替")
            if x.end_line.index + 1 != xds[i + 1].start_line.index:
                bad.append(f"XD{i}/{i + 1} 笔索引不连续")
    return bad


# ---------------------------------------------------------------------------
# 测试输入构造
# ---------------------------------------------------------------------------
def build_bis(values):
    """从交替的转折点价格序列构造一串合法的 BI 对象。

    values 严格上下交替;bi[i] 由 values[i] 指向 values[i+1]。
    相邻笔共享端点 FX,与 BiCalculator 产出的结构一致。
    """
    base = datetime.datetime(2020, 1, 1)
    fxs = []
    for i, v in enumerate(values):
        is_low = (values[1] > values[0]) if i == 0 else (values[i - 1] > values[i])
        k = CLKline(
            k_index=i,
            date=base + datetime.timedelta(minutes=i),
            h=float(v), l=float(v), o=float(v), c=float(v), a=1.0,
            klines=[], index=i,
        )
        fx = FX(_type=("di" if is_low else "ding"), k=k, klines=[k, k, k],
                val=float(v), index=i)
        fxs.append(fx)
    return [
        BI(start=fxs[i], end=fxs[i + 1],
           _type=("up" if values[i + 1] > values[i] else "down"), index=i)
        for i in range(len(values) - 1)
    ]


def _gen_values(kind, n, rng):
    """生成一条 n 笔的随机转折点序列。kind 控制行情形态。"""
    mv = []
    for i in range(n):
        up = (i % 2 == 0)
        if kind == "chop":
            m = rng.uniform(3, 22)
        elif kind == "deep":
            m = rng.uniform(4, 22) if up else rng.uniform(1, 46)
        elif kind == "small":
            m = rng.uniform(0.5, 7)
        elif kind == "spike":
            m = rng.uniform(2, 14) * (rng.uniform(3, 9) if rng.random() < 0.13 else 1)
        else:  # wide
            m = rng.uniform(1, 80)
        mv.append(m if up else -m)
    if rng.random() < 0.5:
        mv = [-x for x in mv]
    vals = [100.0]
    for m in mv:
        vals.append(vals[-1] + m)
    return vals


def _run_fixture(fname, code, freq, cut):
    path = os.path.join(FIX_DIR, fname)
    df = pd.read_parquet(path) if fname.endswith(".parquet") else pd.read_csv(path)
    df.columns = [c.lower() for c in df.columns]
    df = df.iloc[:cut]
    cd = CL(code, freq)
    cd.process_klines(df)
    return list(cd.get_xds())


# ---------------------------------------------------------------------------
# 真实数据回归用例
# ---------------------------------------------------------------------------
def test_tsla_daily_truncated_segment_direction():
    """TSLA 日线截断到 400 根 K线,所有线段方向必须与净走向一致。

    历史 bug:XD[2] 被标为 down 段却从 270.80 涨到 348.22。
    """
    xds = _run_fixture("us_TSLA_US_d.csv", "TSLA", "d", 400)
    bad = direction_violations(xds)
    assert not bad, f"TSLA 日线出现方向矛盾线段: {bad}"


def test_sz301004_30m_truncated_segment_direction():
    """SZ.301004 30分钟截断到 1807 根,线段方向不得与净走向矛盾。

    历史 bug:XD[8] 被标为 down 段却从 49.68 涨到 50.39。
    """
    xds = _run_fixture("a_SZ_301004_30m.csv", "SZ.301004", "30m", 1807)
    bad = direction_violations(xds)
    assert not bad, f"SZ.301004 30m 出现方向矛盾线段: {bad}"


# ---------------------------------------------------------------------------
# 模糊测试用例(确定性,固定种子)
# ---------------------------------------------------------------------------
def test_fuzz_segments_never_direction_contradicting():
    """3000 条随机笔序列(含 spike/gap 形态),任何线段都不得方向矛盾。"""
    rng = random.Random(20260518)
    kinds = ["chop", "deep", "small", "spike", "wide"]
    violations = []
    for t in range(3000):
        n = rng.randint(5, 200)
        vals = _gen_values(rng.choice(kinds), n, rng)
        calc = XdCalculator({})
        calc.calculate(build_bis(vals))
        bad = direction_violations(calc.xds)
        if bad:
            violations.append((t, vals, bad))
    assert not violations, (
        f"{len(violations)}/3000 条随机序列出现方向矛盾线段。\n"
        f"首例 bad={violations[0][2]}\n"
        f"首例输入={[round(v, 2) for v in violations[0][1]]}"
    )


def test_fuzz_segments_structural_invariants():
    """2000 条随机笔序列,线段结构不变量(≥3笔/交替/连续/端点同向)不得违反。"""
    rng = random.Random(771)
    kinds = ["chop", "deep", "small", "spike", "wide"]
    violations = []
    for t in range(2000):
        n = rng.randint(5, 200)
        vals = _gen_values(rng.choice(kinds), n, rng)
        calc = XdCalculator({})
        calc.calculate(build_bis(vals))
        bad = structural_violations(calc.xds)
        if bad:
            violations.append((t, bad))
    assert not violations, (
        f"{len(violations)}/2000 条随机序列出现结构不变量违反。\n"
        f"首例={violations[0]}"
    )
