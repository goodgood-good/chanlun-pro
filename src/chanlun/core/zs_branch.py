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
