"""多级三类（扩展）买卖点。

把单级（L0）三类买卖点扩展到多级:对各级 LevelResult 中枢出三买/三卖,复用
BsBranchCalculator._third_class 并标记归属级别。孤立、不接 CL、不动旧
bs_point_calculator。
"""
from __future__ import annotations

from typing import List

from chanlun.core.recursive_branch import LevelResult
from chanlun.core.zs_branch import ZsBranchResult
from chanlun.core.bs_branch import BuySellPoint, BsBranchCalculator


class Bs3BranchCalculator:
    """多级三类买卖点计算器。无状态，每次 calculate 全量重算。"""

    def calculate(self, levels: List[LevelResult]) -> List[BuySellPoint]:
        """各级中枢出三类买卖点。复用 _third_class,标记归属级别。"""
        out: List[BuySellPoint] = []
        base = BsBranchCalculator()
        for lr in levels:
            zr = ZsBranchResult(                          # _third_class 只读 done_zss+lines,
                done_zss=lr.zss, live=[], freeze_idx=0,   # live/freeze_idx/done_divergence 占位
                done_divergence=lr.done_divergence,
            )
            for p in base._third_class(zr, lr.units):   # 复用单级三类逻辑
                p.level = lr.level                       # 标归属级别
                out.append(p)
        return out
