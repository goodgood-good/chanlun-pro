"""tests/core/test_interval_nest.py — P6 区间套 TDD。

受控 NestedDivergence 森林（divergence 占位 None：P6 calculate 只读 children 结构、
不读 divergence 内容）。
"""
from __future__ import annotations

from chanlun.core.beichi_nest import NestedDivergence
from chanlun.core.interval_nest import IntervalNestCalculator


def _node(level, children=None) -> NestedDivergence:
    return NestedDivergence(level=level, zs_index=0, divergence=None,
                            children=children if children is not None else [])


def _by_node(reads):
    return {id(r.node): r for r in reads}


def test_calculate_empty_returns_empty():
    assert IntervalNestCalculator().calculate([]) == []


def test_three_level_chain():
    # L2 → L1 → L0：L0 最内层+被套=可操作；L1/L2 非最内层=不可操作
    l0 = _node(0)
    l1 = _node(1, [l0])
    l2 = _node(2, [l1])
    reads = IntervalNestCalculator().calculate([l2])
    assert len(reads) == 3
    by = _by_node(reads)
    assert (by[id(l0)].depth, by[id(l0)].is_innermost, by[id(l0)].is_nested, by[id(l0)].operable) == (3, True, True, True)
    assert (by[id(l1)].depth, by[id(l1)].is_innermost, by[id(l1)].is_nested, by[id(l1)].operable) == (2, False, True, False)
    assert (by[id(l2)].depth, by[id(l2)].is_innermost, by[id(l2)].is_nested, by[id(l2)].operable) == (1, False, False, False)


def test_isolated_single_node_not_operable():
    # 孤立单节点:最内层但没被套 → 不可操作(§7.1 孤立背驰无意义)
    n = _node(0)
    reads = IntervalNestCalculator().calculate([n])
    assert len(reads) == 1
    r = reads[0]
    assert (r.depth, r.is_innermost, r.is_nested, r.operable) == (1, True, False, False)


def test_same_parent_multiple_children():
    # L1 含 2 个 L0 叶 → 两 L0 都可操作;L1 非最内层
    a, b = _node(0), _node(0)
    l1 = _node(1, [a, b])
    reads = IntervalNestCalculator().calculate([l1])
    by = _by_node(reads)
    assert by[id(a)].operable and by[id(b)].operable
    assert by[id(a)].depth == 2 and by[id(b)].depth == 2
    assert not by[id(l1)].is_innermost and by[id(l1)].depth == 1 and not by[id(l1)].operable


def test_multiple_trees_independent():
    # 一棵 3 级链 + 一个孤立根 → 标注互不干扰
    l0 = _node(0)
    l1 = _node(1, [l0])
    l2 = _node(2, [l1])
    iso = _node(0)
    reads = IntervalNestCalculator().calculate([l2, iso])
    assert len(reads) == 4
    by = _by_node(reads)
    assert by[id(l0)].operable                 # 链最内层可操作
    assert not by[id(iso)].operable            # 孤立不可操作
    assert by[id(iso)].depth == 1 and by[id(iso)].is_innermost
