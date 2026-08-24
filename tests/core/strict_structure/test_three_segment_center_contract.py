"""原文三段中枢与五段三类点的因果边界。

前三段已完成的同级单元冻结中枢；第四段只可能成为离开观察；只有第五段
在中枢外完成首次回返时，才确认第三类买卖点。第五段不是中枢成立条件。
"""

from chanlun.core.strict_structure.center_machine import calculate_centers
from chanlun.core.strict_structure.models import CenterState, SourceKind, TrendState
from chanlun.core.strict_structure.recursive_engine import StrictRecursiveEngine
from tests.core.strict_structure.helpers import TEST_PRICE_BASIS, unit


def _three_core_up_exit():
    return (
        unit(0, "down", 120, 100),
        unit(1, "up", 100, 115),
        unit(2, "down", 115, 105),
        unit(3, "up", 105, 130),
        unit(4, "down", 130, 120),
    )


def test_three_locked_segments_establish_center_without_future_maturity() -> None:
    values = _three_core_up_exit()

    result = calculate_centers(values[:3], 0, SourceKind.SEGMENT)

    assert len(result.centers) == 1
    center = result.centers[0]
    assert center.state is CenterState.ONGOING
    assert center.entry_unit is None
    assert center.core_units == values[:3]
    assert center.initial_units == values[:3]
    assert center.pending_leave_unit is None
    assert center.established_at == values[2].confirmed_at
    assert (center.zd_tick, center.zg_tick) == (105, 115)


def test_fourth_segment_is_departure_observation_not_center_maturity() -> None:
    values = _three_core_up_exit()

    result = calculate_centers(values[:4], 0, SourceKind.SEGMENT)

    assert len(result.centers) == 1
    center = result.centers[0]
    assert center.state is CenterState.ONGOING
    assert center.core_units == values[:3]
    assert center.pending_leave_unit is values[3]
    assert center.completion_return_unit is None


def test_fifth_segment_confirms_third_class_completion() -> None:
    values = _three_core_up_exit()

    result = calculate_centers(values, 0, SourceKind.SEGMENT)

    assert len(result.centers) == 1
    center = result.centers[0]
    assert center.state is CenterState.COMPLETED
    assert center.core_units == values[:3]
    assert center.completion_leave_unit is values[3]
    assert center.completion_return_unit is values[4]


def test_three_segment_center_is_classified_as_forming_trend_type() -> None:
    values = _three_core_up_exit()

    structure = StrictRecursiveEngine(max_levels=2).calculate(
        values[:3],
        price_basis_revision=TEST_PRICE_BASIS,
    )

    assert len(structure.levels) == 1
    level = structure.levels[0]
    assert len(level.center_result.centers) == 1
    assert len(level.trend_types) == 1
    assert level.trend_types[0].state is TrendState.FORMING
    assert level.trend_types[0].centers == level.center_result.centers
