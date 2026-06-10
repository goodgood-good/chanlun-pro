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


def _three_zoushi_overlap(lines: List[LINE]) -> Optional[Tuple[float, float]]:
    """按股价把区间线段分**三走势**(复用走势类型口径,原文 line10012 三段重合):在**最高线段(顶)**
    与**最低线段(底)**两处转折切成 3 段(对应 上涨/下跌/盘整 的自然分界——顶/底是走势方向反转处,
    其余来回归入相邻段),每段取 [min low, max high],返回三段重合 [max(三段低), min(三段高)]。
    顶/底落在端点或相邻致空段 → 退化为按段数均分 3 组。线段<3 或重合为空 → None。"""
    n = len(lines)
    if n < 3:
        return None
    his = [ln.zs_high for ln in lines]
    los = [ln.zs_low for ln in lines]
    ih = max(range(n), key=lambda k: his[k])             # 最高线段(顶)
    il = min(range(n), key=lambda k: los[k])             # 最低线段(底)
    c1, c2 = sorted((ih, il))
    g1, g2, g3 = lines[:c1 + 1], lines[c1 + 1:c2 + 1], lines[c2 + 1:]
    if not (g1 and g2 and g3):                           # 顶/底在端点或相邻 → 退化均分
        k = n // 3
        if k < 1:
            return None
        g1, g2, g3 = lines[:k], lines[k:2 * k], lines[2 * k:]
        if not (g1 and g2 and g3):
            return None
    lows = [min(ln.zs_low for ln in g) for g in (g1, g2, g3)]
    highs = [max(ln.zs_high for ln in g) for g in (g1, g2, g3)]
    zd, zg = max(lows), min(highs)
    return (zd, zg) if zd < zg else None


def _kuozhang_upgrade(a: ZS, b: ZS, xds: List[LINE]) -> Optional[ZS]:
    """**扩张型升级**(原文中心定理二 line10007/10029):相邻两同级别中枢 GG/DD 包络重叠 → 把跨这两
    中枢的区间**按股价分三走势**(_three_zoushi_overlap,复用走势类型分解、非写死中枢本体——三走势
    可能是 上涨/下跌/盘整 任意组合、段数可变),升级中枢区间 = 三走势重合(原文 line10012)。
    region = A 首线段 ~ B 末线段(供下游 kuozhan_level_signals 补进入/离开段)。"""
    if not a.lines or not b.lines:
        return None
    ia = _line_index(a.lines[0], xds)
    ib = _line_index(b.lines[-1], xds)
    if ia is None or ib is None:
        return None
    region = xds[ia:ib + 1]
    interval = _three_zoushi_overlap(region)             # 按股价三走势重合
    if interval is None:                                 # 退化:两中枢包络重合(line10018 首尾主定)
        zd, zg = max(a.dd, b.dd), min(a.gg, b.gg)
        if zd >= zg:
            return None
        interval = (zd, zg)
    return _build_kuozhan_zs([a, b], region, interval)


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


def _zslx_span(zslx) -> tuple:
    """走势类型**整段价格区间** (低, 高)——原文20课中枢定义:ZG=min(g1,g2)/ZD=max(d1,d2),
    gn、dn 是次级别走势类型 Zn 的**高、低点**(整段极值,含进入/离开段超出段内中枢的部分)。

    ⚠ 不能用 zslx.zs_high/zs_low(zslx_branch 喂回字段=段内中枢 gg/dd **包络**):包络口径
    过严,趋势段两端远超中枢包络 → 三段重合判定饿死(5m图全年 0 个 30m 同级别中枢的根因)。
    极值取 段端点(start/end.val) ∪ 段内中枢 gg/dd;两者皆缺(纯桩)→ fallback 包络字段。"""
    vals = []
    for fx in (getattr(zslx, "start", None), getattr(zslx, "end", None)):
        v = getattr(fx, "val", None) if fx is not None else None
        if v is not None:
            vals.append(v)
    his = [z.gg for z in (getattr(zslx, "zss", None) or []) if getattr(z, "gg", None) is not None]
    los = [z.dd for z in (getattr(zslx, "zss", None) or []) if getattr(z, "dd", None) is not None]
    hi_pool = his + vals
    lo_pool = los + vals
    if not hi_pool or not lo_pool:
        return zslx.zs_low, zslx.zs_high
    return min(lo_pool), max(hi_pool)


class _Seg:
    """**结合运算**后的一段(原文 line25179):由相邻**同方向**走势类型合并而成,使段序列严格交替
    (上下上下…)。供 _tongjibie_groups 读 zs_low/zs_high(段价格区间 = 段内走势类型**整段区间**
    _zslx_span 的并,原文20课 gn/dn 口径);zss = 段内所有走势类型的中枢(供 region 定位线段)。"""

    __slots__ = ("dir", "zss", "zs_low", "zs_high", "start", "end")

    def __init__(self, zslx):
        self.dir = zslx._type
        self.zss = list(getattr(zslx, "zss", []) or [])
        self.zs_low, self.zs_high = _zslx_span(zslx)
        self.start = getattr(zslx, "start", None)
        self.end = getattr(zslx, "end", None)

    def merge(self, zslx) -> None:
        self.zss.extend(getattr(zslx, "zss", []) or [])
        lo, hi = _zslx_span(zslx)
        self.zs_low = min(self.zs_low, lo)
        self.zs_high = max(self.zs_high, hi)
        self.end = getattr(zslx, "end", None) or self.end


class _SegLine:
    """交替段 → LINE 形状适配(供 is_beichi/_xingao_xindi:type/high/low/start/end)。"""

    __slots__ = ("type", "high", "low", "start", "end")

    def __init__(self, seg: "_Seg"):
        self.type = seg.dir
        self.high, self.low = seg.zs_high, seg.zs_low
        self.start, self.end = seg.start, seg.end


def _jiehe_segments(zslxs) -> List[_Seg]:
    """**结合运算**(原文 line25178/25179):把次级别走势类型(zslx)中**相邻同方向**的合并成一段,
    得到严格交替(上下上下…)的段序列,使同级别分解的「三段上下上/下上下」(line24727)成立。
    原文:a+A 分解里 Ai 奇数向下、偶数向上(必交替),正是靠结合运算把同向走势并成一个 Ai。"""
    segs: List[_Seg] = []
    for z in zslxs:
        if segs and segs[-1].dir == z._type:
            segs[-1].merge(z)
        else:
            segs.append(_Seg(z))
    return segs


def tongjibie_zhongshu(zss: List[ZS], xds: List[LINE]) -> List[ZS]:
    """30m 中枢 = **同级别分解**(原文 38/39 课):次级别单位 = **5 分钟走势类型(zslx)**(原文 25178
    「Ai 是 5 分钟走势类型」——**不是原始线段中枢**,那是级别错),经**结合运算**(line25179 合并相邻
    同方向)成严格交替段,连续 3 段(上下上/下上下)价格区间重合即中枢(line24727),**恰好 3 段不延伸、
    允许盘整+盘整**(line24728:5 分次级别延伸成 6 段 = 两个 30 分钟盘整的连接;区别于 5m 以下 kuozhan
    扩展/延伸)。

    入参 zss = 该级别中枢序列(5m 图为 5m 线段中枢、1m 图为 5m kuozhan 中枢)→ ZslxBranchCalculator
    出走势类型 → 结合运算 → 三段重合。中枢区间 [zd,zg] = 三段共同重合;region(线段)= 首段首中枢
    首线段 ~ 末段末中枢末线段(供下游 kuozhan_level_signals 补进入/离开段)。完成度 = 仅最后一个未完成。
    """
    return tongjibie_zhongshu_ex(zss, xds)[0]


def tongjibie_zhongshu_ex(zss: List[ZS], xds: List[LINE]):
    """同 tongjibie_zhongshu,另返回段元数据 meta={'segs','groups'}(groups 与产出中枢
    一一对齐),供 tongjibie_level_signals 在**段粒度**上算 30m 买卖点/背驰。"""
    if not zss:
        return [], {"segs": [], "groups": []}
    from chanlun.core.zslx_branch import ZslxBranchCalculator
    zslxs = ZslxBranchCalculator().calculate(zss, [None] * len(zss))
    segs = _jiehe_segments(zslxs)
    groups = _tongjibie_groups(segs)
    out: List[ZS] = []
    out_groups: List[tuple] = []
    for s, e in groups:
        tri = segs[s:e + 1]
        zd = max(sg.zs_low for sg in tri)
        zg = min(sg.zs_high for sg in tri)
        if not tri[0].zss or not tri[-1].zss:
            continue
        first_zs, last_zs = tri[0].zss[0], tri[-1].zss[-1]
        if not first_zs.lines or not last_zs.lines:
            continue
        a = _line_index(first_zs.lines[0], xds)
        b = _line_index(last_zs.lines[-1], xds)
        if a is None or b is None:
            continue
        run = [z for sg in tri for z in sg.zss]
        # 完成度:恰好3段不延伸 → 出现下一交替段(e+1)即本组3段封闭=done;
        # 右边缘组(无后续段)=正在形成,前端画虚线。
        out.append(_build_kuozhan_zs(run, xds[a:b + 1], (zd, zg),
                                     done=(e + 1 < len(segs))))
        out_groups.append((s, e))
    return out, {"segs": segs, "groups": out_groups}


def tongjibie_level_signals(zss: List[ZS], meta: dict, ld_provider, wzgx: str,
                            frequency: Optional[str] = None):
    """同级别分解(30m)买卖点/背驰——**段粒度**(次级别走势类型交替段),非单根线段。

    旧 kuozhan_level_signals 用 xds[b0+1]/[b0+2](单根 5m 线段)当离开/回试段,对 30m 级别
    级别错配(判定窗口太小→信号恒空)。段粒度语义(第20课/38课):
    - 三类:上下上中枢(末段 up=向上离开),segs[e+1](down)整段即第一次回抽,其终点 ≥ ZG → 3buy
      锚回抽段终点;下上下对称 → 3sell。
    - 背驰/一类:enter=segs[s-1] 与 leave=segs[e+1] 必同向(交替性),is_beichi 比力度
      (创新高/低前提用段整段 high/low);前中枢依次同向(is_qs)→ 趋势背驰 qs → 一类,否则盘整背驰 pz。
    """
    from chanlun.core.bs_branch import BuySellPoint
    from chanlun.core.beichi_calculator import is_beichi, is_qs
    segs = meta.get("segs") or []
    groups = meta.get("groups") or []
    bsp, bcs = [], []
    n = len(segs)
    for k, (z, (s, e)) in enumerate(zip(zss, groups)):
        enter = segs[s - 1] if s - 1 >= 0 else None
        after = segs[e + 1] if e + 1 < n else None           # 离开后的第一个交替段(=回抽载体)
        # 背驰 + 一类(enter/after 同向=交替性保证;末段未完成 end=None 跳过)
        if (enter is not None and after is not None and enter.dir == after.dir
                and ld_provider is not None
                and enter.start is not None and enter.end is not None
                and after.start is not None and after.end is not None
                and is_beichi(_SegLine(enter), _SegLine(after), ld_provider, frequency)):
            kind = "qs" if (k > 0 and is_qs(zss[k - 1], z, wzgx)) else "pz"
            bcs.append((after.end.k.date, after.end.val, kind))
            if kind == "qs":
                bsp.append(BuySellPoint("1buy" if after.dir == "down" else "1sell",
                                        z, _SegLine(after), after.end, None))
        # 三类(几何):中枢末段方向=离开方向;after 整段=第一次回抽,终点不破核心
        if after is not None and after.end is not None:
            leave_dir = segs[e].dir
            if leave_dir == "up" and after.dir == "down" and after.end.val >= z.zg:
                bsp.append(BuySellPoint("3buy", z, _SegLine(after), after.end, None))
            elif leave_dir == "down" and after.dir == "up" and after.end.val <= z.zd:
                bsp.append(BuySellPoint("3sell", z, _SegLine(after), after.end, None))
    return bsp, bcs
