"""The formal center model must reject a same-direction source at every gate.

L107:103-112 in D:\\缠论 requires alternating up/down/up child types for the
bounded thirty-minute example; L038:1450-1456 requires shared type endpoints.
"""

from pathlib import Path

import pandas as pd
import pytest

from chanlun.cl_utils.strict_chart_runtime import build_strict_chart_cd
from chanlun.core.strict_structure.center_machine import (
    _new_ongoing_center,
    establish_center,
)
from chanlun.core.strict_structure.models import SourceKind
from tests.core.strict_structure.helpers import TEST_PRICE_BASIS, unit


@pytest.mark.parametrize(
    ("source_kind", "level", "formation_rule"),
    [
        (SourceKind.SEGMENT, 0, "three_core"),
        (SourceKind.TREND_TYPE, 1, None),
    ],
)
def test_direct_center_model_rejects_contiguous_same_direction_members(
    source_kind, level, formation_rule,
):
    members = (
        unit(0, "up", 90, 100, source_kind=source_kind, structural_level=level),
        unit(1, "up", 100, 110, source_kind=source_kind, structural_level=level),
        unit(2, "down", 110, 100, source_kind=source_kind, structural_level=level),
    )
    assert establish_center(members, level, source_kind) is None

    # A direct model construction or dataclass replay must enforce the same
    # alternation gate as the scanning factory, even though prices and market
    # endpoints happen to be connected and the three ranges touch at 100.
    with pytest.raises(ValueError, match="center body directions must alternate"):
        _new_ongoing_center(
            None, None, members, (), None, members, level, source_kind,
            TEST_PRICE_BASIS, 100, 100, formation_rule=formation_rule,
        )


def test_recorded_failed_leave_keeps_a_connected_physical_chain():
    """Filtered center-body roles may skip a valid failed leave and return."""
    frame = pd.read_parquet(
        Path(__file__).parents[2] / "fixtures/segment_clock/SH.600658_5m.parquet"
    )
    frame.attrs.update(structure_price_quantum="0.01",
                       price_basis_revision="recorded-raw")
    runtime = build_strict_chart_cd(
        market="a", code="SH.600658", frequency="5m",
        frame=frame, last_bar_closed=True,
    )
    assert runtime.cd is not None
    centers = tuple(center for level in runtime.cd.get_strict_evidence().structure.levels
                    for center in level.center_result.centers)
    assert any(first.direction == second.direction
               for center in centers
               for first, second in zip(center.body_units, center.body_units[1:]))
    for center in centers:
        roles = (
            *center.body_units, *center.failed_departure_units,
            *center.supersession_bridge_units,
            *((center.pending_leave_unit,) if center.pending_leave_unit else ()),
            *((center.completion_leave_unit,) if center.completion_leave_unit else ()),
            *((center.completion_return_unit,) if center.completion_return_unit else ()),
        )
        ordered = tuple(sorted(
            roles, key=lambda item: (item.market_start, item.market_end, item.unit_id)
        ))
        physical = (*((center.entry_unit,) if center.entry_unit else ()), *ordered)
        for previous, current in zip(physical, physical[1:]):
            assert previous.direction != current.direction
            assert (previous.market_end, previous.end_tick) == (
                current.market_start, current.start_tick
            )
