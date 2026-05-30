"""zslx_branch.py — P4a 走势类型划分（基于 zs_branch 内联背驰）。

把 zs_branch 的 L0 已完成中枢序列(done_zss + done_divergence)切成走势类型(ZSLX)：
双信号边界——背驰(复用 done_divergence,不重判) + 方向断裂(classify_rel 本体包络)。
孤立、不接 CL、不依赖 beichi_calculator(背驰已在 done_divergence 里)。

设计见 docs/chanlun_core_redesign_4a_走势类型划分_design.md。
"""
from __future__ import annotations

from typing import List, Optional

from chanlun.core.cl_interface import ZS, ZSLX
from chanlun.core.zs_branch import DivergenceResult, classify_rel


class ZslxBranchCalculator:
    """级别无关的走势类型划分（基于 zs_branch 中枢+内联背驰）。无状态，全量重算。"""

    @staticmethod
    def _finalize(
        zss: List[ZS], start_idx: int, cur_dir: Optional[str], done: bool
    ) -> ZSLX:
        """把一个中枢列表收尾成 ZSLX：分类、边界(含进入/离开段 a/b)、包络。"""
        if len(zss) == 1:
            zslx_type = "盘整"
            z = zss[0]
            direction = "up" if z.lines[-1].end.val >= z.lines[0].start.val else "down"
        else:
            direction = "up" if cur_dir == "trend_up" else "down"
            zslx_type = "上涨" if direction == "up" else "下跌"
        # 走势类型边界 = 第一中枢进入段 a → 末中枢离开段 b（原文 a+A+b），缺则退化用核心段
        first = zss[0].start if zss[0].start is not None else zss[0].lines[0]
        last = zss[-1].end if zss[-1].end is not None else zss[-1].lines[-1]
        zslx = ZSLX(
            zslx_level=getattr(zss[0], "level", None),
            start=first.start, end=last.end,
            start_line=first, end_line=last,
            _type=direction, index=start_idx, done=done,
        )
        zslx.zss = list(zss)
        zslx.zslx_type = zslx_type
        # 喂回 zs_branch 备用(P4b)：ZsCalculator 靠构成段 zs_high/zs_low 判重叠
        zslx.zs_high = max(zs.gg for zs in zss)
        zslx.zs_low = min(zs.dd for zs in zss)
        return zslx
