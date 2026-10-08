"""A fixed same-grade type chain must share its market/price endpoint."""

from dataclasses import replace
from datetime import timedelta

import pytest

from chanlun.core.strict_structure.models import SourceKind
from chanlun.core.strict_structure.center_machine import calculate_centers
from chanlun.core.strict_structure.same_level_decomposition import (
    combine_same_level_trends,
    l038_six_member_split_payload,
    observe_l038_six_member_split,
)
from tests.core.strict_structure.helpers import unit


def test_price_connected_completed_types_cannot_leave_unowned_market_time():
    # L038:61-94 fixes one grade; author reply L038:1450-1456 says the next
    # type must start at the prior type's end point.
    first = unit(
        0, "up", 100, 120,
        source_kind=SourceKind.TREND_TYPE, structural_level=1,
    )
    second = unit(
        1, "down", 120, 105,
        source_kind=SourceKind.TREND_TYPE, structural_level=1,
    )
    exact = combine_same_level_trends((first, second), frozenset())
    assert exact.units[0].market_end == exact.units[1].market_start

    gapped = replace(
        second, market_start=first.market_end + timedelta(seconds=1)
    )
    assert first.end_tick == gapped.start_tick
    with pytest.raises(ValueError, match="same-level source market endpoints must connect"):
        combine_same_level_trends((first, gapped), frozenset())


def test_l038_six_overlapping_lower_types_have_a_distinct_no_extension_split():
    # Author L038:81-94 permits two connected consolidations when an overlap
    # reaches six lower types.  Center lifecycle may still retain one center;
    # these are different declared decomposition policies.
    prices = (100, 120, 110, 118, 112, 117, 113)
    values = tuple(
        replace(
            unit(
                index, "up" if end > start else "down", start, end,
                source_kind=SourceKind.TREND_TYPE, structural_level=1,
            ),
            child_ids=(f"completed-child-{index}",),
        )
        for index, (start, end) in enumerate(zip(prices, prices[1:]))
    )
    lifecycle = calculate_centers(values, 1, SourceKind.TREND_TYPE)
    assert len(lifecycle.centers) == 1
    assert len(lifecycle.centers[0].body_units) == 6

    split = observe_l038_six_member_split(
        values, as_of=values[-1].available_at,
    )
    assert split is not None
    assert split.left_member_ids == tuple(item.unit_id for item in values[:3])
    assert split.right_member_ids == tuple(item.unit_id for item in values[3:])
    assert split.left_core_ticks == (110, 118)
    assert split.right_core_ticks == (113, 117)
    assert split.shared_six_core_ticks == (113, 117)
    assert split.boundary_market_at == values[2].market_end == values[3].market_start
    assert split.boundary_tick == values[2].end_tick == values[3].start_tick
    payload = l038_six_member_split_payload(split)
    assert payload["known_at"] == int(values[-1].available_at.timestamp())
    assert not payload["canonical_center_lifecycle_replaced"]
    assert not payload["whole_path_partition_certified"]
    assert not payload["tradable"]

    assert observe_l038_six_member_split(
        values, as_of=values[-1].available_at - timedelta(seconds=1),
    ) is None
    disjoint_prices = (100, 120, 110, 118, 90, 100, 80)
    disjoint = tuple(
        replace(
            unit(
                index, "up" if end > start else "down", start, end,
                source_kind=SourceKind.TREND_TYPE, structural_level=1,
            ),
            child_ids=(f"completed-child-{index}",),
        )
        for index, (start, end) in enumerate(
            zip(disjoint_prices, disjoint_prices[1:])
        )
    )
    assert observe_l038_six_member_split(
        disjoint, as_of=disjoint[-1].available_at,
    ) is None
