"""走势分解多样性精炼层（方案A）。纯函数，零副作用，不改 ZsCalculator/zs_branch。

原文锚:
  L054:47  选最有意义的走势分解
  L038:215 三类点=延伸结束信号
  L033:17  九段升级条件
  L043:57  巨型中枢非法判定

工程补充（R3 v1）：
  running-overlap 移动核心规则——**偏离中心定理一（固定核心延伸）的工程选择**。
  原文中心定理一以首三段构成的固定核心区延伸中枢，不随新段滑动。本规则对
  ≥ upgrade_seg 段的巨型中枢改用「任意两段间的滑动重叠交集」断枢，操作上
  把缓漂盘整（每段都与紧邻段重叠但首尾不重叠）切断为若干子中枢。这与
  min_zs_lines=4（原文字面 3 段，项目门槛 4 段）同性质——均为工程口径而非
  原文字面。适用场景：002299 类 1683 根缓漂巨枢（v1 目标 <1000 根）。
"""
from __future__ import annotations

import copy
from typing import List, Optional

from chanlun.core.types import LINE, ZS
from chanlun.core.zs_branch import ZsBranchResult, core_interval
from chanlun.core.zs_calculator import ZsCalculator


def _first_third_class(
    segs: List[LINE], zd: float, zg: float, core: int = 3
) -> Optional[int]:
    """返回 segs 中首个三类买卖点的离开段下标（核心区后）。

    对齐 bs_branch._third_class 口径：
      离开上 end > zg  且 回试 end >= zg  → 三买
      离开下 end < zd  且 回试 end <= zd  → 三卖

    参数
    ----
    segs : 线段列表（含核心区段 + 延伸段）
    zd   : 中枢下沿 ZD
    zg   : 中枢上沿 ZG
    core : 核心区最少段数，检测从下标 core 开始（默认 3）

    返回
    ----
    首个满足条件的离开段下标 i（i >= core），无则 None。
    """
    for i in range(core, len(segs) - 1):
        leave = segs[i]
        retest = segs[i + 1]
        if leave._type == "up" and leave.end.val > zg and retest.end.val >= zg:
            return i
        if leave._type == "down" and leave.end.val < zd and retest.end.val <= zd:
            return i
    return None


def _zs_segs(z: ZS) -> List[LINE]:
    """中枢覆盖的完整段序列（本体 lines + 离开段 end，去重）。"""
    segs = list(z.lines)
    if z.end is not None and (not segs or z.end is not segs[-1]):
        segs.append(z.end)
    return segs


def _truncate(z: ZS, i: int) -> ZS:
    """把中枢收口到下标 i 处：本体 = segs[:i]，离开段 = segs[i]。"""
    segs = _zs_segs(z)
    z2 = copy.copy(z)
    z2.lines = list(segs[:i])
    z2.end = segs[i]
    z2.done = True
    z2._bounds_dirty = True
    if hasattr(z2, "update_boundaries"):
        z2.update_boundaries()
    return z2


def _refine_r4(zss: List[ZS], units: List[LINE], min_lines: int) -> List[ZS]:
    """R4 精炼：把每个跨三类点的贪婪中枢在三类点处收口，余段复用 ZsCalculator 重扫。

    原文锚: L038:215 三类点=延伸结束信号

    参数
    ----
    zss       : 待精炼中枢列表（通常来自 ZsCalculator.calculate）
    units     : 与 zss 对应的基础段列表（目前仅透传给递归调用，供未来使用）
    min_lines : 传给 ZsCalculator(min_zs_lines) 的最小构成段数

    返回
    ----
    精炼后的中枢列表（保持时序，可能比输入更多）。
    """
    out: List[ZS] = []
    for z in zss:
        segs = _zs_segs(z)
        i = _first_third_class(segs, z.zd, z.zg, core=3)
        if i is None:
            # 无中间三类点 → 原样保留
            # 注：_first_third_class 已保证 i >= core=3，截断后本体至少 3 段（原文合法），
            # 故不再用 i < min_lines 过滤（L0 min_lines=4 会误杀 i=3 的合法切点）。
            out.append(z)
            continue
        # 收口：中枢截止到下标 i
        out.append(_truncate(z, i))
        # 余段（三类点之后）交给引擎重扫，递归精炼
        tail = segs[i + 1 :]
        if len(tail) >= min_lines:
            rescanned = ZsCalculator(min_zs_lines=min_lines).calculate(tail)
            out.extend(_refine_r4(rescanned, tail, min_lines))
    return out


def _running_overlap_groups(segs: List[LINE], min_seg: int) -> List[List[LINE]]:
    """按移动核心 running-overlap 规则把段序列分组。

    维护一个「所有已入组段的滑动重叠交集」[lo, hi]：
      - 加入新段后仍 lo < hi（严格非空）→ 归入当前组；
      - 否则重叠为空 → 当前组若 ≥ min_seg 则成组保留，新段另起新组。

    ⚠ 工程口径（见模块 docstring R3 v1 说明）：偏离原文中心定理一固定核心。

    参数
    ----
    segs    : 段序列（按时序）
    min_seg : 每组最少段数门槛（不足则丢弃）

    返回
    ----
    满足 ≥ min_seg 的分组列表（保持时序）。
    """
    groups: List[List[LINE]] = []
    cur: List[LINE] = []
    lo: Optional[float] = None
    hi: Optional[float] = None
    for s in segs:
        nlo = s.zs_low if lo is None else max(lo, s.zs_low)
        nhi = s.zs_high if hi is None else min(hi, s.zs_high)
        if lo is None or nlo < nhi:
            cur.append(s)
            lo, hi = nlo, nhi
        else:
            if len(cur) >= min_seg:
                groups.append(cur)
            cur, lo, hi = [s], s.zs_low, s.zs_high
    if len(cur) >= min_seg:
        groups.append(cur)
    return groups


def _build_zs(group: List[LINE], ref_zs: "ZS") -> Optional["ZS"]:
    """从段组构建子中枢，以 ref_zs 为模板复制 zs_type/level。

    核心区由前三段按 core_interval 计算；None（退化重叠）则跳过该组。
    子中枢 lines = group，done=True，boundaries 重算。

    ⚠ 工程口径：子中枢核心区由组内前三段确定（仍用原文首三段公式），
    但「哪三段是首三段」取决于 running-overlap 断枢点，与原文固定核心有差异。
    """
    if len(group) < 3:
        return None
    iv = core_interval(group[0], group[1], group[2])
    if iv is None:
        return None
    z = ZS(
        zs_type=ref_zs.zs_type,
        start=group[0],
        end=group[-1],
        zg=iv[1],
        zd=iv[0],
        level=ref_zs.level,
    )
    z.lines = list(group)
    z.done = True
    z._bounds_dirty = True
    z.update_boundaries()
    return z


def _refine_r3(
    zss: List["ZS"],
    min_lines: int,
    upgrade_seg: int = 9,
) -> List["ZS"]:
    """R3 v1 精炼：对 ≥ upgrade_seg 段的巨型中枢用 running-overlap 断枢。

    流程（每个中枢独立处理）：
      1. 取 segs = z.lines（本体段，忽略 z.end 离开段，纯函数不修改原中枢）。
      2. 若 len(segs) < upgrade_seg → 原样透传。
      3. _running_overlap_groups(segs, min_lines) 分组：
         - 仅 1 组或空组 → 原样透传（无缓漂，不断枢）。
         - 多组 → 每组调 _build_zs 构建子中枢，过滤 None，替换原中枢。

    ⚠ 工程口径（见模块 docstring R3 v1）。纯函数，不改 done_zss 以外的状态，
    满足 inc==batch（输入相同段序列得相同结果）。

    参数
    ----
    zss         : 待精炼中枢列表
    min_lines   : 最小构成段数（与 ZsCalculator.min_zs_lines 同口径）
    upgrade_seg : 触发 R3 的段数门槛（默认 9，即九段升级前兆）

    返回
    ----
    精炼后的中枢列表（保持时序，可能比输入更多）。
    """
    out: List["ZS"] = []
    for z in zss:
        segs = list(z.lines)
        if len(segs) < upgrade_seg:
            out.append(z)
            continue
        groups = _running_overlap_groups(segs, min_lines)
        if len(groups) <= 1:
            out.append(z)
            continue
        for g in groups:
            sub = _build_zs(g, z)
            if sub is not None:
                out.append(sub)
    return out


def _recompute_divergence(zss, ld_provider, frequency):
    """为精炼后中枢重算 done_divergence（复用 ZsBranchCalculator._divergence_for，
    a=z.start 进入段 c=z.end 离开段 is_beichi + 趋势/盘整 kind）。ld_provider 为空则全 None。"""
    from chanlun.core.zs_branch import ZsBranchCalculator
    if ld_provider is None:
        return [None] * len(zss)
    zb = ZsBranchCalculator(ld_provider=ld_provider, frequency=frequency)
    out = []
    prev = None
    for z in zss:
        try:
            out.append(zb._divergence_for(z, prev, live=False))
        except Exception:
            out.append(None)
        prev = z
    return out


def refine(
    res: ZsBranchResult,
    units: List[LINE],
    min_lines: int,
    ld_provider=None,
    frequency=None,
) -> ZsBranchResult:
    """R4 编排入口：对 res.done_zss 施 _refine_r4，返回新 ZsBranchResult。

    Task 4 接线：recursive_branch 循环里在 zb.calculate(units) 之后、
    zslx_calc.calculate(...) 之前，当开关 recursive_zs_diversity=True 时调用。

    live / freeze_idx 透传（未完成中枢不精炼）；done_divergence 按精炼后中枢数
    对齐，切分新增的中枢置 None（下游内联背驰重算）。

    原文锚: L038:215 三类点=延伸结束信号（中枢到三类点即告完成）
    """
    if not res.done_zss:
        return res
    zss = _refine_r4(res.done_zss, units, min_lines)
    zss = _refine_r3(zss, min_lines)
    return ZsBranchResult(
        done_zss=zss,
        live=res.live,
        freeze_idx=res.freeze_idx,
        done_divergence=_recompute_divergence(zss, ld_provider, frequency),
    )


def emit_l1_upgrades(levels, min_lines: int, upgrade_seg: int = 9, ld_provider=None, frequency=None):
    """L033:17 升级：紧凑横盘 ≥upgrade_seg 段延伸中枢 → 注入同核心的 L1 中枢。

    缠论定论：紧凑巨枢（子中枢核心区重叠=延伸，非 is_kuozhan 扩张）是一个合法延伸中枢
    （中心定理一/L017:385），按 L033:17 升级到高一级别（同核心 [ZD,ZG]、level+1），
    而非在 L0 拆开（拆=制造重叠同级别中枢=§F-B）。故此为**加法**：L0 延伸巨枢保留不动，
    仅向 L1 注入升级中枢（安全，不改 L0/买卖点/zslx）。与 _refine_r3(running-overlap，
    管缓漂核心漂移者) 互补：emit_l1_upgrades 管紧凑全重叠者。
    """
    if not levels:
        return levels
    import dataclasses

    from chanlun.core.recursive_branch import LevelResult
    from chanlun.core.types import ZS

    l0 = levels[0]
    ups = []
    for z in l0.zss:
        segs = list(z.lines)
        if len(segs) < upgrade_seg:
            continue
        if len(_running_overlap_groups(segs, min_lines)) > 1:
            continue                                  # 缓漂(R3 v1 已断)，非紧凑延伸
        u = ZS(zs_type=z.zs_type, start=segs[0], end=segs[-1],
               zg=z.zg, zd=z.zd, level=z.level)        # 紧凑延伸 → 升级中枢(同核心)
        u.lines = list(segs)
        u.done = True
        u._bounds_dirty = True
        u.update_boundaries()
        ups.append(u)
    if not ups:
        return levels
    new = list(levels)
    if len(new) >= 2:
        l1 = new[1]
        merged = sorted(list(l1.zss) + ups, key=lambda z: z.lines[0].start.k.k_index)
        new[1] = dataclasses.replace(l1, zss=merged,
                                     done_divergence=_recompute_divergence(merged, ld_provider, frequency))
    else:
        new.append(LevelResult(level=1, zss=ups, zslxs=[],
                               done_divergence=_recompute_divergence(ups, ld_provider, frequency)))
    return new
