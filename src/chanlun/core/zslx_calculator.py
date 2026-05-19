"""zslx_calculator.py — 级别无关的缠论走势类型划分(子项目③)。

用双信号状态机把中枢序列切成走势类型(ZSLX)：边界由「背驰」或「中枢方向
断裂」任一触发。盘整 = 1 中枢；趋势 = ≥2 依次同向中枢。复用 ② 的背驰内核。

原文依据见 docs/chanlun_core_redesign_3_zslx_design.md §2。
"""

from __future__ import annotations

from typing import List, Optional, Tuple

from chanlun.core.beichi_calculator import LdProvider, beichi_pz, beichi_qs, is_qs
from chanlun.core.cl_interface import LINE, ZS, ZSLX


def _classify(zss: List[ZS], wzgx_config: str) -> Tuple[str, str]:
    """对一个走势类型的中枢列表分类，返回 (zslx_type, direction)。

    1 中枢 → ("盘整", 净方向)；≥2 中枢 → ("上涨"/"下跌", "up"/"down")。
    """
    if len(zss) == 1:
        lines = zss[0].lines
        net_up = lines[-1].end.val >= lines[0].start.val
        return "盘整", ("up" if net_up else "down")
    direction = is_qs(zss[0], zss[1], wzgx_config)
    return ("上涨" if direction == "up" else "下跌"), direction
