"""zs_expand.py — P8 中枢扩展实体化（中心定理二）。

走势类型递归主链之外的「中枢升级」路径：相邻中枢按定理二判扩展(本体包络重叠+
核心区分离)，借跨越的次级别走势类型实体化为高级别中枢。孤立、不改走势类型边界
(原文 line16429 扩展⊥转折)。设计见 docs/chanlun_core_redesign_8_中枢扩展_design.md。
"""
from __future__ import annotations

from typing import List, Optional

from chanlun.core.cl_interface import ZS, ZSLX


def is_zs_expand(prev: Optional[ZS], cur: Optional[ZS]) -> bool:
    """中心定理二·中枢扩展判定（委托 ZS.can_expand_with，统一几何口径）。"""
    if prev is None or cur is None:
        return False
    return prev.can_expand_with(cur)


def _spanning_zslxs(group: List[ZS], zslxs: List[ZSLX]) -> List[ZSLX]:
    """扩展组跨越的次级别走势类型：取 .zss 含 group 任一中枢的连续 zslxs，
    不足 3 个则向两侧补满到 3（forming 由调用方按实际跨越数判定）。

    注：精确选取规则首版用「含组中枢的 zslxs + 补满到 3」，真实出图审校。
    """
    if not zslxs:
        return []
    gid = {id(z) for z in group}
    idxs = [i for i, w in enumerate(zslxs) if any(id(z) in gid for z in w.zss)]
    if not idxs:
        return []
    lo, hi = idxs[0], idxs[-1]
    while (hi - lo + 1) < 3:
        if lo > 0:
            lo -= 1
        elif hi < len(zslxs) - 1:
            hi += 1
        else:
            break
    return zslxs[lo:hi + 1]


def _build_expanded_zs(spanning: List[ZSLX], subs: List[ZS]) -> ZS:
    """走势类型列表 → 高级别中枢。核心区=重合、包络=并集；done=跨越≥3。"""
    zg = min(w.zs_high for w in spanning)    # 核心区上沿 = 重合
    zd = max(w.zs_low for w in spanning)     # 核心区下沿 = 重合
    gg = max(w.zs_high for w in spanning)    # 包络上沿 = 并集
    dd = min(w.zs_low for w in spanning)     # 包络下沿 = 并集
    z = ZS(zs_type="xd", start=spanning[0], end=spanning[-1],
           zg=zg, zd=zd, gg=gg, dd=dd)
    z.lines = list(spanning)                 # 构成段=次级别走势类型
    z.line_num = len(spanning)
    z.done = len(spanning) >= 3              # 3 走势类型(=9段)才完成(line27278)
    z.real = True
    z.expanded_with = list(subs)             # 记录子中枢链
    z._bounds_dirty = False                  # 防 update_boundaries 把 gg/dd 重算成并集覆盖核心区
    return z


def materialize_expansions(zss: List[ZS], zslxs: List[ZSLX]) -> List[ZS]:
    """检测中枢扩展(定理二)/延伸，借次级别走势类型实体化高级别中枢，按时间序返回。"""
    if not zss:
        return []
    n = len(zss)
    used = [False] * n
    results = []                              # (order_idx, ZS)
    # 扩展：相邻 is_zs_expand 连续组(≥2)
    i = 0
    while i < n:
        j = i
        while j + 1 < n and is_zs_expand(zss[j], zss[j + 1]):
            j += 1
        if j > i:
            group = zss[i:j + 1]
            spanning = _spanning_zslxs(group, zslxs)
            if spanning:
                results.append((i, _build_expanded_zs(spanning, group)))
            for k in range(i, j + 1):
                used[k] = True
            i = j + 1
        else:
            i += 1
    # 延伸：未用过的单中枢 ≥9 段
    for k in range(n):
        if not used[k] and zss[k].is_extension_candidate(9):
            spanning = _spanning_zslxs([zss[k]], zslxs)
            if spanning:
                results.append((k, _build_expanded_zs(spanning, [zss[k]])))
    results.sort(key=lambda t: t[0])
    return [z for _, z in results]
