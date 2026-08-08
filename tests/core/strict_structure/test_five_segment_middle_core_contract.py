"""物理中枢的五线段窗口与中间三段核心契约。

线段流中的连续五段固定解释为：进入段、核心 A/B/C、离开段。
五段都必须与 A/B/C 的正宽交集重叠，但中枢价格区间和图表时间矩形
只由中间三段决定。走势类型递归仍是三个已完成次级别走势，不套用
物理线段的五段门。
"""

from dataclasses import replace

import pytest

from chanlun.cl_utils.strict_chart import strict_center_to_chart_dict
from chanlun.core.strict_structure.center_machine import establish_center
from chanlun.core.strict_structure.models import CenterState, SourceKind
from tests.core.strict_structure.helpers import unit


@pytest.fixture()
def physical_five():
    return (
        unit(0, "up", 90, 120),
        unit(1, "down", 120, 100),
        unit(2, "up", 100, 115),
        unit(3, "down", 115, 105),
        unit(4, "up", 105, 130),
    )


def test_five_consecutive_segments_establish_middle_three_core(physical_five):
    center = establish_center(physical_five, 0, SourceKind.SEGMENT)

    assert center is not None
    assert center.state is CenterState.ONGOING
    assert center.entry_unit is physical_five[0]
    assert center.core_units == physical_five[1:4]
    assert center.initial_units == physical_five[1:4]
    assert center.initial_exit_unit is physical_five[4]
    assert center.pending_leave_unit is physical_five[4]
    assert center.establishment_units == physical_five
    assert center.body_units == physical_five[1:4]
    assert (center.zd_tick, center.zg_tick) == (105, 115)
    assert center.established_at == physical_five[4].confirmed_at


def test_center_rectangle_uses_only_middle_three_time_span(physical_five):
    center = establish_center(physical_five, 0, SourceKind.SEGMENT)
    assert center is not None

    payload = strict_center_to_chart_dict(center)
    assert payload["points"] == [
        {
            "time": int(physical_five[1].market_start.timestamp()),
            "price_tick": 115,
        },
        {
            "time": int(physical_five[3].market_end.timestamp()),
            "price_tick": 105,
        },
    ]
    assert payload["display_range"] == {
        "start_role": "middle_three_first_start",
        "end_role": "middle_three_last_end",
        "includes_entry": False,
        "includes_leave": False,
        "price_core_source": "middle_three_intersection",
    }
    assert payload["entering_segment"]["unit_id"] == physical_five[0].unit_id
    assert payload["leaving_segment"]["unit_id"] == physical_five[4].unit_id


@pytest.mark.parametrize("bad_role", ("entry", "leave"))
def test_entry_and_leave_must_positively_overlap_middle_core(
    physical_five, bad_role
):
    values = list(physical_five)
    if bad_role == "entry":
        values[0] = replace(
            values[0],
            start_tick=80,
            end_tick=100,
            low_tick=80,
            high_tick=100,
        )
        values[1] = replace(values[1], start_tick=100, high_tick=120)
    else:
        values[3] = replace(values[3], end_tick=115, low_tick=105)
        values[4] = replace(
            values[4],
            start_tick=115,
            end_tick=130,
            low_tick=115,
            high_tick=130,
        )

    assert establish_center(tuple(values), 0, SourceKind.SEGMENT) is None


def test_four_segments_never_establish_physical_center(physical_five):
    assert establish_center(physical_five[:4], 0, SourceKind.SEGMENT) is None


def test_fifth_segment_can_mature_center_as_an_extension(physical_five):
    maturity = replace(
        physical_five[4],
        end_tick=110,
        low_tick=105,
        high_tick=110,
    )
    center = establish_center(
        physical_five[:4] + (maturity,), 0, SourceKind.SEGMENT
    )

    assert center is not None
    assert center.establishment_unit is maturity
    assert center.initial_exit_unit is None
    assert center.pending_leave_unit is None
    assert center.core_units == physical_five[1:4]
    assert center.extension_units == (maturity,)
    assert center.body_units == physical_five[1:4] + (maturity,)
    assert center.establishment_units == physical_five[:4] + (maturity,)
    assert center.has_minimum_physical_roles is True


def test_recursive_trend_center_keeps_three_lower_trend_seed():
    entry = unit(
        -1,
        "down",
        130,
        120,
        source_kind=SourceKind.TREND_TYPE,
        structural_level=1,
    )
    trends = (
        unit(
            0, "up", 120, 140,
            source_kind=SourceKind.TREND_TYPE, structural_level=1,
        ),
        unit(
            1, "down", 140, 125,
            source_kind=SourceKind.TREND_TYPE, structural_level=1,
        ),
        unit(
            2, "up", 125, 135,
            source_kind=SourceKind.TREND_TYPE, structural_level=1,
        ),
    )

    center = establish_center(
        trends,
        1,
        SourceKind.TREND_TYPE,
        entry_unit=entry,
    )
    assert center is not None
    assert center.initial_units == trends
    assert center.core_units == trends
    assert center.initial_exit_unit is None
