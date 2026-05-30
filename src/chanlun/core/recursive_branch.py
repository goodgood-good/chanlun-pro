"""recursive_branch.py — P4b 缠论递归装配（走势类型递归主链 + 独立升级标注）。

把 L0 线段自底向上递归装配成多级层级树：units→zs_branch(中枢+内联背驰)→
zslx_branch(走势类型)→_as_units→units 逐级。升级标注(9段/扩展)是独立旁路、
不改走势类型边界(原文 line16429：中枢扩展⊥走势转折)。

孤立、不接 CL、不动旧 recursive_calculator。
设计见 docs/chanlun_core_redesign_4b_递归装配_design.md。
"""
from __future__ import annotations

import copy
from dataclasses import dataclass, field
from typing import List, Optional

from chanlun.core.beichi_calculator import LdProvider
from chanlun.core.cl_interface import LINE, ZS, ZSLX
from chanlun.core.zs_branch import DivergenceResult, ZsBranchCalculator, classify_rel
from chanlun.core.zslx_branch import ZslxBranchCalculator

_MAX_LEVELS = 50    # 护栏；走势单元逐级收缩，正常远不及


@dataclass
class LevelResult:
    """单个递归级别的产出。"""
    level: int                                          # 0 = L0
    zss: List[ZS]                                       # 本级已完成中枢
    done_divergence: List[Optional[DivergenceResult]]   # 与 zss 索引对齐(本级内联背驰)
    zslxs: List[ZSLX]                                   # 本级走势类型
    upgrade_idx: List[int] = field(default_factory=list)  # 升级标注:9段/扩展候选的中枢索引(P5 用)


def _as_units(zslxs: List[ZSLX]) -> List[ZSLX]:
    """ZSLX 喂回 zs_branch 当输入段：返回 index 重排为连续 0,1,2… 的**浅拷贝**
    (不改原 ZSLX——原对象 index 保留 start_idx,供 LevelResult 审图定位)。
    zs_high/zs_low 已由 zslx_branch._finalize 填,浅拷贝沿用。"""
    out: List[ZSLX] = []
    for i, zslx in enumerate(zslxs):
        u = copy.copy(zslx)
        u.index = i
        out.append(u)
    return out


def _mark_upgrades(done_zss: List[ZS]) -> List[int]:
    """本级中枢中「9 段升级 / 中枢扩展候选」的索引（line16429 解耦：仅标注、不改
    走势类型；实体化与 2/3 类买点留 P5）。"""
    out: List[int] = []
    for i, z in enumerate(done_zss):
        if len(z.lines) >= 9:                                       # 9 段升级(第33课)
            out.append(i)
        elif i > 0 and classify_rel(done_zss[i - 1], z) == "expand":  # 中枢扩展(中心定理二本体相交)
            out.append(i)
    return out
