"""zs_branch.py — P1 中枢多假设结构核（子项目①·宪法 §2/§3.5/§4 结构层）。

单级别、以确定性线段为输入，产出「冻结的已完成中枢 + 右边缘多假设分支池」。
本模块**不依赖、也不改动** zs_calculator.py 与任何生产链路（零回归风险）。

口径（宪法 §3.5）：
- 成中枢的重叠用严格 `<`（ZD<ZG 才算非退化重叠）。
- 延伸/扩张的「触及」用闭区间 `<=`（触边即算）。

不含：背驰（H2a/H2b）、升级/扩张实体化、买卖点、区间套、增量——见后续子项目。
"""

from __future__ import annotations

import copy
from dataclasses import dataclass
from typing import List, Optional, Tuple

from chanlun.core.cl_interface import LINE, ZS
from chanlun.core.zs_calculator import ZsCalculator


def core_interval(seg_a: LINE, seg_b: LINE, seg_c: LINE) -> Optional[Tuple[float, float]]:
    """前三段重叠的核心区间 [ZD, ZG]（第18课严格公式）。

    ZD=max(三段低), ZG=min(三段高)；严格 ZD<ZG 才算非退化重叠，否则 None。

    primitive：calculate 已委托 ZsCalculator 找中枢、本函数当前未接线，留作
    下游（P2+ 延伸/扩张判定）复用的缠论几何基元。
    """
    zd = max(seg_a.zs_low, seg_b.zs_low, seg_c.zs_low)
    zg = min(seg_a.zs_high, seg_b.zs_high, seg_c.zs_high)
    if zd >= zg:
        return None
    return (zd, zg)


def envelope(lines: List[LINE]) -> Tuple[float, float]:
    """中枢包络 [DD, GG]：DD=min(所有段低), GG=max(所有段高)（第20课瞬间波动区间）。"""
    dd = min(ln.zs_low for ln in lines)
    gg = max(ln.zs_high for ln in lines)
    return (dd, gg)


def touches(seg: LINE, lo: float, hi: float) -> bool:
    """线段是否触及闭区间 [lo, hi]（延伸/扩张口径：触边即算，对应中心定理二的 ≥/≤）。

    primitive：同 ``core_interval``，当前未接线，留作下游复用。
    """
    return max(seg.zs_low, lo) <= min(seg.zs_high, hi)


def body_envelope(zs: ZS) -> Tuple[float, float]:
    """中枢本体包络 (DD, GG)：只取前 3 段定义段，剔除延伸/离开段的远摆。

    若用完整 gg/dd（含离开段），离开段总朝下一中枢延伸 → 相邻中枢包络恒重叠
    → 趋势恒判不出（only-3rd-bspoint 根因 / 第33课 a+A+b+B+c）。故节点③ 趋势/
    扩张判定必须用本体包络。无 lines 时退化用 zs.dd/zs.gg（测试/边界）。
    """
    if not zs.lines:
        return (zs.dd, zs.gg)
    return envelope(zs.lines[:3])


def classify_rel(prev: ZS, cur: ZS) -> str:
    """节点③：相邻中枢关系（中心定理二，**本体包络**口径）。

    比较两中枢的本体包络（剔除离开段远摆）：后 DD>前GG → "trend_up"；
    后 GG<前DD → "trend_down"；否则本体相交 → "expand"（升级候选，P4 实体化）。
    """
    p_dd, p_gg = body_envelope(prev)
    c_dd, c_gg = body_envelope(cur)
    if c_dd > p_gg:
        return "trend_up"
    if c_gg < p_dd:
        return "trend_down"
    return "expand"


def _zone_dist(v: float, zd: float, zg: float) -> float:
    """价 v 到中枢区间 [zd,zg] 的距离（区间内为 0）。"""
    if zd <= v <= zg:
        return 0.0
    return min(abs(v - zd), abs(v - zg))


def correct_entry(zs: ZS, min_lines: int = 4) -> ZS:
    """进入段校正（原文第20课回升/回调形成 + line 21650「别把中枢之前的混进来」）。

    委托的 ZsCalculator 用「第一根与 [ZD,ZG] 几何重叠的段即核心」的口径，会把
    *方向性升/跌入段* 误当中枢第一段（审图 #3 的病）。原文口径：进入段必须**朝
    中枢走、升/跌进区间**；若引擎认的进入段 ``zs.start`` **背离**区间（其终点比
    起点离区间更远），则它不是真进入段——真进入段是 ``zs.lines[0]`` 那根升/跌入
    段，中枢起点右移到 ``lines[1]``，``zd/zg`` 由新前三段（即原文的 Z 走势段）重算。

    返回校正后的中枢（背离才动，否则原样返回）。
    """
    s = zs.start
    if s is None or s.start is None or s.end is None:
        return zs                                  # 开头中枢无进入段可测
    if len(zs.lines) < min_lines + 1:
        return zs                                  # 移除首段后不足成中枢，保守不动
    if _zone_dist(s.end.val, zs.zd, zs.zg) <= _zone_dist(s.start.val, zs.zd, zs.zg):
        return zs                                  # 进入段朝中枢走 → 引擎认对了
    new_core = zs.lines[1:]                         # 背离 → lines[0] 才是真进入段
    iv = core_interval(new_core[0], new_core[1], new_core[2])
    if iv is None:
        return zs
    z = copy.copy(zs)
    z.start = zs.lines[0]                           # 真进入段 = 升/跌入段
    z.lines = list(new_core)
    z.zd, z.zg = iv
    z._bounds_dirty = True
    z.update_boundaries()                           # 重算 gg/dd 包络
    return z


@dataclass
class ZsHypothesis:
    """右边缘的一个中枢读法（一个 live 分支）。"""

    zs: ZS                            # 该读法下的中枢对象
    node1: str                        # 节点①: "core"(H1·末段为核心/延伸) | "leave"(H2·末段为离开段/完成)
    rel_prev: Optional[str] = None    # 节点③: "trend_up"|"trend_down"|"expand"|None(无前中枢)
    upgrade: bool = False             # 节点②: True=已达 9 段触发升级（本计划只标记，不实体化）


@dataclass
class ZsBranchResult:
    """单级别一次 calculate 的产出。"""

    done_zss: List[ZS]                # 左侧已冻结的已完成中枢
    live: List[ZsHypothesis]          # 右边缘活分支（通常 1~2 个）
    freeze_idx: int                   # 冻结边界：< freeze_idx 的线段已 settled；live 分支从此起


class ZsBranchCalculator:
    """单级别多假设中枢引擎（薄 manager）。

    「找中枢」委托给已验证的 ``ZsCalculator``——look-ahead 完成判定、进入段提升、
    扫描定位都在那里解决（避免手搓重蹈覆辙）。本类只在其 pending 中枢上加右边缘
    H1/H2 分叉 + 节点③ 本体包络分类；已完成中枢即左侧冻结。

    本计划（P1）只到结构层：H2 表示「中枢结构完成」，不评背驰、不分 H2a/H2b。
    C3 段数封顶暂不启用（与 9 段升级标记有交互，留 P4 前小修）：``max_zs_lines``
    给极大值，让中枢能长到 ≥9 段以触发升级标记。
    """

    MIN_LINES = 4         # L0 最小中枢段数（含离开段）
    _NO_CAP = 10 ** 9     # 暂不封顶（C3 留 P4 前修）

    def calculate(self, lines: List[LINE]) -> ZsBranchResult:
        if not lines:
            return ZsBranchResult(done_zss=[], live=[], freeze_idx=0)
        zc = ZsCalculator(
            require_alternation=False,
            min_zs_lines=self.MIN_LINES,
            max_zs_lines=self._NO_CAP,
        )
        zc.calculate(lines)
        # 进入段校正（原文口径）：把被误当核心的升/跌入段还原成进入段，右移中枢起点
        done: List[ZS] = [correct_entry(z, self.MIN_LINES) for z in zc.zss]
        pending: Optional[ZS] = zc.pending_zs    # 右边缘进行中中枢（单解）
        if pending is not None:
            pending = correct_entry(pending, self.MIN_LINES)
        for z in done:                           # 合法性不变量（防回归）
            assert len(z.lines) >= self.MIN_LINES and z.zd < z.zg
        if pending is None:
            return ZsBranchResult(done_zss=done, live=[], freeze_idx=len(lines))
        prev = done[-1] if done else None
        return ZsBranchResult(
            done_zss=done,
            live=self._fork_pending(pending, prev),
            freeze_idx=self._line_index(pending.lines[0], lines),
        )

    @staticmethod
    def _line_index(target: LINE, lines: List[LINE]) -> int:
        """pending 中枢第一段在 lines 中的下标（按对象身份）。"""
        for k, ln in enumerate(lines):
            if ln is target:
                return k
        return len(lines)

    def _fork_pending(self, pending: ZS, prev: Optional[ZS]) -> List[ZsHypothesis]:
        """在 pending 中枢上分叉：H1=中枢仍开(done=False)，H2=末段为离开段(done=True)。

        两分支各自独立拷贝（含独立 lines 容器），互不串台——也不别名委托引擎的
        pending 对象；下游若对某分支实体化/延伸 lines 不会污染另一分支或上游。
        """
        upgrade = len(pending.lines) >= 9        # 第33课：9 段触发升级（本计划只标记）
        rel = classify_rel(prev, pending) if prev is not None else None
        h1 = self._branch_copy(pending, done=False)   # 仍开
        h2 = self._branch_copy(pending, done=True)    # 完成读法
        return [
            ZsHypothesis(zs=h1, node1="core", rel_prev=rel, upgrade=upgrade),
            ZsHypothesis(zs=h2, node1="leave", rel_prev=rel, upgrade=upgrade),
        ]

    @staticmethod
    def _branch_copy(zs: ZS, done: bool) -> ZS:
        """分支用的独立中枢拷贝：独立 lines 容器（元素 LINE 只读、可继续共享），置 done。"""
        z = copy.copy(zs)
        z.lines = list(zs.lines)
        z.done = done
        return z
