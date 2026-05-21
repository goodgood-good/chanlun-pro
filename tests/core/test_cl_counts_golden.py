"""tests/core/test_cl_counts_golden.py — US-002 golden 计数测试。

对三种合成行情形态 (上涨段 / 下跌段 / 震荡段) 跑完整缠论流水线，
固化 fxs / bis / xds / bi_zss / xd_zss / bi_mmds / xd_mmds / bi_bcs / xd_bcs
九项数量作为 baseline (record-then-assert)。

后续 P4 改增量缓存、P5+ 任何 core 算法重构，**只要这九项数量稳定**，
就说明结构没出现回归。若某项预期变化，需显式更新基线并在 commit message
中说明动机 (例如修了真实 bug)。

数据形态: multi_freq=True, n=500, seed=42, trend ∈ {up, down, oscillate}。
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest


@dataclass(frozen=True)
class CoreCounts:
    """缠论流水线九项数量基线 (与 cl_snapshot 对应)。"""

    fxs: int
    bis: int
    xds: int
    bi_zss: int
    xd_zss: int
    bi_mmds: int
    xd_mmds: int
    bi_bcs: int
    xd_bcs: int


# ---------------------------------------------------------------------------
# Golden baselines —— 当前 master (commit c9c2553 附近) 跑出的结果固化值
# ---------------------------------------------------------------------------
# 修改基线的判定标准:
#   - 算法 bug 修复 → 必须更新基线, commit message 说明前后差异与原因
#   - 性能重构 (无语义变化) → 基线必须不变, 不变才算重构成功
#   - 配置变更 → 测试本身要更新 (而非偷偷调基线)
GOLDEN_COUNTS = {
    # 注:bi_zss 在 BI.zs_low/zs_high 修复(子项目⑤ 后续, 2026-05-20)前为 0
    # —— BI 默认 zs_low/zs_high=0 让笔层中枢重叠判定全部失败、笔中枢识别为
    # 0。修复后笔层中枢正常识别,bi_mmds(主要为 3 类)随之出现。
    "up": CoreCounts(
        fxs=81, bis=38, xds=4,
        bi_zss=7, xd_zss=1,
        bi_mmds=7, xd_mmds=0,
        bi_bcs=0, xd_bcs=0,
    ),
    "down": CoreCounts(
        fxs=79, bis=40, xds=4,
        bi_zss=6, xd_zss=1,
        bi_mmds=6, xd_mmds=0,
        bi_bcs=0, xd_bcs=0,
    ),
    "oscillate": CoreCounts(
        fxs=81, bis=36, xds=4,
        bi_zss=6, xd_zss=1,
        bi_mmds=6, xd_mmds=0,
        bi_bcs=0, xd_bcs=0,
    ),
}


def _measure_counts(cd) -> CoreCounts:
    """从 CL 对象抽取九项数量。"""
    bis = cd.get_bis()
    xds = cd.get_xds()
    return CoreCounts(
        fxs=len(cd.get_fxs()),
        bis=len(bis),
        xds=len(xds),
        bi_zss=len(cd.get_bi_zss()),
        xd_zss=len(cd.get_xd_zss()),
        bi_mmds=sum(len(b.line_mmds()) for b in bis),
        xd_mmds=sum(len(x.line_mmds()) for x in xds),
        bi_bcs=sum(len(b.line_bcs()) for b in bis),
        xd_bcs=sum(len(x.line_bcs()) for x in xds),
    )


@pytest.mark.parametrize("trend", ["up", "down", "oscillate"])
def test_golden_counts(cl_with_synthetic_klines, trend):
    """三种 trend × 多频合成 K 线，九项数量与基线完全一致。"""
    cd = cl_with_synthetic_klines(500, seed=42, trend=trend, multi_freq=True)
    actual = _measure_counts(cd)
    expected = GOLDEN_COUNTS[trend]
    assert actual == expected, (
        f"\ntrend={trend} 出现数量回归:\n"
        f"  expected: {expected}\n"
        f"  actual:   {actual}\n"
        f"如果这是预期的算法修复, 请更新 GOLDEN_COUNTS[{trend!r}] 并在 commit message 说明前后差异与动机。"
    )


def test_simple_fixture_baseline_unchanged(cl_with_synthetic_klines):
    """smoke fixture (multi_freq=False) 也要跨重构保持稳定: 兜一遍 oscillate n=200, seed=42。

    这条用例的存在是为了防止 _generate_kline_df 的简单模式 (US-001) 被无意改动后,
    smoke test 仍因 RandomState 巧合而通过, 但语义已偏移。
    """
    cd = cl_with_synthetic_klines(200, seed=42, trend="oscillate", multi_freq=False)
    counts = _measure_counts(cd)
    # 简单模式的基线数量 (与之前 explore 出的一致)
    assert counts.fxs == 11
    assert counts.bis == 10
    assert counts.xds == 1
    # BI.zs_low/zs_high 修复后, 笔中枢可正常识别(此前因默认 0 完全失败)
    assert counts.bi_zss == 1
    assert counts.xd_zss == 0


def test_recursive_levels_not_starved(cl_with_synthetic_klines):
    """回归保护: get_recursive_levels 不应被 L0 口径问题饿死在 L0。

    历史 bug (fix/zhongshu-l0 的 l0 改动): 递归 L0 改用主链路 xd_zss/xd_zslx,
    其走势类型数量远少于递归内部自算 (实测 1~2 vs 3~10), 不足 3 个即在
    ``if len(l0_zslxs) < 3: return`` 提前终止, 导致 L1+ 全空、图上多级中枢消失。
    足量数据 (n=1500, up, 内部自算实测装出 L1) 必须能升出 L1+。
    """
    cd = cl_with_synthetic_klines(1500, seed=42, trend="up", multi_freq=True)
    levels = cd.get_recursive_levels()
    max_level = max((lv.level for lv in levels), default=-1)
    assert max_level >= 1, (
        f"递归只到 L{max_level}, L1+ 被饿死——疑似 L0 口径回归。\n"
        f"levels={[(lv.level, len(lv.zss), len(lv.zslxs)) for lv in levels]}"
    )
