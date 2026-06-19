"""30m 同级别分解(tongjibie)。

kuozhan 升级(延伸/扩张)已统一到走势类型递归(见 cl.get_kuozhan_levels /
recursive_branch);本文件原 kuozhan 相关实现已删。

保留 30m 同级别分解:次级别走势类型 + 结合运算(_jiehe_segments,合并相邻同向)成
严格交替段,连续 3 段(上下上/下上下)价格重合即 30m 中枢,恰好 3 段不延伸、允许盘整
连接盘整;买卖点/背驰在段粒度(tongjibie_level_signals)。由 cl.get_kuozhan_levels
的 tongjibie 级调用,入参 = 上一级走势类型(L_{k-1}.zslxs)。
"""
from __future__ import annotations

from typing import List, Optional, Tuple

from chanlun.core.types import LINE, ZS


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


def _tongjibie_candidate_groups(zslxs) -> List[tuple]:
    """同级别分解审计候选:所有连续 3 段共同重合的三元组。

    允许按结合律选择不同当下组合,但一套同级别操作必须维持已确认前缀的唯一分解；
    因此这些候选只用于解释和审计,不能同时作为交易中枢入选。
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
    """同级别分解分组:连续 3 段走势类型价格区间重合 → (start,end) 中枢组,恰好 3 段不延伸
    (三段上下上/下上下重合=中枢,不延伸,6 段=两段盘整连接);前 3 段不重合则前移 1 段
    (连接走势不吞段)。每组用 zs_low/zs_high 取共同重合区间。"""
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
    """走势类型整段价格区间 (低, 高):取整段极值(含进入/离开段超出段内中枢的部分)。

    不能用 zslx.zs_high/zs_low(zslx_branch 喂回字段=段内中枢 gg/dd 包络):包络口径
    过严,趋势段两端远超中枢包络 → 三段重合判定饿死(导致 30m 同级别中枢恒空)。
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
    """结合运算后的一段:由相邻同方向走势类型合并而成,使段序列严格交替(上下上下…)。
    供 _tongjibie_groups 读 zs_low/zs_high(段价格区间 = 段内走势类型整段区间 _zslx_span
    的并);zss = 段内所有走势类型的中枢(供 region 定位线段)。"""

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
    """结合运算:把次级别走势类型(zslx)中相邻同方向的合并成一段,得到严格交替(上下上下…)
    的段序列,使同级别分解的「三段上下上/下上下」成立。

    tongjibie 主链直接用本函数:直接以走势类型 zslxs + 结合运算成段,不经摆动腿合并;
    走势类型 _type 已由 zslx_branch._finalize 修正为净位移口径。"""
    segs: List[_Seg] = []
    for z in zslxs:
        if segs and segs[-1].dir == z._type:
            segs[-1].merge(z)
        else:
            segs.append(_Seg(z))
    return segs


def tongjibie_zhongshu_ex(zslxs, xds: List[LINE]):
    """同级别分解(30m)中枢 + 段元数据 meta={'segs','groups','all_groups'},供
    tongjibie_level_signals 在段粒度算 30m 买卖点/背驰。

    入参 = 上一级走势类型(L_{k-1}.zslxs),经结合运算(`_jiehe_segments` 合并相邻同向
    走势类型)成交替段、连续 3 段价格重合成 30m 中枢(不延伸、允许盘整连接盘整)。直接
    以走势类型成段,不经摆动腿合并;走势类型 `_type` 已由 zslx_branch._finalize 修正为
    净位移转折点口径。
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
    """同级别分解(30m)买卖点/背驰——段粒度(次级别走势类型交替段),非单根线段。

    用次级别走势类型交替段(而非单根线段)做判定,避免判定窗口太小致信号恒空。段粒度语义:
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
