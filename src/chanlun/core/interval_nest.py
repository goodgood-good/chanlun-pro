"""interval_nest.py — 缠论区间套（自顶向下标可操作性）。

消费 beichi_nest 的嵌套背驰森林，自顶向下 DFS 给每个节点标区间套属性
（depth/is_innermost/is_nested/operable）。可操作 ⟺ 嵌套链最内层 + 被逐级套住。
深度门限的可操作性策略交策略层。孤立、不接 CL、不改上游、不动旧 recursive_calculator。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import List

from chanlun.core.beichi_nest import NestedDivergence


@dataclass
class NestRead:
    """一个森林节点(=某级一类点/背驰段)的区间套 READ 标注。"""
    node: NestedDivergence     # 森林节点(其 .divergence 可回溯对应买卖点)
    depth: int                 # 嵌套链层级(顶层根=1,每下一层 +1)
    is_innermost: bool         # 无 children = 最内层(最低级别背驰)
    is_nested: bool            # depth>1 = 被更高级别套住(有祖先)
    operable: bool             # is_innermost & is_nested = 结构可操作信号


class IntervalNestCalculator:
    """区间套计算器。无状态，每次 calculate 全量重算。"""

    def calculate(self, forest: List[NestedDivergence]) -> List[NestRead]:
        """森林每个节点标区间套属性。顶层根 depth=1,逐级向内 +1。"""
        out: List[NestRead] = []

        def _dfs(node: NestedDivergence, depth: int) -> None:
            innermost = not node.children                   # 无子 = 最内层
            nested = depth > 1                              # 有祖先 = 被套住
            out.append(NestRead(
                node=node, depth=depth, is_innermost=innermost,
                is_nested=nested, operable=innermost and nested,
            ))
            for ch in node.children:
                _dfs(ch, depth + 1)

        for root in forest:
            _dfs(root, 1)
        return out
