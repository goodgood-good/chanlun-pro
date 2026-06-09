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


def _yanshen_upgrade(z: ZS) -> Optional[ZS]:
    """**延伸型升级**(原文 line8157「非标准趋势延伸 9 段成大中枢」/ line23045「9 段以上次级别走势,
    每 3 段构成一个中枢」):单中枢延伸到 line_num>=9 → 构成线段按顺序分 3 组(尽量均匀,9→3+3+3),
    每组取**组内真实**最高/最低(line30290:三角收敛时组高低不一定是组间连接点),升级中枢区间 =
    三组重合 [max(三组低), min(三组高)](line10012 三段重合公式)。region = 该中枢全部线段。"""
    lines = z.lines
    n = len(lines)
    if n < 9:
        return None
    k = n // 3
    groups = [lines[0:k], lines[k:2 * k], lines[2 * k:]]
    if any(not g for g in groups):
        return None
    lows = [min(ln.zs_low for ln in g) for g in groups]
    highs = [max(ln.zs_high for ln in g) for g in groups]
    zd, zg = max(lows), min(highs)
    if zd >= zg:
        return None
    return _build_kuozhan_zs([z], list(lines), (zd, zg))


def _kuozhang_upgrade(a: ZS, b: ZS, xds: List[LINE]) -> Optional[ZS]:
    """**扩张型升级**(原文中心定理二 line10007/10029):相邻两同级别中枢 GG/DD 包络重叠 → 取三走势
    [中枢A(盘整)·A→B 连接(趋势)·中枢B(盘整)],升级中枢区间 = 三走势重合 [max(三段低), min(三段高)]
    (line10012;按 line10018 由首尾两中枢主定,连接段一般不约束)。region = A 首线段 ~ B 末线段
    (供下游 kuozhan_level_signals 补进入/离开段)。"""
    if not a.lines or not b.lines:
        return None
    ia = _line_index(a.lines[0], xds)
    ib = _line_index(b.lines[-1], xds)
    if ia is None or ib is None:
        return None
    lows = [a.dd, b.dd]                                   # 走势①中枢A、走势③中枢B 本体高低
    highs = [a.gg, b.gg]
    ja = _line_index(a.lines[-1], xds)
    jb = _line_index(b.lines[0], xds)
    if ja is not None and jb is not None and jb > ja + 1:  # 走势②=A 末段~B 首段之间的连接走势
        conn = xds[ja + 1:jb]
        lows.append(min(ln.zs_low for ln in conn))
        highs.append(max(ln.zs_high for ln in conn))
    zd, zg = max(lows), min(highs)
    if zd >= zg:
        return None
    return _build_kuozhan_zs([a, b], xds[ia:ib + 1], (zd, zg))


def kuozhan_zhongshu(zss: List[ZS], xds: List[LINE]) -> List[ZS]:
    """子中枢(本级别)→ 高级别中枢:**非同级别分解**(扩展/扩张)。两种升级,**延伸优先于扩张**
    (用户口径):

    ① **延伸**(line8157/23045):单中枢延伸到 9 段 → 直接 3+3+3 分 3 组,三组重合成升级中枢;
    ② **扩张**(中心定理二 line10029):相邻两同级别中枢 GG/DD 包络重叠 → 三走势[A·连接·B]重合成
       升级中枢。延伸用掉的中枢不再参与扩张。

    完成度 = **结束条件**(line7260「走势终完美」):只有序列**最后一个**升级中枢未完成(右边缘正在
    形成,done=False),其余全部已完成(done=True)。
    """
    found: List[Tuple[int, ZS]] = []                     # (起始线段在 xds 的序, 升级中枢)
    used = [False] * len(zss)
    for i, z in enumerate(zss):                          # ① 延伸优先
        if len(z.lines) >= 9:
            up = _yanshen_upgrade(z)
            if up is not None:
                idx = _line_index(z.lines[0], xds)
                found.append((idx if idx is not None else i, up))
                used[i] = True
    i = 0
    while i < len(zss) - 1:                              # ② 扩张:相邻两中枢,跳过延伸用掉的
        if used[i] or used[i + 1]:
            i += 1
            continue
        if is_kuozhan(zss[i], zss[i + 1]):
            up = _kuozhang_upgrade(zss[i], zss[i + 1], xds)
            if up is not None:
                idx = _line_index(zss[i].lines[0], xds) if zss[i].lines else None
                found.append((idx if idx is not None else i, up))
                used[i] = used[i + 1] = True
                i += 2
                continue
        i += 1
    found.sort(key=lambda t: t[0])                       # 按图上线段顺序
    out = [z for _, z in found]
    last = len(out) - 1
    for k, z in enumerate(out):                          # 仅最后一个未完成(line7260)
        z.done = k < last
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


class _Leg:
    """同级别分解的一条交替腿(单向运动,含 ≥1 个同向中枢)。
    供 _tongjibie_groups 读 zs_low/zs_high(腿价格区间 = 腿内中枢包络并 [min dd, max gg])。"""

    __slots__ = ("type", "zss", "zs_low", "zs_high")

    def __init__(self, ztype: str, zss: List[ZS]):
        self.type = ztype
        self.zss = list(zss)
        self.zs_low = min(z.dd for z in zss)
        self.zs_high = max(z.gg for z in zss)


def _alternating_legs(zss: List[ZS]) -> List[_Leg]:
    """中枢序列 → **严格交替腿**(上下上下…),同级别分解的正确输入(原文 24727 三段上下上/
    下上下、24751 操作程式严格交替、25123「更大就分解成小的」= 不把同向多中枢并成大趋势单元)。

    腿 = 一段单向运动:按中枢核心区中心 (zd+zg)/2 比较方向,连续同向中枢并成一腿,方向反转处断开;
    反转腿从极值中枢起(**共享该端点**——反转发生在极值中枢,它既是上腿之尾又是下腿之头)。这样得到
    的腿天然 上下上下 交替,任意连续三腿即 上下上 / 下上下,可直接套 _tongjibie_groups 取重合。
    (区别于 ZslxBranchCalculator 的本体摆动走势类型:那个为递归升级把同向多中枢并成一个趋势 unit、
    方向不交替,喂给同级别分解凑不出上下上 → 中枢恒 0,见 000001 5m 图实测 0 而本法得 2。)"""
    n = len(zss)
    if n == 0:
        return []
    if n == 1:
        return [_Leg("up", [zss[0]])]
    centers = [(z.zd + z.zg) / 2 for z in zss]
    legs: List[_Leg] = []
    start = 0
    cur_dir: Optional[str] = None
    for i in range(1, n):
        d = "up" if centers[i] >= centers[i - 1] else "down"
        if cur_dir is None:
            cur_dir = d
        elif d != cur_dir:
            legs.append(_Leg(cur_dir, zss[start:i]))      # 收尾当前腿 [start, i-1]
            start, cur_dir = i - 1, d                       # 反转腿从极值中枢 i-1 起(共享端点)
    legs.append(_Leg(cur_dir or "up", zss[start:n]))
    return legs


def tongjibie_zhongshu(zss: List[ZS], xds: List[LINE]) -> List[ZS]:
    """30m 中枢 = **同级别分解**(操作级,原文 line24727/24735):把中枢序列做**严格交替腿**
    (_alternating_legs,原文 25123「更大就分解成小的」),连续 3 腿(上下上/下上下)价格区间重合即
    中枢,**恰好 3 段不延伸、允许盘整+盘整**(区别于 5m 以下 kuozhan 扩展/延伸)。

    入参 zss = 该级别中枢序列(5m 图为 5m 线段中枢、1m 图为 5m kuozhan 中枢);**不再吃
    ZslxBranchCalculator 走势类型**(其同向合并令同级别分解失效)。中枢区间 [zd,zg] = 三腿包络
    共同重合;region(线段)= 首腿首中枢首线段 ~ 末腿末中枢末线段(供下游 kuozhan_level_signals 补
    进入/离开段算背驰/买卖点)。完成度 = 纯结束条件(仅序列最后一个未完成,同 kuozhan_zhongshu)。
    """
    legs = _alternating_legs(zss)
    groups = _tongjibie_groups(legs)
    out: List[ZS] = []
    last = len(groups) - 1
    for k, (s, e) in enumerate(groups):
        tri = legs[s:e + 1]
        zd = max(lg.zs_low for lg in tri)
        zg = min(lg.zs_high for lg in tri)
        first_zs, last_zs = tri[0].zss[0], tri[-1].zss[-1]
        if not first_zs.lines or not last_zs.lines:
            continue
        a = _line_index(first_zs.lines[0], xds)
        b = _line_index(last_zs.lines[-1], xds)
        if a is None or b is None:
            continue
        run = [z for lg in tri for z in lg.zss]             # 组内全部中枢(腿间共享端点会重,不影响区间)
        out.append(_build_kuozhan_zs(run, xds[a:b + 1], (zd, zg), done=(k < last)))
    return out
