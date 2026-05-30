"""beichi_nest.py — P4c 缠论背驰贯通（自底向上 BUILD 嵌套背驰森林）。

把 recursive_branch 多级 LevelResult 里各级已固化背驰段，按「同向 + 严格时间
包含」自底向上挂成嵌套森林（宪法 §7.1 第27课嵌套性 / §7.2 贯通到顶 BUILD）。
是 P6 区间套 READ 的输入。孤立、不接 CL、不改上游。
设计见 docs/chanlun_core_redesign_4c_背驰贯通_design.md。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional, Tuple

from chanlun.core.zs_branch import DivergenceResult


@dataclass
class NestedDivergence:
    """嵌套背驰森林的一个节点：一段已固化背驰 + 被它严格包含+同向的次级别背驰。"""
    level: int                                       # 背驰所在级别 (0=L0)
    zs_index: int                                    # 该级 done_divergence 中的索引(回溯定位)
    divergence: DivergenceResult                     # 背驰本体(P3/P4b 已算,自带 leave_seg 时间区间)
    children: List["NestedDivergence"] = field(default_factory=list)  # 被严格包含+同向的次级别背驰


class BeichiNestCalculator:
    """背驰嵌套森林计算器。无状态，每次 calculate 全量重算。"""

    @staticmethod
    def _span(dv: DivergenceResult) -> Tuple[int, int]:
        """背驰段 = 离开段 c 的 K 线序号区间 [start_k, end_k]。leave_seg 是 LINE
        (XD/ZSLX)，start/end 是 FX、FX.k 是代表 K 线、.k_index 是序号。P3 _divergence_for
        已守卫 leave_seg.start/end 非 None。"""
        c = dv.leave_seg
        return (c.start.k.k_index, c.end.k.k_index)

    def _find_parent(self, lo: "NestedDivergence",
                     hi_nodes: List["NestedDivergence"]) -> Optional["NestedDivergence"]:
        """在 hi_nodes 中找唯一严格时间包含 lo 且同向者；多个候选取最内层(跨度最小)；
        无则 None(断链)。"""
        lo_s, lo_e = self._span(lo.divergence)
        lo_dir = lo.divergence.leave_seg._type
        best, best_w = None, None
        for hi in hi_nodes:
            if hi.divergence.leave_seg._type != lo_dir:          # 同向
                continue
            hi_s, hi_e = self._span(hi.divergence)
            if hi_s <= lo_s and lo_e <= hi_e:                    # 严格时间包含(边界可贴合)
                w = hi_e - hi_s
                if best_w is None or w < best_w:                 # 取最内层
                    best, best_w = hi, w
        return best
