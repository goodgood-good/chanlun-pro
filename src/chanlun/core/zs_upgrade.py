"""zs_upgrade.py — 中枢升级(非同级别分解 kuozhan:延伸/扩张)+ 30m 同级别分解(tongjibie)。

原文锚(全部已对齐,2026-06-10 审计):
- 中枢区间 [ZD,ZG] = 三段重叠 [max(a2,b2,c2), min(a1,b1,c1)],共享端点下**恒可简化为
  [max(a2,c2), min(a1,c1)]**(line10012-10018,A/C=与中枢方向一致的 Z 走势段整段高低点)。
- 中心定理一(line10026):延伸 ⟺ Zn 区间与 [ZD,ZG] 重叠;ZD/ZG 由前两 Z 段固定,延伸不改区间。
- 延伸升级(line23046):「延伸不能超过 5 段,一旦出现 6 段延伸,加上本体三段(=9 段)就构成更大
  级别中枢」→ _yanshen_upgrade(≥9 段,3+3+3 组真实极值重合,line31774 实例/line34162 结合律)。
- 扩张升级(中心定理二 line10027):前后中枢核心分离 + 包络触及(后GG≥前DD / 后DD≤前GG)
  ⟺ 高级别中枢,区间 = [max(DD), min(GG)](10018 公式应用于 Z=前/后中枢段)→ _kuozhang_upgrade。
- 30m 同级别分解(line24711-24751):次级别走势类型+结合运算成交替段,恰好 3 段重合、不延伸、
  允许盘整+盘整;买卖点/背驰在段粒度(tongjibie_level_signals)。
完成度 = line7260「走势终完美」结束条件。孤立、不接 CL(由 cl.get_kuozhan_levels 调)。
"""
from __future__ import annotations

from typing import List, Optional, Tuple

from chanlun.core.types import LINE, ZS


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
    """**延伸型升级**(原文 line23046 精确口径:「中枢的延伸不能超过 5 段,一旦出现 6 段的延伸,
    加上形成中枢本身那三段(=9 段),就构成更大级别的中枢」;line8157/23045/37215 互证):单中枢
    本体延伸到 line_num>=9 → 构成线段按顺序分 3 组(尽量均匀,9→3+3+3,line34162「三个三段结合
    起来看」),每组取**组内真实**最高/最低(line30290:三角收敛时组高低不一定是组间连接点),
    升级中枢区间 = 三组重合 [max(三组低), min(三组高)](line31774 实例:「扩展后的中枢区间就是
    每 3 段中的最高最低点的重合区域」)。region = 该中枢全部线段。"""
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
    """**扩张型升级**(原文中心定理二 line10027/10029):相邻两同级别中枢核心分离、GG/DD 包络触及
    → 高级别中枢。

    升级后区间 = **[max(前DD,后DD), min(前GG,后GG)]**(原文 line10018:三段重叠公式「无论哪种
    情况都可简化为 [max(a2,c2), min(a1,c1)]」——高一级的 Z 走势段 A/C = 前/后中枢段(各自整段
    极值=GG/DD),中间连接段 B 与两者共享端点、不影响交集)。其非空性 ⟺ 定理二的触及条件
    (后GG≥前DD / 后DD≤前GG),与 is_kuozhan 自洽——「外围行星发生关系」(line10010 恒星比喻)。
    ⚠ 曾用「顶/底切三走势再交集」:顶/底常落在中枢内部,把中枢本体劈开(违背 Z 段=完整次级别
    走势类型),区间系统性偏窄/空(实测 10/19 流入退化)——已废弃(原文 10018 直接给出简化公式)。
    region = A 首线段 ~ B 末线段(供下游补进入/离开段)。"""
    if not a.lines or not b.lines:
        return None
    ia = _line_index(a.lines[0], xds)
    ib = _line_index(b.lines[-1], xds)
    if ia is None or ib is None:
        return None
    region = xds[ia:ib + 1]
    zd, zg = max(a.dd, b.dd), min(a.gg, b.gg)            # line10018 简化公式
    if zd >= zg:
        return None
    return _build_kuozhan_zs([a, b], region, (zd, zg))


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


def _leg_sub_seg(leg: "_Seg", zz: ZS, side: str) -> Optional["_Seg"]:
    """腿内子段:升级中枢本体首/末中枢不在腿端点时,腿内剩余部分当进入/离开段(方向=腿向)。

    side='after' → zz 之后(start = zz 离开线段起点 FX,转折点口径同 _swing_alternating_segs);
    side='before' → zz 之前(end = zz 首线段起点 FX)。span = 子段内中枢 gg/dd ∪ 两端 FX 值。
    零跨度(zz 即腿端点的退化)→ None。"""
    try:
        idx = next(i for i, z in enumerate(leg.zss) if z is zz)
    except StopIteration:
        return None
    if side == "after":
        run = leg.zss[idx + 1:]
        start = zz.end.start if zz.end is not None else (
            zz.lines[-1].end if zz.lines else None)
        end = leg.end
    else:
        run = leg.zss[:idx]
        start = leg.start
        end = zz.start.start if zz.start is not None else (
            zz.lines[0].start if zz.lines else None)
    if start is None or end is None:
        return None
    fx_vals = [v for v in (getattr(start, "val", None), getattr(end, "val", None))
               if v is not None]
    if not run and (len(fx_vals) < 2 or fx_vals[0] == fx_vals[1]):
        return None
    lo_pool = [z.dd for z in run] + fx_vals
    hi_pool = [z.gg for z in run] + fx_vals
    if not lo_pool:
        return None
    seg = _Seg.__new__(_Seg)
    seg.dir, seg.zss = leg.dir, list(run)
    seg.start, seg.end = start, end
    seg.zs_low, seg.zs_high = min(lo_pool), max(hi_pool)
    return seg


def kuozhan_level_signals_ex(zss: List[ZS], lower_zss: List[ZS], ld_provider, wzgx: str,
                             frequency: Optional[str] = None):
    """kuozhan 级(<30m)中枢序列 → (买卖点[一三类], 背驰段)——**段粒度**(次级别=下级中枢摆动腿)。

    V3 修复(2026-06-11 审计裁决):旧版用单根线段 xds[b0+1]/[b0+2] 当 L1(5m) 中枢的离开/回试段,
    与 30m 修复前同质的级别错配(topic2 C2.10:对 5m 中枢,3买回试=「1m 走势类型」——即下级中枢
    摆动腿,非单根线段;单线段窗口太小,背驰力度与回试判定失真)。

    次级别段 = _swing_alternating_segs(lower_zss)(与 tongjibie 同口径);z 本体由 expanded_with
    (延伸=[z]/扩张=[a,b])定位段范围:
    - 本体端点恰为腿端点 → 进入/离开/回试 = 相邻整腿;
    - 本体在腿中间 → 进入/离开 = 腿内剩余子段(_leg_sub_seg,方向=腿向),回试 = 下一整腿。
    信号口径与旧版同构(仅判定单位升格):
    - **背驰(中继型)**:进入/离开同向 → is_beichi;前同向中枢(is_qs)→ 趋势背驰 qs → 一类
      (离开向下=1buy/向上=1sell,原文 3544),否则盘整背驰 pz(不产一类)。
    - **三类(几何)**:离开向上冲出 + 回试段终点 ≥ ZG → 3buy;向下 + 回试 ≤ ZD → 3sell
      (原文第20课「离开中枢、第一次回试不破核心」)。
    返回 (bsp, bcs):bsp = List[BuySellPoint](level 未填,调用方按级别标);bcs = List[(date,val,kind)]。
    """
    from chanlun.core.bs_branch import BuySellPoint
    from chanlun.core.beichi_calculator import is_beichi, is_qs
    if not zss or not lower_zss:
        return [], []
    segs = _swing_alternating_segs(lower_zss)
    seg_of = {}
    for si, sg in enumerate(segs):
        for zz in sg.zss:
            seg_of[id(zz)] = si
    n = len(segs)
    bsp, bcs = [], []
    for k, z in enumerate(zss):
        comp = list(getattr(z, "expanded_with", None) or [])
        if not comp:
            continue
        s_seg = seg_of.get(id(comp[0]))
        e_seg = seg_of.get(id(comp[-1]))
        if s_seg is None or e_seg is None:
            continue
        if comp[0] is segs[s_seg].zss[0]:                    # 进入段
            enter = segs[s_seg - 1] if s_seg - 1 >= 0 else None
        else:
            enter = _leg_sub_seg(segs[s_seg], comp[0], "before")
        if comp[-1] is segs[e_seg].zss[-1]:                  # 离开段 + 回试段
            leave = segs[e_seg + 1] if e_seg + 1 < n else None
            retest = segs[e_seg + 2] if e_seg + 2 < n else None
        else:
            leave = _leg_sub_seg(segs[e_seg], comp[-1], "after")
            retest = segs[e_seg + 1] if e_seg + 1 < n else None
        # 背驰 + 一类(中继型:进入/离开同向才比较力度)
        if (enter is not None and leave is not None and enter.dir == leave.dir
                and ld_provider is not None
                and enter.start is not None and enter.end is not None
                and leave.start is not None and leave.end is not None
                and is_beichi(_SegLine(enter), _SegLine(leave), ld_provider, frequency)):
            kind = "qs" if (k > 0 and is_qs(zss[k - 1], z, wzgx)) else "pz"
            bcs.append((leave.end.k.date, leave.end.val, kind))
            if kind == "qs":                                  # 趋势背驰 → 一类
                bsp.append(BuySellPoint("1buy" if leave.dir == "down" else "1sell",
                                        z, _SegLine(leave), leave.end, None))
        # 三类(几何:离开 + 回试段终点不破核心 ZG/ZD)
        if leave is not None and retest is not None and retest.end is not None:
            if leave.dir == "up" and retest.end.val >= z.zg:
                bsp.append(BuySellPoint("3buy", z, _SegLine(retest), retest.end, None))
            elif leave.dir == "down" and retest.end.val <= z.zd:
                bsp.append(BuySellPoint("3sell", z, _SegLine(retest), retest.end, None))
    return bsp, bcs


class NestCandidate:
    """区间套介入候选(6b 设计 §3.1):大级别背驰类买点的「进行中」状态——
    在大级别买卖点自身钉死前生成(解 L1 确认滞后 170-519 bar),供次级别确认下推介入。
    纯结构、无未来:仅用当根可见的离开段端点判定。"""

    __slots__ = ("kind", "level", "zs", "leave_end_date", "retest_end_date",
                 "zg", "zd", "invalid_below")

    def __init__(self, kind, level, zs, leave_end_date, zg, zd, invalid_below,
                 retest_end_date=None):
        self.kind = kind
        self.level = level
        self.zs = zs
        self.leave_end_date = leave_end_date      # 时间域下界:确认须晚于离开段终点
        self.retest_end_date = retest_end_date    # 时间域上界:确认须不晚于回试段终点(回试腿内)
        self.zg = zg
        self.zd = zd
        self.invalid_below = invalid_below

    def __repr__(self):
        return (f"NestCandidate({self.kind} L{self.level} "
                f"zg={self.zg} zd={self.zd} inv<{self.invalid_below})")


def kuozhan_level_candidates(zss, lower_zss, wzgx, frequency=None, level=1):
    """右边缘区间套候选(只看最近中枢 zss[-1],对齐 v8「最近中枢」语义)。

    候选 = 离开腿已冲出核心区上沿 ZG、但回试腿尚未走出(leave 是右边缘最后一腿、
    retest=None)——kuozhan_level_signals_ex 此窗口正好留白(其 3buy 要 retest.end≥ZG)。
    候选 active 期间由 collect_nest_cascade_signals(R78 后续)下推 L0 确认触发介入,
    免去等回试腿钉死的 170-519 bar 滞后(6b 设计;原文 H.56 行13922「没必要等回抽走完,
    在次级别第一类买点介入即可」)。

    与 kuozhan_level_signals_ex 段定位口径一致(enter/leave/retest);二者按 retest
    是否走出**互斥**:retest=None→本函数产候选、signals_ex 留白;retest 存在→signals_ex
    接管、本函数不产。纯结构、无未来:仅用当根可见的 leave 段端点。

    第一版只产 3buy 候选(回试腿未形成窗口)。1buy 候选(趋势背驰假设)、回试进行中
    (retest provisional)更细窗口、3sell 镜像留后续(需 walk-forward 逐根语境)。
    """
    if not zss or not lower_zss:
        return []
    segs = _swing_alternating_segs(lower_zss)
    seg_of = {}
    for si, sg in enumerate(segs):
        for zz in sg.zss:
            seg_of[id(zz)] = si
    n = len(segs)
    out = []
    for z in zss:   # 遍历所有中枢:walk-forward 中枢快速易主,只看 zss[-1] 会漏掉历史中枢回试窗口
        comp = list(getattr(z, "expanded_with", None) or [])
        if not comp:
            continue
        s_seg = seg_of.get(id(comp[0]))
        e_seg = seg_of.get(id(comp[-1]))
        if s_seg is None or e_seg is None:
            continue
        if comp[-1] is segs[e_seg].zss[-1]:
            leave = segs[e_seg + 1] if e_seg + 1 < n else None
            retest_idx = e_seg + 2
        else:
            leave = _leg_sub_seg(segs[e_seg], comp[-1], "after")
            retest_idx = e_seg + 1
        retest = segs[retest_idx] if 0 <= retest_idx < n else None
        # 3buy 候选:离开腿向上冲出核心区上沿 ZG + 回试腿已开始(回试腿定义 L0 确认时间窗口
        # [leave_end, retest_end])。遍历所有中枢 + 每 bar 重算 + wf fresh 去重 = 无状态
        # 等价跨 bar 候选状态机:回试腿内 L0 买点首次可见时 collect_nest_cascade_signals 触发。
        if leave is None or leave.dir != "up" or leave.end is None \
                or leave.end.val < z.zg:
            continue
        if retest is None:
            continue                      # 回试腿未开始,无 L0 确认窗口(下一 bar 回试出现再产)
        rlow = min((zz.dd for zz in getattr(retest, "zss", []) or []), default=None)
        if rlow is None and retest.end is not None:
            rlow = retest.end.val
        if rlow is not None and rlow < z.zg:
            continue                      # 回试破核心区上沿 → 3buy 几何失效
        out.append(NestCandidate(
            "3buy", level, z,
            leave.end.k.date if getattr(leave.end, "k", None) is not None else None,
            z.zg, z.zd, z.zg,
            retest_end_date=(retest.end.k.date if (retest.end is not None
                             and getattr(retest.end, "k", None) is not None) else None),
        ))
    return out


def _tongjibie_candidate_groups(zslxs) -> List[tuple]:
    """同级别分解审计候选:所有连续 3 段共同重合的三元组。

    原文 36/39 课允许按结合律选择不同当下组合,但一套同级别操作必须维持
    已确认前缀的唯一分解；因此这些候选只用于解释和审计,不能同时作为交易
    中枢入选。
    """
    candidates = []
    n = len(zslxs)
    for i in range(0, max(n - 2, 0)):
        tri = zslxs[i:i + 3]
        zd = max(z.zs_low for z in tri)
        zg = min(z.zs_high for z in tri)
        if zd < zg:
            candidates.append((i, i + 2))
    return candidates


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
    原文:a+A 分解里 Ai 奇数向下、偶数向上(必交替),正是靠结合运算把同向走势并成一个 Ai。

    primitive:tongjibie 主链 2026-06-11 已改走 _swing_alternating_segs(标签合并对 V/Λ 型
    expand 链方向歧义),本函数当前未接线;保留作 39 课结合运算原语(将来 C3.3 精确条件版
    「A2 升破 a 高点且 A3 不跌回」可在此基础上做)。"""
    segs: List[_Seg] = []
    for z in zslxs:
        if segs and segs[-1].dir == z._type:
            segs[-1].merge(z)
        else:
            segs.append(_Seg(z))
    return segs


def _swing_alternating_segs(zss: List[ZS]) -> List[_Seg]:
    """同级别分解的交替段 = 中枢**本体摆动腿**(直接复用 zslx_branch._swing_segments)。

    曾经的路径「zslx 标签 + _jiehe_segments 同 _type 合并」对 V/Λ 型 expand 链的方向
    标签存在固有歧义,两类实测各错一半:SH.000001 5m 高位横盘+暴跌收尾标 up 被并入上涨
    (净下跌的 up 段);SH.600519 5m V 型链净位移 up 与前上涨合并、丢失中间向下段(2026-06-11
    fix/zhongshu-l0)。同级别分解的段语义是**转折点之间的涨跌摆动**(39课 L25179 Ai 奇下
    偶上严格交替;42课 L26239 含 N 个不延伸中枢的趋势仍是一段——摆动腿粒度正合此条),
    摆动腿天然交替且边界落在极值中枢,直接生成、与走势类型标签解耦。

    段端点=**转折点**(18课 L8131 公式 a1=b1:相邻段共享端点;39课 L25128 段起点=前一
    走势类型结束点):start = 腿首中枢进入段起点 FX,end = 腿末中枢**离开段起点** FX——
    离开段跨越转折、属下一腿(其终点若计入本腿,down 腿 span 会被拉进下一 up 腿的升幅,
    600519 实测 down 腿 span 高点 1431→1565 失真)。span = 腿内中枢整段极值(gg/dd)
    ∪ 两端转折点值。单中枢序列腿方向 None → 净位移 fallback。
    """
    from chanlun.core.zslx_branch import ZslxBranchCalculator

    segs: List[_Seg] = []
    for s, e, leg_dir in ZslxBranchCalculator._swing_segments(zss):
        run = zss[s:e + 1]
        seg = _Seg.__new__(_Seg)
        seg.zss = list(run)
        first, last = run[0], run[-1]
        seg.start = first.start.start if first.start is not None else (
            first.lines[0].start if first.lines else None)
        seg.end = last.end.start if last.end is not None else (
            last.lines[-1].end if last.lines else None)
        lo = min(z.dd for z in run)
        hi = max(z.gg for z in run)
        for fx in (seg.start, seg.end):
            v = getattr(fx, "val", None) if fx is not None else None
            if v is not None:
                lo, hi = min(lo, v), max(hi, v)
        seg.zs_low, seg.zs_high = lo, hi
        if leg_dir in ("up", "down"):
            seg.dir = leg_dir
        else:                                            # 单中枢序列:净位移 fallback
            sv = getattr(seg.start, "val", None) if seg.start is not None else None
            ev = getattr(seg.end, "val", None) if seg.end is not None else None
            if sv is not None and ev is not None and sv != ev:
                seg.dir = "up" if ev > sv else "down"
            else:
                seg.dir = "up"
        segs.append(seg)
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


def tongjibie_zhongshu_ex(zslxs, xds: List[LINE]):
    """同级别分解(30m)中枢 + 段元数据 meta={'segs','groups','all_groups'},供
    tongjibie_level_signals 在段粒度算 30m 买卖点/背驰。

    ★R5 D-③(2026-06-18):入参 = 块R **走势类型**(L_{k-1}.zslxs,即原文 L038「5m 走势
    类型」),经结合运算(`_jiehe_segments` 合并相邻同向走势类型,39课 L25179)成交替段、
    连续 3 段价格重合成 30m 中枢(line24727,不延伸、允许盘整+盘整)。去掉旧
    `_swing_alternating_segs`(中枢→摆动腿,审计 D3 疑其合并过激致 30m 饿死);走势类型
    `_type` 已由 zslx_branch._finalize 修正净位移转折点口径(旧 V/Λ 标签歧义已解)。
    """
    if not zslxs:
        return [], {"segs": [], "groups": [], "all_groups": []}
    segs = _jiehe_segments(zslxs)
    groups = _tongjibie_groups(segs)
    all_groups = _tongjibie_candidate_groups(segs)
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
    return out, {"segs": segs, "groups": out_groups, "all_groups": all_groups}


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
