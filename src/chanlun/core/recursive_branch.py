"""recursive_branch.py — 缠论递归装配（走势类型递归主链 + 独立升级标注）。

把 L0 线段自底向上递归装配成多级层级树：units→zs_branch(中枢+内联背驰)→
zslx_branch(走势类型)→_as_units→units 逐级。升级标注(9段/扩展)是独立旁路、
不改走势类型边界。

孤立、不接 CL、不动旧 recursive_calculator。
"""
from __future__ import annotations

import copy
from dataclasses import dataclass, field
from typing import Callable, List, Optional, Tuple

from chanlun.core.beichi_calculator import LdProvider
from chanlun.core.types import LINE, ZS, ZSLX
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
    units: List[LINE] = field(default_factory=list)       # P5c:该级输入段序列(回试段定位)
    live_zss: List[ZS] = field(default_factory=list)      # 右边缘正在形成的未完成中枢(done=False)。
    # 纯图表展示用(虚线框);刻意不并入 zss——下游买卖点/走势类型只读已完成 zss,保「各级只用 done」不变量。
    live_qs_divergence: List[Tuple[ZS, DivergenceResult]] = field(default_factory=list)
    # R78 区间套真闭环:右边缘 node1=="leave" 读法(中枢完成、末段为离开段)的 provisional 趋势背驰段
    # (kind=="qs" 且 is_beichi)。candidates 用「进行中背驰段」而非 done_divergence,避免回试钉死时
    # 确认已过期的 stale(见 spec R78 实现关键细节)。同样只读不入 zss,不扰动既有信号链。


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
    """本级中枢中「9 段升级 / 中枢扩展候选」的索引（仅标注、不改走势类型）。

    z.lines 元素随级别而异：L0=线段、L≥1=走势类型(ZSLX)，故「9 段」泛指 9 个
    次级别单元。
    注：calculate 的 pending 分支传入的 H2 中枢未剥离开段(含末段读法)，9 段阈值
    在该路径偏松一档——pending 仅供审图标注。"""
    out: List[int] = []
    for i, z in enumerate(done_zss):
        if len(z.lines) >= 9:                          # 9 段升级(9 个次级别单元)
            out.append(i)
        elif i > 0 and classify_rel(done_zss[i - 1], z) == "expand":  # 中枢扩展(相邻中枢本体相交)
            out.append(i)
    return out


def _dingli2_upgrade_forming(done_zss: List[ZS]) -> List[ZS]:
    """定理二升级预告(L020:39):相邻 done 中枢「核心区[ZD,ZG]分离 + 波动区间[DD,GG]
    重叠」⟺ 更大级别中枢正在形成。

    按**进行式**语义(L043 缠答:两中枢区间重合「必然扩展成」更大级别中枢,进行式非完成式)
    产 forming 中枢:done=False、核心区候选=波动重叠带 [max(DD),min(GG)],供注入下一级
    live_zss 作虚线预告;**不入 done 链**——完成确认仍走自然递归(3 个次级别走势类型重叠)。
    无未来函数:仅读已完成中枢的当下几何。趋势对(波动分离)与延伸族(核心重叠)不产。
    """
    # 只看**末对**(done_zss[-2], done_zss[-1]):进行式语义=当下正在形成;历史升级对已被
    # 走势演化消化(自然递归 L1 / 走势类型分段),不作预告——否则全历史升级对灌 live 层,
    # 图表满屏虚线(600519 笔塔实测 26 个)且违「正在形成」语义。每级至多 1 个预告。
    out: List[ZS] = []
    for i in range(max(1, len(done_zss) - 1), len(done_zss)):
        a, b = done_zss[i - 1], done_zss[i]
        vals = (a.zd, a.zg, b.zd, b.zg,
                getattr(a, "dd", None), getattr(a, "gg", None),
                getattr(b, "dd", None), getattr(b, "gg", None))
        if any(v is None for v in vals):
            continue
        if not (b.zd > a.zg or b.zg < a.zd):      # 核心区未分离 → 延伸族
            continue
        lo, hi = max(a.dd, b.dd), min(a.gg, b.gg)
        if lo >= hi:                              # 波动区间分离 → 趋势
            continue
        z = ZS(zs_type=getattr(a, "zs_type", "xd"),
               start=(a.lines[0] if a.lines else None),
               end=(b.lines[-1] if b.lines else None),
               zg=hi, zd=lo, gg=max(a.gg, b.gg), dd=min(a.dd, b.dd))
        z.lines = list(a.lines or []) + list(b.lines or [])
        z.line_num = len(z.lines)
        z.done = False
        z._dingli2_upgrade = True
        z._gg_cache, z._dd_cache, z._bounds_dirty = z.gg, z.dd, False
        out.append(z)
    return out


def _forming_dedup(forming: List[ZS], existing: List[ZS]) -> List[ZS]:
    """升级预告与本级已有(done/live)中枢时间范围重叠者丢弃——自然递归已捕获,避免双框。"""

    def span(z):
        ls = getattr(z, "lines", None) or []
        if not ls or ls[0].start is None or ls[-1].end is None:
            return None
        return (ls[0].start.k.k_index, ls[-1].end.k.k_index)

    spans = [s for s in (span(z) for z in existing) if s is not None]
    out = []
    for f in (forming or []):
        fs = span(f)
        if fs is None:
            continue
        if any(not (fs[1] < s[0] or fs[0] > s[1]) for s in spans):
            continue
        out.append(f)
    return out


class RecursiveBranchCalculator:
    """递归装配计算器。无状态，每次 calculate 全量重算。"""

    def __init__(self, l0_min_zs_lines: int = 5):  # 用户口径 2026-06-26(4→5);语义=lines(不含进入段)≥5,详见 cl._recursive_l0_min_zs_lines
        self.l0_min_zs_lines = int(l0_min_zs_lines or 5)
        # 递归层增量化:持久化各级 ZsBranchCalculator(内含持久 ZsCalculator)+ ZslxBranch,
        # 跨 calculate(跨 K)复用 → 各级 calculator 增量状态保留。L0 输入(units=xds/bis 浅
        # 拷贝)identity 稳定 → L0 增量立即生效;L1+ 输入(_as_units 每次 copy.copy)暂不稳定、
        # 仍全量(留级联阶段)。本实例须按「塔」(笔/段)分持久,见 cl._recursive_branch_calc。
        self._zs_branch_by_level: dict = {}
        self._zslx_calc = ZslxBranchCalculator()
        # L1+ 级联增量:_as_units 按走势类型值签名缓存 unit 副本。稳定走势类型(几何不变)→
        # 复用同一 unit 对象 → 上级 ZsCalculator 输入 identity 稳定 → 其增量 + 背驰/校正缓存
        # 对上级中枢级联生效。zslx_branch 每 K 产新 ZSLX 对象,但稳定者值签名不变 → 命中。
        self._units_cache: dict = {}

    @staticmethod
    def _zslx_key(zslx) -> tuple:
        """走势类型几何值签名(L1+ 级联缓存 key):上级 ZsCalculator 把走势类型当 LINE、用其
        _type/zs_high/zs_low/start/end 几何;稳定走势类型这些不变 → key 稳定 → 复用同一 unit。
        附 zss 段数防几何巧合碰撞。"""
        s = getattr(zslx, "start", None)
        e = getattr(zslx, "end", None)

        def fx(f):
            k = getattr(f, "k", None) if f is not None else None
            return None if k is None else (k.k_index, f.val)

        return (
            getattr(zslx, "_type", None),
            getattr(zslx, "zs_high", None), getattr(zslx, "zs_low", None),
            fx(s), fx(e), len(getattr(zslx, "zss", None) or []),
        )

    def _as_units_cached(self, zslxs: List[ZSLX], level: int) -> List[ZSLX]:
        """_as_units 的级联缓存版:稳定走势类型复用同一 unit 对象(身份稳定 → 上级 ZsCalculator
        增量 + 背驰/校正缓存级联生效)。unit.index 每轮按位置刷新(稳定前缀位置不变、无副作用)。
        key 含 level:防不同级别几何巧合相同的走势类型共用同一 unit(index 互相覆盖)。"""
        cache = self._units_cache
        if len(cache) > 8192:
            cache.clear()
        out: List[ZSLX] = []
        for i, zslx in enumerate(zslxs):
            key = (level, self._zslx_key(zslx))
            u = cache.get(key)
            if u is None:
                u = copy.copy(zslx)
                cache[key] = u
            u.index = i
            out.append(u)
        return out

    def calculate(
        self,
        xds: List[LINE],
        ld_provider: LdProvider,
        wzgx_config: str,
        frequency: Optional[str] = None,
        ld_provider_for_level: Optional[Callable[[int], LdProvider]] = None,
        zs_diversity: bool = False,
    ) -> List[LevelResult]:
        """把线段递归装配成多级中枢/走势类型层级树。

        每级：zs_branch(中枢+内联背驰) → zslx_branch(走势类型) → _as_units → 下一级。
        L0 构成段=线段、L≥1 构成段=走势类型;成枢门全级别统一 = l0_min(用户口径 2026-06-26:
        与核心区重叠段数·含离开段·不含进入段 ≥5;L≥1 升级中枢也用 5)。
        终止：扫不出中枢 / 走势类型 <3 / 触 _MAX_LEVELS。
        """
        if not xds:
            return []
        results: List[LevelResult] = []
        carry_forming: List[ZS] = []   # 上一级的定理二升级预告,注入本级 live_zss(O3b-lite)
        units: List[LINE] = list(xds)
        zslx_calc = self._zslx_calc    # 持久复用(见 __init__)
        level = 0
        while level < _MAX_LEVELS:
            min_lines = self.l0_min_zs_lines  # 用户口径 2026-06-26: 全级别统一(L0 与 L≥1 升级中枢都用 l0_min=5)
            # 换周期 MACD:各级用对应级别 ld_provider(L0→5m/L1→30m…);无 factory 退化用单一
            lp = ld_provider_for_level(level) if ld_provider_for_level is not None else ld_provider
            # 复用本级持久 ZsBranchCalculator(其持久 ZsCalculator 承载增量状态);ld/freq/wzgx
            # 每轮可变(同 CL 内实际稳定),更新即可;min_zs_lines 全级别统一=l0_min(用户口径)。
            zb = self._zs_branch_by_level.get(level)
            if zb is None:
                zb = ZsBranchCalculator(
                    ld_provider=lp, frequency=frequency,
                    wzgx=wzgx_config, min_zs_lines=min_lines,
                )
                self._zs_branch_by_level[level] = zb
            else:
                zb.ld_provider = lp
                zb.frequency = frequency
                zb.wzgx = wzgx_config
            res = zb.calculate(units)
            if zs_diversity and res.done_zss:
                from chanlun.core import zs_diversity as _zsd
                res = _zsd.refine(res, units, min_lines, lp, frequency)
            if not res.done_zss:
                # 右边缘只剩 pending 高级中枢(未被离开段确认完成)：记录其中枢与 live 背驰
                # 后再终止——让层级树展示到右边缘正在形成的高级中枢(未完成无法切走势类型，
                # 不上卷)。此处放宽“仅采用已完成中枢”的默认口径，允许右边缘 pending 中枢入树。
                pend = [h for h in res.live if h.node1 == "leave"]
                inject = _forming_dedup(carry_forming, [h.zs for h in pend])
                carry_forming = []
                if pend or inject:
                    results.append(LevelResult(
                        level=level, zss=[h.zs for h in pend],
                        done_divergence=[h.divergence for h in pend],
                        zslxs=[], upgrade_idx=_mark_upgrades([h.zs for h in pend]),
                        units=list(units), live_zss=inject,
                    ))
                break
            zslxs = zslx_calc.calculate(res.done_zss, res.done_divergence)
            assert zslxs, "done_zss 非空时 zslx_branch 必产 ≥1 走势类型(末段 done=False)"
            # 右边缘正在形成的未完成中枢(core=仍开读法,done=False):本级已有 done 中枢,
            # 不再走上面「无 done 放宽一档」分支,但仍要把这个未完成中枢带给图表(否则
            # 用户看不到正在形成的中枢)。只入 live_zss、不入 zss,不扰动买卖点/走势类型。
            forming = [h.zs for h in res.live if h.node1 == "core"]
            live_qs = [
                (h.zs, h.divergence) for h in res.live
                if h.node1 == "leave" and h.divergence is not None
                and h.divergence.is_beichi and h.divergence.kind == "qs"
            ]   # R78:进行中趋势背驰段(离开读法、provisional qs 背驰),供区间套候选下推确认
            results.append(LevelResult(
                level=level, zss=res.done_zss, done_divergence=res.done_divergence,
                zslxs=zslxs, upgrade_idx=_mark_upgrades(res.done_zss), units=list(units),
                live_zss=forming + _forming_dedup(carry_forming, res.done_zss + forming),
                live_qs_divergence=live_qs,
            ))
            carry_forming = _dingli2_upgrade_forming(res.done_zss)   # 本级升级对 → 下一级预告
            if len(zslxs) < 3:
                break
            units = self._as_units_cached(zslxs, level)
            level += 1
        if carry_forming:
            # 递归止于 L_k 而定理二升级预告属于 L_{k+1}(自然递归尚够不到的高级别正在形成
            # ——预告最有价值的场景):追加仅含 live_zss 的层,图表画虚线框。
            nxt = (results[-1].level + 1) if results else 0
            results.append(LevelResult(
                level=nxt, zss=[], done_divergence=[], zslxs=[],
                live_zss=list(carry_forming),
            ))
        return results
