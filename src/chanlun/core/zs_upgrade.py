"""zs_upgrade.py — P9 中枢升级（line4898 走势）。

中枢升级本质 = 3 段次级别走势类型重叠（原文 line8131/8155）。三种情况确定区间：
- 正常（非延伸/扩展）：按走势类型（中枢级，原文 line8155 边界=产生新中枢）。【待做】
- 延伸 9 段：9 线段 3+3+3 分三组。【待做】
- 扩展：≥2 中枢按中心定理二重叠 → 涉及线段取「底/顶」切 3 段。【本模块】

设计：513100 真实数据验证 [1.713,1.737]=用户确认值；301004 无重叠/无退化。
孤立、不接 CL。
"""
from __future__ import annotations

from typing import List, Optional, Tuple

from chanlun.core.cl_interface import LINE, ZS


def is_kuozhan(a: ZS, b: ZS) -> bool:
    """中心定理二·中枢扩展：本体包络重叠（闭区间）且 核心区分离（原文 line10029）。

    包络重叠 max(dd)<=min(gg)；核心区分离 b.zg<a.zd 或 b.zd>a.zg。
    核心区也重叠=延伸；包络分离=趋势——均非扩展。
    """
    if None in (a.zd, a.zg, b.zd, b.zg, a.dd, a.gg, b.dd, b.gg):
        return False
    env_overlap = max(a.dd, b.dd) <= min(a.gg, b.gg)
    core_sep = (b.zg < a.zd) or (b.zd > a.zg)
    return env_overlap and core_sep


def three_segment_interval(lines: List[LINE]) -> Optional[Tuple[float, float]]:
    """扩展区域的线段序列 → 高级别中枢区间 [ZD,ZG]。

    line4898 高低点定走势：在区域里取「底」=最低的 down 线段、「顶」=最高的 up 线段
    （只看 down/up 各自，避开进入段把方向带歪），用底/顶两个内部切点把区域切成 3 段；
    每段取 [最低,最高]，[ZD,ZG]=[max(3段DD), min(3段GG)]（原文 line8131 中枢=3走势重叠区）。
    退化（ZD>=ZG，无共同重合）或切不出 3 段 → None。
    """
    n = len(lines)
    if n < 3:
        return None
    down = [i for i in range(n) if lines[i].type == "down"]
    up = [i for i in range(n) if lines[i].type == "up"]
    if not down or not up:
        return None
    p_lo = min(down, key=lambda i: lines[i].zs_low)     # 底：最低 down 线段
    p_hi = max(up, key=lambda i: lines[i].zs_high)       # 顶：最高 up 线段
    inner = sorted({p_lo, p_hi} - {0, n - 1})            # 内部切点（边界不算转折）
    if len(inner) < 2:
        return None
    a, b = inner
    groups = [lines[: a + 1], lines[a + 1: b + 1], lines[b + 1:]]
    if any(not g for g in groups):
        return None
    seg_dd = [min(x.zs_low for x in g) for g in groups]
    seg_gg = [max(x.zs_high for x in g) for g in groups]
    zd, zg = max(seg_dd), min(seg_gg)
    return (zd, zg) if zd < zg else None


def _line_index(ln: LINE, xds: List[LINE]) -> Optional[int]:
    for i, x in enumerate(xds):
        if x is ln:
            return i
    return None


def _build_kuozhan_zs(run: List[ZS], region: List[LINE], interval: Tuple[float, float]) -> ZS:
    """扩展组 → 高级别中枢 ZS。核心区[zd,zg]=三段重合;包络[dd,gg]=区域并集。"""
    zd, zg = interval
    dd = min(x.zs_low for x in region)
    gg = max(x.zs_high for x in region)
    z = ZS(zs_type="xd", start=run[0].start, end=run[-1].end,
           zg=zg, zd=zd, gg=gg, dd=dd)
    z.lines = list(region)
    z.line_num = len(region)
    z.done = True
    z.real = True
    z.expanded_with = list(run)
    z._gg_cache, z._dd_cache, z._bounds_dirty = gg, dd, False
    return z


def kuozhan_zhongshu(zss: List[ZS], xds: List[LINE]) -> List[ZS]:
    """连续 is_kuozhan 中枢成组(≥2)→ 每组取涉及线段区域、按 three_segment_interval
    产 1 个高级别中枢(原文「中枢以前三个为准+延伸」: 一组只一个中枢)。
    退化/切不出 3 段 → 跳过该组。按时间序返回。
    """
    out: List[ZS] = []
    n = len(zss)
    i = 0
    while i < n - 1:
        if not is_kuozhan(zss[i], zss[i + 1]):
            i += 1
            continue
        j = i
        while j + 1 < n and is_kuozhan(zss[j], zss[j + 1]):
            j += 1
        run = zss[i:j + 1]
        i0 = _line_index(run[0].lines[0], xds) if run[0].lines else None
        i1 = _line_index(run[-1].lines[-1], xds) if run[-1].lines else None
        if i0 is not None and i1 is not None and i1 > i0:
            region = xds[i0:i1 + 1]
            interval = three_segment_interval(region)
            if interval is not None:
                out.append(_build_kuozhan_zs(run, region, interval))
        i = j + 1
    return out
