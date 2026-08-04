"""盘整走势类型在高一级中枢递归中的交替语义。

线段恒有方向，相邻线段必然一上一下，所以线段层保持严格交替。走势类型层不同：
盘整没有方向，把它按净位移强行记成 up/down 再要求交替，会把「上涨—盘整—上涨」
这种中枢上移的标准形态判成非法，递归因此永远升不上去。

这些用例钉死两件事：
1. 线段层（``oscillatory_ids`` 为空）行为与原严格交替完全等价；
2. 走势类型层允许盘整夹在同向趋势之间，但两段趋势直接相邻时仍须反向；
3. 直接相邻的同向趋势必须先用结合律合成一个单元再递归。
"""

from __future__ import annotations

import pytest

from chanlun.core.strict_structure.center_machine import (
    validate_unit_sequence,
)
from chanlun.core.strict_structure.models import SourceKind
from chanlun.core.strict_structure.same_level_decomposition import (
    combine_same_level_trends,
)

from tests.core.strict_structure.helpers import unit


def trend_unit(index: int, direction: str, start_tick: int, end_tick: int):
    return unit(
        index,
        direction,
        start_tick,
        end_tick,
        source_kind=SourceKind.TREND_TYPE,
        structural_level=1,
    )


def connected(*specs):
    """Build a contiguous unit chain: each unit starts where the last ended."""

    units = []
    tick = specs[0][0]
    for index, (start, end) in enumerate(specs):
        assert start == tick, "specs must connect"
        units.append(
            trend_unit(index, "up" if end >= start else "down", start, end)
        )
        tick = end
    return tuple(units)


def test_segment_level_keeps_strict_alternation() -> None:
    """线段层不受本次改动影响：同向相邻仍然非法。"""

    values = (
        unit(0, "up", 100, 120),
        unit(1, "up", 120, 140),
    )
    with pytest.raises(ValueError, match="unit directions must alternate"):
        validate_unit_sequence(values, 0, SourceKind.SEGMENT)


def test_adjacent_same_direction_trends_require_combination() -> None:
    """同向直接相邻不是两个合法递归单元，须先应用结合律。"""

    values = connected((100, 120), (120, 140))
    with pytest.raises(ValueError, match="unit directions must alternate"):
        validate_unit_sequence(values, 1, SourceKind.TREND_TYPE, frozenset())

    decomposed = combine_same_level_trends(values, frozenset())
    assert len(decomposed.units) == 1
    assert decomposed.units[0].child_ids == tuple(item.unit_id for item in values)
    assert decomposed.units[0].start_tick == 100
    assert decomposed.units[0].end_tick == 140
    assert len(decomposed.combinations) == 1


def test_consolidation_between_same_direction_trends_is_legal() -> None:
    """「上涨—盘整—上涨」是中枢上移的标准形态，必须合法。"""

    values = connected((100, 120), (120, 130), (130, 150))
    oscillatory = frozenset({values[1].unit_id})

    with pytest.raises(ValueError, match="unit directions must alternate"):
        validate_unit_sequence(values, 1, SourceKind.TREND_TYPE, frozenset())
    validate_unit_sequence(values, 1, SourceKind.TREND_TYPE, oscillatory)

    decomposed = combine_same_level_trends(values, oscillatory)
    assert decomposed.units == values
    assert decomposed.oscillatory_ids == oscillatory


def test_consolidation_itself_never_conflicts_with_either_neighbour() -> None:
    """盘整只作无方向连接件，与左右邻居都不构成方向冲突。"""

    values = connected((100, 120), (120, 130), (130, 125), (125, 140))
    oscillatory = frozenset({values[1].unit_id, values[2].unit_id})
    validate_unit_sequence(values, 1, SourceKind.TREND_TYPE, oscillatory)


def test_declaring_every_unit_oscillatory_still_enforces_connectivity() -> None:
    """放开的只有方向交替；首尾相接与区间不重叠仍然强制。"""

    broken = (
        trend_unit(0, "up", 100, 120),
        trend_unit(1, "up", 130, 150),  # 130 != 120，断链
    )
    oscillatory = frozenset(item.unit_id for item in broken)
    with pytest.raises(ValueError, match="adjacent unit prices must connect"):
        validate_unit_sequence(broken, 1, SourceKind.TREND_TYPE, oscillatory)
