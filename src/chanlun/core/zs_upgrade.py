"""zs_upgrade.py — P9 中枢升级（line4898 走势）。

中枢升级本质 = 3 段次级别走势类型重叠（原文 line8131/8155）。三种情况确定区间：
- 正常（非延伸/扩展）：按走势类型（中枢级，原文 line8155 边界=产生新中枢）。【待做】
- 延伸 9 段：9 线段 3+3+3 分三组。【待做】
- 扩展：≥2 中枢按中心定理二重叠 → 涉及线段按 line4898 摆动分段(进入段+前3走势)。【本模块】

设计：扩展区间按「走势」划分(非全局底/顶硬切)，513100→[1.713,1.737]、
301004 z9-11→[39.01,41.73]，均真实数据人工确认。孤立、不接 CL。
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


def _pivots(lines: List[LINE]) -> List[float]:
    """线段序列 → 交替转折点价(p[0]=首线段起点, 之后每线段终点)。

    up 线段 起=zs_low 终=zs_high; down 线段 起=zs_high 终=zs_low。
    线段方向交替, 故转折点也交替(底/顶)。
    """
    if not lines:
        return []
    first = lines[0]
    pv = [first.zs_low if first.type == "up" else first.zs_high]
    for ln in lines:
        pv.append(ln.zs_high if ln.type == "up" else ln.zs_low)
    return pv


def _segment_swings(pv: List[float], first_up: bool) -> List[Tuple[int, int]]:
    """交替转折点 → 走势摆动段 [(起idx, 极值idx), ...]。

    line4898: 下跌走势「创新低则延伸; 出现更高低点 或 更高高点 → 结束于最低底」,
    上涨对称。每段从上一段的极值续起。
    """
    n = len(pv)
    if n < 2:
        return []

    def is_low(idx: int) -> bool:          # p[0] 类型由首线段定, 之后交替
        return first_up if idx % 2 == 0 else (not first_up)

    segs: List[Tuple[int, int]] = []
    s = 0
    while s < n - 1:
        down = pv[s + 1] < pv[s]
        ext_idx = s
        ref = pv[s]                         # 反向极值参考(下跌=last_top, 上涨=last_bot)
        end = n - 1
        i = s + 1
        while i < n:
            if down:
                if is_low(i):
                    if pv[i] < pv[ext_idx]:
                        ext_idx = i          # 新低, 延伸
                    else:
                        end = ext_idx
                        break                # 更高低点 → 结束
                elif pv[i] > ref:
                    end = ext_idx
                    break                    # 更高高点 → 结束
                else:
                    ref = pv[i]
            else:
                if not is_low(i):
                    if pv[i] > pv[ext_idx]:
                        ext_idx = i
                    else:
                        end = ext_idx
                        break
                elif pv[i] < ref:
                    end = ext_idx
                    break
                else:
                    ref = pv[i]
            i += 1
        else:
            end = ext_idx
        if end <= s:                         # 防呆(正常不触发)
            break
        segs.append((s, end))
        s = end
    return segs


def three_segment_interval(lines: List[LINE]) -> Optional[Tuple[float, float]]:
    """扩展区域的线段序列 → 高级别中枢区间 [ZD,ZG]。

    按 line4898 走势把区域切成「进入段 + 若干走势」(摆动分段, 反向极值打断),
    丢掉进入段、取后面前 3 段走势, 每段 [最低,最高],
    [ZD,ZG]=[max(3段低), min(3段高)](原文 line8131 中枢=3走势重叠区;前3为准、余为延伸)。
    退化(ZD>=ZG, 无共同重合)或不足「进入段+3走势」→ None。
    513100→[1.713,1.737]; 301004 z9-11→[39.01,41.73](均真实数据人工确认)。
    """
    pv = _pivots(lines)
    if len(pv) < 2:
        return None
    swings = _segment_swings(pv, first_up=(lines[0].type == "up"))
    if len(swings) < 4:                      # 进入段 + 3 走势
        return None
    three = swings[1:4]                       # 丢进入段, 取前 3 段走势
    lows = [min(pv[a:b + 1]) for a, b in three]
    highs = [max(pv[a:b + 1]) for a, b in three]
    zd, zg = max(lows), min(highs)
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
