"""bs1_branch.py — R5 多级一类买卖点(L1+ 趋势背驰一类)。

块R 升级路径(R5 后 get_kuozhan_levels 改走块R)需 L1+ 一类:各级(level>=1)的 qs 趋势
背驰离开段 → 一类(离开向下=1buy 跌势衰竭 / 向上=1sell)。L0 一类仍由 bs_branch 在
branch 操作级模式产,故本计算器**只 emit level>=1**(不重复 L0、不动 branch golden)。
逻辑同 bs_branch._first_class 但跨级。孤立、不接 CL 之外、不改上游。

设计见 docs/superpowers/specs/2026-06-18-r5-u1-d3-unify-upgrade.md 部分②。
"""
from __future__ import annotations

from typing import List

from chanlun.core.bs_branch import BuySellPoint
from chanlun.core.recursive_branch import LevelResult


class Bs1BranchCalculator:
    """多级一类买卖点计算器(L1+)。无状态,每次 calculate 全量重算。"""

    def calculate(self, levels: List[LevelResult]) -> List[BuySellPoint]:
        out: List[BuySellPoint] = []
        for lr in levels:
            if getattr(lr, "level", 0) < 1:            # L0 一类由 bs_branch 在 branch 模式产
                continue
            zss = getattr(lr, "zss", None) or []
            for i, dv in enumerate(getattr(lr, "done_divergence", None) or []):
                if (dv is None or not getattr(dv, "is_beichi", False)
                        or getattr(dv, "kind", None) != "qs"          # 一类 = 趋势背驰
                        or getattr(dv, "provisional", False)):        # 只坐实背驰
                    continue
                if i >= len(zss):
                    continue
                c = dv.leave_seg
                if c is None:
                    continue
                z = zss[i]
                if c._type == "down":                                 # 跌势衰竭 → 一类买
                    out.append(BuySellPoint("1buy", z, c, c.end, dv, level=lr.level))
                elif c._type == "up":
                    out.append(BuySellPoint("1sell", z, c, c.end, dv, level=lr.level))
        return out
