"""zs_expand.py — P8 中枢扩展实体化（中心定理二）。

走势类型递归主链之外的「中枢升级」路径：相邻中枢按定理二判扩展(本体包络重叠+
核心区分离)，按子中枢包络重合实体化为高级别中枢(每个子中枢=一个最小次级别走势
类型,原文 line31774)。孤立、不改走势类型边界(line16429 扩展⊥转折)。
设计见 docs/chanlun_core_redesign_8_中枢扩展_design.md。
"""
from __future__ import annotations

from typing import List, Optional

from chanlun.core.cl_interface import ZS


def is_zs_expand(prev: Optional[ZS], cur: Optional[ZS]) -> bool:
    """中心定理二·中枢扩展判定（委托 ZS.can_expand_with，统一几何口径）。"""
    if prev is None or cur is None:
        return False
    return prev.can_expand_with(cur)


def _build_expanded_zs(group: List[ZS]) -> Optional[ZS]:
    """扩展组(≥2 相邻定理二中枢) → 高级别中枢。

    核心区[ZD,ZG] = 子中枢包络重合 [max(z.dd), min(z.gg)]；
    包络[DD,GG]   = 并集       [min(z.dd), max(z.gg)]。
    无共同重合(zd>=zg) → None。done = 子中枢数≥3(原文 line27278: 9段=3组次级走势)。
    每个子中枢即一个最小次级别走势类型(原文 line31774)。
    """
    zd = max(z.dd for z in group)        # 核心区下沿 = 重合
    zg = min(z.gg for z in group)        # 核心区上沿 = 重合
    if zd >= zg:                          # 无共同重合区 → 非中枢(退化)，不实体化
        return None
    dd = min(z.dd for z in group)        # 包络下沿 = 并集
    gg = max(z.gg for z in group)        # 包络上沿 = 并集
    z = ZS(zs_type="xd", start=group[0].start, end=group[-1].end,
           zg=zg, zd=zd, gg=gg, dd=dd)
    z.lines = [seg for sub in group for seg in sub.lines]   # 构成段=子中枢线段拼接(类型正确)
    z.line_num = len(z.lines)
    z.done = len(group) >= 3
    z.real = True
    z.expanded_with = list(group)        # 记录子中枢链
    z._gg_cache, z._dd_cache, z._bounds_dirty = gg, dd, False  # 同步缓存,防 update_boundaries 覆盖
    return z


def materialize_expansions(zss: List[ZS]) -> List[ZS]:
    """检测中枢扩展(定理二)，按子中枢包络重合实体化为高级别中枢，按时间序返回。

    扩展：相邻 is_zs_expand 为真的连续中枢成组(≥2) → 1 个高级别中枢。
    延伸(单中枢≥9段)实体化留后续(原文 line31774 段窗口分解，独立口径)。
    """
    if not zss:
        return []
    n = len(zss)
    results: List[ZS] = []
    i = 0
    while i < n:
        j = i
        while j + 1 < n and is_zs_expand(zss[j], zss[j + 1]):
            j += 1
        if j > i:                         # 组 [i..j] ≥2 中枢
            hi_zs = _build_expanded_zs(zss[i:j + 1])
            if hi_zs is not None:         # 退化(无共同重合)→ 跳过
                results.append(hi_zs)
            i = j + 1
        else:
            i += 1
    return results
