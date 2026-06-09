"""zs_upgrade.py — P9 中枢升级（中枢扩展 / 中心定理二）。

子中枢(L0)→ 高级别(5min)中枢:连续 is_kuozhan(包络重叠 + 核心区分离, 原文 line10029 定理二)
的子中枢按「运行交集」分组, 区间 = 组内子中枢包络重合 [max(dd),min(gg)](原文 line31774
「扩展后的中枢区间就是每 3 段中的最高最低点的重合区域」)。完成度 = line26870「2 中枢扩展=
进行式」+ line7260「走势终完美」结束条件。孤立、不接 CL。

待做（原文 line8155）: 正常型（按走势类型边界）、延伸型（单中枢 ≥9 段 3+3+3）升级。
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


def _line_index(ln: LINE, xds: List[LINE]) -> Optional[int]:
    for i, x in enumerate(xds):
        if x is ln:
            return i
    return None


def _build_kuozhan_zs(run: List[ZS], region: List[LINE], interval: Tuple[float, float],
                      done: bool = True) -> ZS:
    """扩展组 → 高级别中枢 ZS。核心区[zd,zg]=三段重合;包络[dd,gg]=区域并集;
    起止 = region 首末线段(起=进入段起点, 止=离开不回的结束线段)。
    done=False 表示离开未确认、区域延伸到右边缘=正在形成(前端按 linestyle=1 画虚线)。"""
    zd, zg = interval
    dd = min(x.zs_low for x in region)
    gg = max(x.zs_high for x in region)
    z = ZS(zs_type="xd", start=region[0], end=region[-1],
           zg=zg, zd=zd, gg=gg, dd=dd)
    z.lines = list(region)
    z.line_num = len(region)
    z.done = done
    z.real = True
    z.expanded_with = list(run)
    z._gg_cache, z._dd_cache, z._bounds_dirty = gg, dd, False
    return z


def _group_by_running_overlap(zss: List[ZS]) -> List[List[ZS]]:
    """连续 is_kuozhan 子中枢按「运行交集」分组(原文 line31774 每3段最高最低重合 / line10029
    定理二)。沿 is_kuozhan run 累积子中枢, 维持包络运行交集 [max(dd),min(gg)] 有效;加入下一
    个会使交集塌缩(max(dd)>=min(gg))时本组收尾、从破坏点开新组(破坏的子中枢起下一组)。
    每组 ≥2 子中枢(line10029:2 个同级中枢核心分离+包络重叠即成高级别中枢)。"""
    groups: List[List[ZS]] = []
    n = len(zss)
    i = 0
    while i < n - 1:
        if not is_kuozhan(zss[i], zss[i + 1]):
            i += 1
            continue
        grp = [zss[i]]
        czd, czg = zss[i].dd, zss[i].gg          # 运行交集(子中枢包络的交)
        j = i + 1
        while j < n and is_kuozhan(zss[j - 1], zss[j]):
            nzd, nzg = max(czd, zss[j].dd), min(czg, zss[j].gg)
            if nzd >= nzg:                       # 加入塌缩交集 → 本组收尾
                break
            grp.append(zss[j])
            czd, czg = nzd, nzg
            j += 1
        if len(grp) >= 2:
            groups.append(grp)
            i = j                                # 从破坏点继续(破坏的子中枢起新组)
        else:
            i += 1
    return groups


def kuozhan_zhongshu(zss: List[ZS], xds: List[LINE]) -> List[ZS]:
    """子中枢(L0)→ 高级别(5min)中枢:按「运行交集」分组,每组区间=组内子中枢包络重合。

    原文 line31774「扩展后的中枢区间就是每 3 段中的最高最低点的重合区域」+ line10029 定理二:
    区间 [ZD,ZG] = [max(组内子中枢 dd), min(组内子中枢 gg)](重合);包络 [DD,GG] = 区域并集。
    长 run 按交集塌缩点切成多个中枢(替代旧摆动 three_segment_interval:旧法把整段囫囵框成
    超宽框、过度框选,见 000001 出图对比 2026-06)。

    完成度 = **结束条件**(原文 line7260「走势终完美」/ line10031 三类点):一个中枢「已完成」须
    由后续中枢确认其离开;**只有序列最后一个中枢**未被后续确认 → 未完成(done=False, 虚线),其余
    全部已完成(done=True, 实线)。**任意时刻只有一个未完成中枢**(右边缘正在形成的那个)——历史
    中间的中枢(含 2 子中枢组)早被后续结构确认离开、是定局,不能因 line26870「2 中枢扩展=进行式」
    把它们也标成未完成(那只在「当下正在形成」时成立, 即最后一个)。
    """
    groups = _group_by_running_overlap(zss)
    out: List[ZS] = []
    last = len(groups) - 1
    for k, grp in enumerate(groups):
        a = _line_index(grp[0].lines[0], xds) if grp[0].lines else None
        b = _line_index(grp[-1].lines[-1], xds) if grp[-1].lines else None
        if a is None or b is None:
            continue
        region = xds[a:b + 1]                    # 首子中枢首段 → 末子中枢末段(连续线段区域)
        zd = max(z.dd for z in grp)              # 核心区 = 子中枢包络重合
        zg = min(z.gg for z in grp)
        done = k < last                          # 仅序列最后一个未完成(结束条件), 其余已完成
        out.append(_build_kuozhan_zs(grp, region, (zd, zg), done=done))
    return out


def kuozhan_level_signals(zss: List[ZS], xds: List[LINE], ld_provider, wzgx: str,
                          frequency: Optional[str] = None):
    """一级 kuozhan 中枢序列 → (买卖点[一三类], 背驰段)。各级(5m/30m…)复用。

    kuozhan 中枢无独立进入/离开段(z.start/z.end 是本体段),故在 xds 里补:进入段 =
    xds[a0-1](中枢本体区前一段)、离开段 = xds[b0+1](后一段),a0/b0 = 本体首末段在 xds 的位。
    - **背驰(中继型)**:进入/离开同向 → is_beichi(进入, 离开, ld);前同向中枢(is_qs)→ 趋势背驰
      qs → 一类(离开向下=1buy/向上=1sell,原文 3544),否则盘整背驰 pz(不产一类)。
    - **三类(几何,同 bs_branch._third_class 口径)**:离开向上冲出 + 回试低点 ≥ ZG → 3buy;离开
      向下 + 回试高点 ≤ ZD → 3sell(原文第20课「离开中枢、第一次回试不破核心」)。
    返回 (bsp, bcs):bsp = List[BuySellPoint](level 未填,调用方按级别标);bcs = List[(date,val,kind)]。
    """
    from chanlun.core.bs_branch import BuySellPoint
    from chanlun.core.beichi_calculator import is_beichi, is_qs
    n = len(xds)
    bsp, bcs = [], []
    for k, z in enumerate(zss):
        if not z.lines:
            continue
        a0 = _line_index(z.lines[0], xds)
        b0 = _line_index(z.lines[-1], xds)
        if a0 is None or b0 is None:
            continue
        enter = xds[a0 - 1] if a0 - 1 >= 0 else None        # 进入段
        leave = xds[b0 + 1] if b0 + 1 < n else None          # 离开段
        # 背驰 + 一类(中继型:进入/离开同向才比较力度)
        if (enter is not None and leave is not None and enter.type == leave.type
                and ld_provider is not None and is_beichi(enter, leave, ld_provider, frequency)):
            kind = "qs" if (k > 0 and is_qs(zss[k - 1], z, wzgx)) else "pz"
            bcs.append((leave.end.k.date, leave.end.val, kind))
            if kind == "qs":                                  # 趋势背驰 → 一类
                bsp.append(BuySellPoint("1buy" if leave.type == "down" else "1sell",
                                        z, leave, leave.end, None))
        # 三类(几何:离开 + 回试不破核心 ZG/ZD)
        if leave is not None and b0 + 2 < n:
            retest = xds[b0 + 2]
            if leave.type == "up" and retest.end.val >= z.zg:
                bsp.append(BuySellPoint("3buy", z, retest, retest.end, None))
            elif leave.type == "down" and retest.end.val <= z.zd:
                bsp.append(BuySellPoint("3sell", z, retest, retest.end, None))
    return bsp, bcs


def _tongjibie_groups(zslxs) -> List[tuple]:
    """同级别分解分组:连续 3 段走势类型价格区间重合 → (start,end) 中枢组,**恰好 3 段不延伸**
    (line24727 三段上下上/下上下重合=中枢 / line24735 不延伸 / line24728 6段=2盘整连接);
    前 3 段不重合则前移 1 段(连接走势不吞段)。每组用 zs_low/zs_high 取共同重合区间。"""
    groups = []
    i = 0
    n = len(zslxs)
    while i + 3 <= n:
        tri = zslxs[i:i + 3]
        zd = max(z.zs_low for z in tri)
        zg = min(z.zs_high for z in tri)
        if zd < zg:                                      # 3 段共同重合 → 中枢
            groups.append((i, i + 2))
            i += 3                                        # 恰好 3 段不延伸(6 段→下一组另成中枢)
        else:
            i += 1
    return groups


def tongjibie_zhongshu(zslxs, xds: List[LINE]) -> List[ZS]:
    """30m 中枢 = **同级别分解**(操作级,line24727/24735):连续 3 段次级别走势类型价格区间重合,
    恰好 3 段不延伸、允许盘整+盘整(区别于 5m 以下的 kuozhan 扩展/延伸)。

    中枢区间 [zd,zg] = 3 段走势类型 zs_low/zs_high 的共同重合;region(线段)= 首段首中枢首线段
    ~ 末段末中枢末线段(供下游 kuozhan_level_signals 补进入/离开段算背驰/买卖点)。完成度 = 纯
    结束条件(仅序列最后一个未完成,同 kuozhan_zhongshu / line7260)。
    """
    groups = _tongjibie_groups(zslxs)
    out: List[ZS] = []
    last = len(groups) - 1
    for k, (s, e) in enumerate(groups):
        tri = zslxs[s:e + 1]
        zd = max(z.zs_low for z in tri)
        zg = min(z.zs_high for z in tri)
        fz = getattr(tri[0], "zss", None)
        lz = getattr(tri[-1], "zss", None)
        if not fz or not lz or not fz[0].lines or not lz[-1].lines:
            continue
        a = _line_index(fz[0].lines[0], xds)
        b = _line_index(lz[-1].lines[-1], xds)
        if a is None or b is None:
            continue
        out.append(_build_kuozhan_zs(list(tri), xds[a:b + 1], (zd, zg), done=(k < last)))
    return out
