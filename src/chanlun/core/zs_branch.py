"""zs_branch.py — P1 中枢多假设结构核（子项目①·宪法 §2/§3.5/§4 结构层）。

单级别、以确定性线段为输入，产出「冻结的已完成中枢 + 右边缘多假设分支池」。
本模块**不依赖、也不改动** zs_calculator.py 与任何生产链路（零回归风险）。

口径（宪法 §3.5）：
- 成中枢的重叠用严格 `<`（ZD<ZG 才算非退化重叠）。
- 延伸/扩张的「触及」用闭区间 `<=`（触边即算）。

不含：背驰（H2a/H2b）、升级/扩张实体化、买卖点、区间套、增量——见后续子项目。
"""

from __future__ import annotations

from typing import List, Optional, Tuple

from chanlun.core.cl_interface import LINE


def core_interval(seg_a: LINE, seg_b: LINE, seg_c: LINE) -> Optional[Tuple[float, float]]:
    """前三段重叠的核心区间 [ZD, ZG]（第18课严格公式）。

    ZD=max(三段低), ZG=min(三段高)；严格 ZD<ZG 才算非退化重叠，否则 None。
    """
    zd = max(seg_a.zs_low, seg_b.zs_low, seg_c.zs_low)
    zg = min(seg_a.zs_high, seg_b.zs_high, seg_c.zs_high)
    if zd >= zg:
        return None
    return (zd, zg)
