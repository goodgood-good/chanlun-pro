"""bs2_branch.py — P5b 缠论二类买卖点（定律一 · 次级别一类递归）。

从 recursive_branch 多级 LevelResult 产二类买卖点：L_k 一买之后、次级别 L_{k-1}
时间在后且不破前低的第一个一买 = L_k 二买（买卖点定律一,原文 3562/3598）。孤立、
不接 CL、不动旧 bs_point_calculator。设计见 docs/chanlun_core_redesign_5b_二类买卖点_design.md。
"""
from __future__ import annotations

from typing import List, Optional, Tuple

from chanlun.core.cl_interface import LINE, ZS
from chanlun.core.recursive_branch import LevelResult
from chanlun.core.zs_branch import DivergenceResult
from chanlun.core.bs_branch import BuySellPoint


class Bs2BranchCalculator:
    """二类买卖点计算器。无状态，每次 calculate 全量重算。"""

    def calculate(self, levels: List[LevelResult]) -> List[BuySellPoint]:
        """各级识别一类点,跨级关联二类:L_k 一买后、L_{k-1} 不破前低的第一个一买。"""
        first_by_level = {lr.level: self._first_points(lr) for lr in levels}
        out: List[BuySellPoint] = []
        for lr in levels:
            k = lr.level
            if k == 0:                                   # L0 无次级别 → 无二买
                continue
            sub = first_by_level.get(k - 1, [])          # 次级别 L_{k-1} 一类点
            for _zs_k, _dv_k, c_k in self._first_points(lr):
                found = self._find_second(c_k, sub)
                if found is not None:
                    zs_sub, dv_sub, c_sub = found
                    bs = "2buy" if c_k._type == "down" else "2sell"
                    stop_kwargs = (
                        {"structural_stop_below": c_k.end.val}
                        if bs == "2buy"
                        else {"structural_stop_above": c_k.end.val}
                    )
                    out.append(BuySellPoint(
                        bs,
                        zs_sub,
                        c_sub,
                        c_sub.end,
                        dv_sub,
                        level=k,
                        **stop_kwargs,
                    ))
        return out

    @staticmethod
    def _first_points(level: LevelResult) -> List[Tuple[ZS, DivergenceResult, LINE]]:
        """该级已固化一类点:(zs, divergence, 离开段 c)。仅趋势背驰 qs、非 provisional。"""
        out: List[Tuple[ZS, DivergenceResult, LINE]] = []
        for i, dv in enumerate(level.done_divergence):
            if dv is not None and dv.is_beichi and dv.kind == "qs" and not dv.provisional:
                out.append((level.zss[i], dv, dv.leave_seg))
        return out

    @staticmethod
    def _find_second(c_k: LINE,
                     sub: List[Tuple[ZS, DivergenceResult, LINE]]
                     ) -> Optional[Tuple[ZS, DivergenceResult, LINE]]:
        """L_k 一类点 c_k 之后,次级别同向、不破前低/高的第一个(时间最早)一类点。"""
        t_k = c_k.end.k.k_index
        val_k = c_k.end.val
        best: Optional[Tuple[ZS, DivergenceResult, LINE]] = None
        best_t: Optional[int] = None
        for zs_sub, dv_sub, c_sub in sub:
            if c_sub._type != c_k._type:                 # 同向
                continue
            t_sub = c_sub.end.k.k_index
            if t_sub <= t_k:                             # 必须在后
                continue
            if c_k._type == "down" and c_sub.end.val < val_k:   # 一买:破前低 → 跳过
                continue
            if c_k._type == "up" and c_sub.end.val > val_k:     # 一卖:破前高 → 跳过
                continue
            if best_t is None or t_sub < best_t:         # 取时间最早
                best, best_t = (zs_sub, dv_sub, c_sub), t_sub
        return best
