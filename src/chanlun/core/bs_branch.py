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
    bs_type: str                              # "1buy"|"1sell"|"3buy"|"3sell"|"2buy"|"2sell"
    zs: ZS                                    # 关联中枢
    signal_seg: LINE                          # 信号段(一类=背驰离开段 c;三类=回试段;二类=次级别一买离开段)
    anchor_fx: FX                             # 出图锚点(一类=c 末端;三类=回试段末端;二类=次级别一买末端)
    divergence: Optional[DivergenceResult]    # 一类/二类带背驰本体;三类 None
    level: Optional[int] = None               # P5b:二类归属级别 L_k;P5a 一三类 None


class BsBranchCalculator:
    """买卖点计算器。无状态，每次 calculate 全量重算。"""

    def calculate(self, zs_result: ZsBranchResult,
                  lines: List[LINE]) -> List[BuySellPoint]:
        return self._first_class(zs_result) + self._third_class(zs_result, lines)

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

    def _third_class(self, zs_result: ZsBranchResult,
                     lines: List[LINE]) -> List[BuySellPoint]:
        """三类 = 离开中枢、第一次回试不破核心 ZG/ZD(第20课)。
        向上离开 & 回试低点 >= ZG → 3buy;向下离开 & 回试高点 <= ZD → 3sell。
        注:P5c bs3_branch 跨级复用此方法做多级三类——改签名/口径需同步它。"""
        out: List[BuySellPoint] = []
        for z in zs_result.done_zss:
            leave = z.end                                          # 离开段(correct_exit 剥出)
            if leave is None:
                continue
            retest = self._next_seg(leave, lines)                  # 紧邻下一段 = 第一次回试
            if retest is None:                                     # 离开到右边缘、无回试 → 不产
                continue
            if leave._type == "up" and retest.end.val >= z.zg:     # 回试低点不破 ZG
                out.append(BuySellPoint("3buy", z, retest, retest.end, None))
            elif leave._type == "down" and retest.end.val <= z.zd:  # 回试高点不破 ZD
                out.append(BuySellPoint("3sell", z, retest, retest.end, None))
        return out

    @staticmethod
    def _next_seg(leave: LINE, lines: List[LINE]) -> Optional[LINE]:
        """离开段在 lines 中的紧邻下一段(按对象身份;leave 是 ZsCalculator 输入段之一)。"""
        for k, ln in enumerate(lines):
            if ln is leave:
                return lines[k + 1] if k + 1 < len(lines) else None
        return None
