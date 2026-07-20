"""多级二类买卖点（次级别一类递归关联）。

从 recursive_branch 多级 LevelResult 产二类买卖点：L_k 一买之后、次级别 L_{k-1}
时间在后且不破前低的第一个一买 = L_k 二买。孤立、不接 CL、不动旧
bs_point_calculator。
"""
from __future__ import annotations

from typing import List, Optional, Tuple

from chanlun.core.types import LINE, ZS
from chanlun.core.recursive_branch import LevelResult
from chanlun.core.zs_branch import DivergenceResult
from chanlun.core.bs_branch import BuySellPoint


class Bs2BranchCalculator:
    """二类买卖点计算器。无状态，每次 calculate 全量重算。"""

    def calculate(self, levels: List[LevelResult],
                  ld_provider=None, frequency=None) -> List[BuySellPoint]:
        """各级识别一类点,跨级关联二类:L_k 一买后、L_{k-1} 第一个一买。

        三档:① 不破前低/高=强/一般档(止损用 L_k 极值);② 最弱档=次级别一买跌破
        L_k 一买 BUT 对其构成盘整背驰(is_beichi)→ 最弱二买(止损下移到次级别新低)。
        最弱档需 ``ld_provider``;缺省只产强/一般档。
        """
        first_by_level = {lr.level: self._first_points(lr) for lr in levels}
        out: List[BuySellPoint] = []
        for lr in levels:
            k = lr.level
            if k == 0:                                   # L0 无次级别 → 无二买
                continue
            sub = first_by_level.get(k - 1, [])          # 次级别 L_{k-1} 一类点
            for _zs_k, _dv_k, c_k in self._first_points(lr):
                found = self._find_second(c_k, sub, ld_provider, frequency)
                if found is not None:
                    zs_sub, dv_sub, c_sub = found
                    bs = "2buy" if c_k._type == "down" else "2sell"
                    # 最弱档(破前低/高):止损用次级别一买自身极值;强/一般:用 L_k 极值
                    breaks = ((c_k._type == "down" and c_sub.end.val < c_k.end.val)
                              or (c_k._type == "up" and c_sub.end.val > c_k.end.val))
                    stop_val = c_sub.end.val if breaks else c_k.end.val
                    stop_kwargs = ({"structural_stop_below": stop_val} if bs == "2buy"
                                   else {"structural_stop_above": stop_val})
                    out.append(BuySellPoint(
                        bs, zs_sub, c_sub, c_sub.end, dv_sub, level=k, **stop_kwargs,
                        definition_variant=("weak_divergence" if breaks else "strict"),
                    ))
        return out

    @staticmethod
    def _first_points(level: LevelResult) -> List[Tuple[ZS, DivergenceResult, LINE]]:
        """该级已固化一类点:(zs, divergence, 离开段 c)。仅趋势背驰、非临时态。"""
        out: List[Tuple[ZS, DivergenceResult, LINE]] = []
        for i, dv in enumerate(level.done_divergence):
            if dv is not None and dv.is_beichi and dv.kind == "qs" and not dv.provisional:
                out.append((level.zss[i], dv, dv.leave_seg))
        return out

    @staticmethod
    def _find_second(c_k: LINE,
                     sub: List[Tuple[ZS, DivergenceResult, LINE]],
                     ld_provider=None, frequency=None
                     ) -> Optional[Tuple[ZS, DivergenceResult, LINE]]:
        """L_k 一类点 c_k 之后,次级别同向、在后的第一个(时间最早)一类点。
        不破前低/高=强/一般档直接取;破前低/高仅当对 c_k 构成盘整背驰(is_beichi)才取
        =最弱档,否则跳过。最弱档需 ld_provider。"""
        from chanlun.core.beichi_calculator import is_beichi

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
            breaks = ((c_k._type == "down" and c_sub.end.val < val_k)
                      or (c_k._type == "up" and c_sub.end.val > val_k))   # 破前低/高
            if breaks and not (ld_provider is not None
                               and is_beichi(c_k, c_sub, ld_provider, frequency)):
                continue                                 # 破前低/高 且 非盘整背驰 → 跳(非最弱二买)
            if best_t is None or t_sub < best_t:         # 取时间最早
                best, best_t = (zs_sub, dv_sub, c_sub), t_sub
        return best
