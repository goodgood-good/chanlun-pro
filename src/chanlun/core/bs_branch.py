"""bs_branch.py — P5a 缠论买卖点（一类 + 三类，单级别 done）。

从 zs_branch 的 ZsBranchResult(done_zss+done_divergence) + 原始 lines，产已完成
中枢的一类(趋势背驰,宪法 §6/第18·24课)+三类(离开中枢回试不破 ZG/ZD,节点① H2
坍缩,第20课)买卖点。孤立、不接 CL、不改上游、不动旧 bs_point_calculator。
设计见 docs/chanlun_core_redesign_5a_买卖点_design.md。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional

from chanlun.core.cl_interface import LINE, FX, ZS
from chanlun.core.zs_branch import ZsBranchResult, DivergenceResult


@dataclass
class BuySellPoint:
    """一个买卖点信号。"""
    bs_type: str                              # "1buy" | "1sell" | "3buy" | "3sell"
    zs: ZS                                    # 关联中枢
    signal_seg: LINE                          # 信号段(一类=背驰离开段 c;三类=回试段)
    anchor_fx: FX                             # 出图锚点(一类=c 末端;三类=回试段末端极值)
    divergence: Optional[DivergenceResult]    # 一类带背驰本体;三类 None


class BsBranchCalculator:
    """买卖点计算器。无状态，每次 calculate 全量重算。"""

    def calculate(self, zs_result: ZsBranchResult,
                  lines: List[LINE]) -> List[BuySellPoint]:
        return self._first_class(zs_result)

    def _first_class(self, zs_result: ZsBranchResult) -> List[BuySellPoint]:
        """一类 = 趋势背驰(done_divergence 里 is_beichi & kind=='qs')。
        离开段向下→1buy(跌势衰竭)、向上→1sell;锚离开段末端极值。"""
        out: List[BuySellPoint] = []
        for i, dv in enumerate(zs_result.done_divergence):
            if dv is None or not dv.is_beichi or dv.kind != "qs":   # 仅趋势背驰
                continue
            c = dv.leave_seg
            z = zs_result.done_zss[i]
            if c._type == "down":
                out.append(BuySellPoint("1buy", z, c, c.end, dv))
            elif c._type == "up":
                out.append(BuySellPoint("1sell", z, c, c.end, dv))
        return out
