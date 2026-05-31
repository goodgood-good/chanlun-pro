"""tests/core/test_cl_branch.py — 接 CL:新核心 8 模块 lazy 方法(并存,不动旧版)。

用确定性合成多频 K 线(conftest cl_with_synthetic_klines, 线段多)验证新方法可调、
返回正确类型、与旧链路并存独立。
"""
from __future__ import annotations

from chanlun.core.bs_branch import BuySellPoint
from chanlun.core.recursive_branch import LevelResult
from chanlun.core.interval_nest import NestRead


def test_branch_methods_callable_and_coexist(cl_with_synthetic_klines):
    """新核心 3 方法(递归/买卖点/区间套)可调、返回正确类型、并存旧链路独立。"""
    cd = cl_with_synthetic_klines(600, multi_freq=True, trend="up")

    levels = cd.get_recursive_branch_levels()
    pts = cd.get_branch_bspoints()
    reads = cd.get_branch_interval_nest()

    assert isinstance(levels, list)
    assert all(isinstance(lv, LevelResult) for lv in levels)
    assert isinstance(pts, list)
    assert all(isinstance(p, BuySellPoint) for p in pts)
    assert isinstance(reads, list)
    assert all(isinstance(r, NestRead) for r in reads)

    # 并存:旧 get_recursive_levels 独立可调、不受新方法影响
    assert isinstance(cd.get_recursive_levels(), list)

    # 新核心 L0 在位(多频合成数据有线段结构)
    if levels:
        assert levels[0].level == 0


def test_branch_bspoints_no_crash_when_few_segments(cl_with_synthetic_klines):
    """线段太少(无递归级别)→ 买卖点/区间套空,不抛异常(退化健壮)。"""
    cd = cl_with_synthetic_klines(30, multi_freq=False)   # 少 K 线 → 少线段
    assert isinstance(cd.get_branch_bspoints(), list)     # 可能空,但不抛
    assert isinstance(cd.get_branch_interval_nest(), list)
