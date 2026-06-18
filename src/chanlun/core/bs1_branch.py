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


class QuasiFirstClassCalculator:
    """B3 · 类一买/类一卖(原文 L027 大级别盘整背驰=历史性底部=类买点)。

    done_divergence 含 pz(盘整背驰)条目(zs_branch:402 kind="qs"if趋势else"pz");pz+is_beichi
    的离开段 → 类一类(离开向下=类1buy/向上=类1sell)。**类=quasi**(缠明言操作意义弱、需次级别
    确认),故:① bs_type='类1buy'/'类1sell' 区别于 strict 一类(qs,Bs1);② **不入默认
    get_branch_bspoints/回测 BUYS 白名单**(engine.buy_class 取首字符 int,'类'会崩),作单独
    marker(cd.get_branch_quasi_first)暴露,供图表/分析/可选用,不改默认信号/golden/回测。全级别。
    """

    def calculate(self, levels: List[LevelResult]) -> List[BuySellPoint]:
        out: List[BuySellPoint] = []
        for lr in levels:
            zss = getattr(lr, "zss", None) or []
            for i, dv in enumerate(getattr(lr, "done_divergence", None) or []):
                if (dv is None or not getattr(dv, "is_beichi", False)
                        or getattr(dv, "kind", None) != "pz"           # 类一类 = 盘整背驰
                        or getattr(dv, "provisional", False)):
                    continue
                if i >= len(zss):
                    continue
                c = dv.leave_seg
                if c is None:
                    continue
                z = zss[i]
                if c._type == "down":
                    out.append(BuySellPoint("类1buy", z, c, c.end, dv, level=lr.level))
                elif c._type == "up":
                    out.append(BuySellPoint("类1sell", z, c, c.end, dv, level=lr.level))
        return out
