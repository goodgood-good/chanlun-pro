"""走势分解多样性精炼层。纯函数，零副作用，不改 ZsCalculator/zs_branch。

职责：在贪婪中枢之上做后处理，按三类买卖点切分跨点的中枢、对巨型中枢按移动核心
断枢，使中枢划分更贴合走势分解，避免长盘整被吸成单个巨枢。

移动核心 running-overlap 规则：对 ≥ upgrade_seg 段的巨型中枢，改用「任意两段间的
滑动重叠交集」断枢，把缓漂盘整（每段都与紧邻段重叠但首尾不重叠）切断为若干子中枢。
这是工程口径，与固定核心延伸不同。适用场景：长盘整缓漂巨枢的细分。
"""
from __future__ import annotations

import copy
import statistics
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
    """三类点精炼：把每个跨三类点的贪婪中枢在三类点处收口，余段复用 ZsCalculator 重扫。

    三类买卖点视为中枢延伸的结束信号。

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
            # 注：_first_third_class 已保证 i >= core=3，截断后本体至少 3 段（合法），
            # 故不再用 i < min_lines 过滤（level0 min_lines=4 会误杀 i=3 的合法切点）。
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

    工程口径（见模块 docstring 说明）：用滑动核心而非固定核心。

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

    工程口径：子中枢核心区由组内前三段确定（仍用首三段重叠公式），
    但「哪三段是首三段」取决于 running-overlap 断枢点，与固定核心有差异。
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
    """巨型中枢断枢：对 ≥ upgrade_seg 段的巨型中枢用 running-overlap 断枢。

    流程（每个中枢独立处理）：
      1. 取 segs = z.lines（本体段，忽略 z.end 离开段，纯函数不修改原中枢）。
      2. 若 len(segs) < upgrade_seg → 原样透传。
      3. _running_overlap_groups(segs, min_lines) 分组：
         - 仅 1 组或空组 → 原样透传（无缓漂，不断枢）。
         - 多组 → 每组调 _build_zs 构建子中枢，过滤 None，替换原中枢。

    工程口径（见模块 docstring）。纯函数，不改 done_zss 以外的状态，
    满足 inc==batch（输入相同段序列得相同结果）。

    参数
    ----
    zss         : 待精炼中枢列表
    min_lines   : 最小构成段数（与 ZsCalculator.min_zs_lines 同口径）
    upgrade_seg : 触发断枢的段数门槛（默认 9）

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


_YW_TOL_FRAC = 0.25     # 「离开核心」容差 = 核心宽 × 此系数，滤分位级噪声(防 1 分钱误判离开)
_YW_TREND_K = 3.0       # 趋势段判据:百分比涨跌幅 > 此系数 × 中位幅度 → 趋势/离开段(中枢硬边界)


def _pct_mag(seg: LINE) -> float:
    """段的百分比涨跌幅 |end/start − 1|（尺度无关，避开价位高低与核心宽循环依赖）。"""
    s = seg.start.val
    return abs(seg.end.val - s) / s if s else 0.0


def _trend_flags(units: List[LINE], k: float = _YW_TREND_K) -> List[bool]:
    """标记趋势段:百分比幅度 > k × 中位幅度。

    用户口径(2026-06-20, TSLA 1m 手标):中枢小而紧凑、趋势裸露。大摆动方向段(暴跌/飙升)的
    价格区间虽扫过震荡区、会被 core_interval 误当核心三段(区间重叠类判据全证伪),故改按
    「段移动幅度」剥离:中位数 + 百分比 = 尺度无关，不依赖核心宽(避免循环依赖 + 误杀正常震荡段)。
    """
    # 审计 D2-HIGH-3(用户口径 2026-06-26): 原用全量 median 作 scale → 追加段改 median →
    # 已结算前缀段 trend 标记翻转(inc≠batch 非确定性、未来函数式污染)。改用【因果前缀中位数】
    # scale_i = median(mags[0:i+1]): 段 i 分类只依赖到 i 为止的段, 追加未来段不改其分类 →
    # inc==batch 确定性。段 0 无"典型"可超 → 恒非趋势(median=自身, m>k*m 对 k≥1 恒 False)。
    mags = [_pct_mag(u) for u in units]
    if not mags:
        return []
    flags: List[bool] = []
    for i in range(len(mags)):
        scale = statistics.median(mags[: i + 1])
        flags.append(scale > 0 and mags[i] > k * scale)
    return flags


def _zs_in_run(run: List[LINE], min_lines: int) -> List[List[LINE]]:
    """在一段连续「非趋势段」run 内找中枢（原文 L020 中心定理一 + L054「一个具体走势的分析」口径）。

    - 核心 [ZD,ZG] = 前 3 段 ``core_interval``（前两个 Z 走势段的重叠，宽松、非 4 段裸重叠）。
    - 延伸看「离开核心后是否回到核心」：某段端点显著离开核心（超容差 _YW_TOL_FRAC×核心宽）
      且下一段端点不回到核心附近 → 三类点（统一三买/三卖/反向 overshoot）→ 中枢终结，
      该「离开段」并入本中枢跨度（L054:15）。震荡（离开后回核心）则继续延伸。
    - 两个同级别中枢之间必留次级别连接（L054:29）：离开段之后另起，连接腿不入任何中枢。
    - 跨度（含离开段）< ``min_lines`` → 不成枢，首段归方向腿（化解高位假枢）。
    """
    groups: List[List[LINE]] = []
    n = len(run)
    i = 0
    while i + 2 < n:
        iv = core_interval(run[i], run[i + 1], run[i + 2])
        if iv is None:                       # 前 3 段不重叠 → 方向腿，前移
            i += 1
            continue
        zd, zg = iv
        tol = _YW_TOL_FRAC * (zg - zd)
        k = i + 2
        leave_k: Optional[int] = None
        while k + 2 < n:
            le, re = run[k + 1], run[k + 2]
            le_out = le.end.val > zg + tol or le.end.val < zd - tol       # 端点显著离开核心
            re_in = (zd - tol) <= re.end.val <= (zg + tol)                # 下一段端点回核心附近
            if le_out and not re_in:         # 离开 + 不回 → 三类点终结，离开段=run[k+1]
                leave_k = k + 1
                break
            k += 1                           # 震荡(离开后回核心) → 继续延伸
        hi = leave_k if leave_k is not None else min(k + 1, n - 1)
        if hi - i + 1 < min_lines:           # 真振荡跨度不足 → 非中枢，首段归方向腿
            i += 1
            continue
        groups.append(list(run[i:hi + 1]))
        i = hi + 1                           # 离开段后另起(=两中枢间的次级别连接，L054:29)
    return groups


def _yuanwen_groups(units: List[LINE], min_lines: int) -> List[List[LINE]]:
    """走势感知中枢分组 = 幅度法剥离趋势段 + run 内 L020/L054 中枢构成。

    用户拍板口径(2026-06-20, TSLA 1m 手标范例):中枢小而紧凑、趋势裸露(大摆动方向段不包进
    中枢)。先用 ``_trend_flags`` 把移动幅度远超中位(>_YW_TREND_K×)的趋势/离开段标出、作中枢
    硬边界,再在每段连续「非趋势段」run 内按 ``_zs_in_run`` 找中枢；趋势段本身不入任何中枢
    (=离开/进入/连接腿)。

    纯函数；返回中枢的构成段组列表（按时序）。口径在 002299(巨枢区 6/6)/TSLA/AAPL 三标的
    对齐(k=3.0:002299 GT 不变、TSLA 大摆动巨枢拆开、AAPL 最大枢 12→8 段)。无趋势段时退化为
    单一 run = 原走势感知划分(等价历史行为)。
    """
    is_trend = _trend_flags(units)
    groups: List[List[LINE]] = []
    n = len(units)
    i = 0
    while i < n:
        if is_trend[i]:                      # 趋势段 = 中枢硬边界,不入中枢
            i += 1
            continue
        j = i
        while j < n and not is_trend[j]:     # 取最大连续非趋势 run
            j += 1
        groups.extend(_zs_in_run(units[i:j], min_lines))
        i = j
    return groups


def refine(
    res: ZsBranchResult,
    units: List[LINE],
    min_lines: int,
    ld_provider=None,
    frequency=None,
) -> ZsBranchResult:
    """走势感知中枢重建入口：用 ``_yuanwen_groups`` 从 units 重建 L0 中枢，化解贪婪巨枢。

    接线：recursive_branch 循环里在 zb.calculate(units) 之后、zslx_calc.calculate(...)
    之前，当开关 recursive_zs_diversity=True 时调用。替代旧 R4/R3 后处理——后者在贪婪
    巨枢之上做切分（治标），本版直接按原文从段序列重建中枢（治本）。

    ZS 对象用 ``_build_zs`` 构建（start/end/lines/zd/zg 与 R3 同约定）；zs_type/level 取
    res.done_zss[0] 为模板。live / freeze_idx 透传；done_divergence 按重建后中枢数对齐重算。
    旧 _refine_r4 / _refine_r3 保留(暂不删)以便回退对比。
    """
    if not res.done_zss:
        return res
    ref = res.done_zss[0]
    zss = [z for g in _yuanwen_groups(units, min_lines)
           if (z := _build_zs(g, ref)) is not None]
    return ZsBranchResult(
        done_zss=zss,
        live=res.live,
        freeze_idx=res.freeze_idx,
        done_divergence=_recompute_divergence(zss, ld_provider, frequency),
    )


def emit_l1_upgrades(levels, min_lines: int, upgrade_seg: int = 9, ld_provider=None, frequency=None):
    """【已弃用·违原文 R7，cl 已停止调用】紧凑横盘 ≥upgrade_seg 段延伸中枢 → 注入同核心 level1 中枢。

    弃用原因（知乎 67-71 课 R7 + 实证）：升级不是把单个 L0 巨枢同核心 relabel 到高一级——单个延伸
    巨枢只是一个盘整走势类型、不能自成高级别中枢；高级别中枢须 3 个次级别走势类型重叠构成、核心取
    走势类型重叠、锚定最后一个次级别中枢。这正是 recursive_branch 自然递归（走势类型→L1，每级过
    refine）已忠实做到。本函数在自然 L1 之上多塞同核心单枢（002299 多 6 个伪升级），保留仅供回退。

    紧凑巨枢（子中枢核心区重叠=延伸，非扩张）是一个合法延伸中枢，升级到高一级别
    （同核心 [ZD,ZG]、level+1），而非在 level0 拆开（拆=制造重叠的同级别中枢）。
    故此为加法：level0 延伸巨枢保留不动，仅向 level1 注入升级中枢（安全，不改
    level0/买卖点/zslx）。与 _refine_r3(running-overlap，管缓漂核心漂移者) 互补：
    本函数管紧凑全重叠者。
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
            continue                                  # 缓漂(已由断枢处理)，非紧凑延伸
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
