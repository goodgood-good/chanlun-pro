"""背驰段嵌套森林构建。

把 recursive_branch 多级 LevelResult 里各级已固化背驰段，按「同向 + 严格时间
包含」自底向上组织成嵌套森林，供上层区间套分析使用。孤立、不接 CL、不改上游。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional, Set, Tuple

from chanlun.core.recursive_branch import LevelResult
from chanlun.core.zs_branch import DivergenceResult


@dataclass
class NestedDivergence:
    """嵌套背驰森林的一个节点：一段已固化背驰 + 被它严格包含+同向的次级别背驰。"""
    level: int                                       # 背驰所在级别 (0=L0)
    zs_index: int                                    # 该级 done_divergence 中的索引(回溯定位)
    divergence: DivergenceResult                     # 背驰本体，自带 leave_seg 时间区间
    children: List["NestedDivergence"] = field(default_factory=list)  # 被严格包含+同向的次级别背驰


class BeichiNestCalculator:
    """背驰嵌套森林计算器。无状态，每次 calculate 全量重算。"""

    @staticmethod
    def _span(dv: DivergenceResult) -> Tuple[int, int]:
        """背驰段 = 离开段 c 的 K 线序号区间 [start_k, end_k]。leave_seg 是 LINE
        (XD/ZSLX)，start/end 是 FX、FX.k 是代表 K 线、.k_index 是序号。上游已守卫
        leave_seg.start/end 非 None（FX.k 由 FX 构造保证非 None）。"""
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

    def calculate(self, levels: List[LevelResult]) -> List[NestedDivergence]:
        """各级 done 背驰段自底向上 BUILD 成嵌套森林。返回顶层森林(所有未被更高
        级别包含的背驰节点;最高级 + 断链低级各成树根)。"""
        if not levels:
            return []

        # 1. 每级抽出「已固化 + is_beichi」的背驰 → 叶节点
        per_level: List[List[NestedDivergence]] = []
        for lr in levels:
            nodes: List[NestedDivergence] = []
            for zi, dv in enumerate(lr.done_divergence):
                if dv is not None and dv.is_beichi and not dv.provisional:   # 仅已坐实背驰
                    nodes.append(NestedDivergence(level=lr.level, zs_index=zi, divergence=dv))
            per_level.append(nodes)

        # 2. 自底向上:相邻级别 (k → k+1),把 L_k 节点挂到 严格包含+同向 的 L_{k+1} 节点。
        #    同一父下多个同向兄弟只保留「末端」一个(leave_seg 结束 k_index 最大=最接近
        #    离开段转折点=区间套收敛确认点),其余不挂→落森林根(depth=1→非 operable→
        #    ratio 走 0.75 分支)。忠实缠论区间套「收敛到单一转折点」:中途同向已坐实背驰
        #    非套内确认点(终检 R12-2)。
        attached: Set[int] = set()
        for k in range(len(per_level) - 1):
            best_child: dict[int, NestedDivergence] = {}   # parent id → 末端子
            parents: dict[int, NestedDivergence] = {}
            for lo in per_level[k]:
                parent = self._find_parent(lo, per_level[k + 1])
                if parent is None:
                    continue
                parents[id(parent)] = parent
                cur = best_child.get(id(parent))
                if cur is None or self._span(lo.divergence)[1] > self._span(cur.divergence)[1]:
                    best_child[id(parent)] = lo
            for pid, child in best_child.items():
                parents[pid].children.append(child)
                attached.add(id(child))

        # 3. 顶层森林 = 所有未被挂载的节点(最高级别 + 断链的低级别各成树根)
        return [n for nodes in per_level for n in nodes if id(n) not in attached]
