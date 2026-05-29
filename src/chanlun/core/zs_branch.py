"""zs_branch.py — P1 中枢多假设结构核（子项目①·宪法 §2/§3.5/§4 结构层）。

单级别、以确定性线段为输入，产出「冻结的已完成中枢 + 右边缘多假设分支池」。
本模块**不依赖、也不改动** zs_calculator.py 与任何生产链路（零回归风险）。

口径（宪法 §3.5）：
- 成中枢的重叠用严格 `<`（ZD<ZG 才算非退化重叠）。
- 延伸/扩张的「触及」用闭区间 `<=`（触边即算）。

不含：背驰（H2a/H2b）、升级/扩张实体化、买卖点、区间套、增量——见后续子项目。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional, Tuple

from chanlun.core.cl_interface import LINE, ZS


def core_interval(seg_a: LINE, seg_b: LINE, seg_c: LINE) -> Optional[Tuple[float, float]]:
    """前三段重叠的核心区间 [ZD, ZG]（第18课严格公式）。

    ZD=max(三段低), ZG=min(三段高)；严格 ZD<ZG 才算非退化重叠，否则 None。
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
    """线段是否触及闭区间 [lo, hi]（延伸/扩张口径：触边即算，对应中心定理二的 ≥/≤）。"""
    return max(seg.zs_low, lo) <= min(seg.zs_high, hi)


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
    """单级别多假设中枢引擎。全量重算：左侧确定性中枢冻结，右边缘产出 H1/H2 分支。

    本计划（P1）只到结构层：H2 表示「中枢结构完成」，不评背驰、不分 H2a/H2b。
    """

    MIN_LINES = 4  # L0 最小中枢段数（含离开段）

    def calculate(self, lines: List[LINE]) -> ZsBranchResult:
        done: List[ZS] = []
        i = -1                                   # 进入段下标；-1=从开头无进入段中枢扫起
        n = len(lines)
        while i <= n - 1 - 3:                     # 需为 3 核心段留空间
            cs = i + 1                            # 核心起点
            interval = core_interval(lines[cs], lines[cs + 1], lines[cs + 2])
            if interval is None:
                i += 1
                continue
            zd, zg = interval
            core = [lines[cs], lines[cs + 1], lines[cs + 2]]
            j = cs + 3
            # 延伸：后续段触及核心则并入
            while j < n and touches(lines[j], zd, zg):
                core.append(lines[j])
                j += 1
            reached_end = (j >= n)
            if reached_end:
                # 右边缘：数据到此为止 → H1/H2 分叉（须 >= MIN_LINES 段）
                if len(core) >= self.MIN_LINES:
                    return ZsBranchResult(
                        done_zss=done,
                        live=self._fork(core, zd, zg, prev=(done[-1] if done else None)),
                        freeze_idx=cs,
                    )
                break
            else:
                # 第 j 段不触核心 → 离开确认，中枢 done（左侧冻结）
                if len(core) >= self.MIN_LINES:
                    done.append(self._make_zs(core, zd, zg, done_flag=True))
                    i = j - 1                    # 离开段作下一中枢进入段
                else:
                    i += 1                       # 不足 4 段，作废
        return ZsBranchResult(done_zss=done, live=[], freeze_idx=max(0, n))

    def _make_zs(self, core: List[LINE], zd: float, zg: float, done_flag: bool) -> ZS:
        zs = ZS(zs_type="xd", start=None, _type=core[1].type)
        zs.lines = list(core)
        zs.zg, zs.zd = zg, zd
        zs._bounds_dirty = True
        zs.update_boundaries()                   # 填 gg/dd 包络 + line_num
        zs.end = core[-1]
        zs.done = done_flag
        return zs

    def _fork(self, core: List[LINE], zd: float, zg: float, prev: Optional[ZS]) -> List[ZsHypothesis]:
        # H1：末段为核心，中枢仍开；H2：末段为离开段，中枢完成
        upgrade = len(core) >= 9                 # 第33课：3 本体 + 6 延伸 = 9 段 → 升级（本计划只标记）
        zs_h1 = self._make_zs(core, zd, zg, done_flag=False)
        zs_h2 = self._make_zs(core, zd, zg, done_flag=True)
        return [
            ZsHypothesis(zs=zs_h1, node1="core", upgrade=upgrade),
            ZsHypothesis(zs=zs_h2, node1="leave", upgrade=upgrade),
        ]
