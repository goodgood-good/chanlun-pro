"""Selected three-core scan against L054 and recorded causal center boundaries."""

from dataclasses import replace
from decimal import Decimal

import pandas as pd
import pytest

from chanlun.cl_utils.strict_chart import strict_center_preview_to_chart_dict
from chanlun.cl_utils.strict_chart_runtime import build_strict_chart_cd
from chanlun.core.strict_structure.center_machine import (
    calculate_three_core_segment_centers,
)
from chanlun.core.strict_structure.general_decomposition import (
    build_declared_three_core_signal_view,
    declared_general_signal_view_payload,
    qualify_locked_trend_type,
)
from chanlun.core.strict_structure.models import CenterPreviewState, CenterState, TrendState
from chanlun.core.strict_structure.trend_assembler import assemble_trend_types
from tests.core.strict_structure.test_general_decomposition import _l054_segments


def test_l054_selected_scan_forms_at_d2_and_keeps_g3_completion():
    # D:\缠论\chanlun_lesson_corpus\L054_一个具体走势的分析...
    # author lines 73-88, chart line 97.
    values = _l054_segments()
    at_g1 = calculate_three_core_segment_centers(values[:2], 0)
    at_d2 = calculate_three_core_segment_centers(values[:3], 0)
    at_g3 = calculate_three_core_segment_centers(values[:6], 0)

    assert at_g1.centers == ()
    assert len(at_d2.centers) == 1
    center = at_d2.centers[0]
    assert center.initial_units == values[:3]
    assert (center.zd_tick, center.zg_tick) == (80, 90)
    assert center.established_at == values[2].confirmed_at
    three_segment_type, = assemble_trend_types(
        at_d2.centers, values[:3], 0,
    ).current_trends
    assert three_segment_type.kind.value == "consolidation"
    assert three_segment_type.state is TrendState.FORMING
    assert len(three_segment_type.centers) == 1
    assert three_segment_type.constituent_units == values[:3]
    completed = at_g3.centers[0]
    assert completed.center_id == center.center_id
    assert completed.state is CenterState.COMPLETED
    assert completed.completion_leave_unit == values[4]
    assert completed.completion_return_unit == values[5]


def test_l054_completed_return_may_start_later_center_with_connector():
    values = _l054_segments()
    result = calculate_three_core_segment_centers(values[:10], 0)
    first, second = result.centers

    assert first.state is CenterState.COMPLETED
    assert second.state is CenterState.ONGOING
    assert first.completion_leave_unit == values[4]
    assert first.completion_return_unit == second.initial_units[0] == values[5]
    assert not {unit.unit_id for unit in first.body_units} & {
        unit.unit_id for unit in second.body_units
    }


def test_l054_unlocked_third_segment_preview_keeps_future_center_identity():
    values = _l054_segments()
    live = (*values[:2], replace(values[2], locked=False, confirmed_at=None))
    selected = calculate_three_core_segment_centers(live, 0)
    locked = calculate_three_core_segment_centers(values[:3], 0)

    assert selected.centers == ()
    assert len(selected.previews) == 1
    preview = selected.previews[0]
    assert preview.state is CenterPreviewState.FORMING
    assert preview.formation_rule == "three_core"
    assert preview.formal_center_id == locked.centers[0].center_id
    payload = strict_center_preview_to_chart_dict(
        preview, {unit.unit_id: unit for unit in live}
    )
    assert payload["formation_rule"] == "three_core"
    assert not payload["tradable"]


def test_l054_unlocked_outside_return_projects_same_center_completion():
    values = _l054_segments()
    live = (*values[:5], replace(values[5], locked=False, confirmed_at=None))
    selected = calculate_three_core_segment_centers(live, 0)
    completed = calculate_three_core_segment_centers(values[:6], 0)

    assert len(selected.centers) == len(selected.previews) == 1
    assert selected.centers[0].state is CenterState.ONGOING
    preview = selected.previews[0]
    assert preview.state is CenterPreviewState.COMPLETED
    assert preview.formal_center_id == selected.centers[0].center_id
    assert preview.completion_return_unit_id == values[5].unit_id
    assert completed.centers[0].state is CenterState.COMPLETED
    assert completed.centers[0].center_id == preview.formal_center_id


def test_recorded_selected_scan_preserves_disjoint_successor_bridge():
    frame = pd.read_parquet("tests/fixtures/SH.600519_5m.parquet").head(10000)
    frame.attrs.update(
        structure_price_quantum="0.01", price_basis_revision="recorded-raw"
    )
    runtime = build_strict_chart_cd(
        market="a", code="SH.600519", frequency="5m", frame=frame
    )
    units = runtime.cd.get_strict_evidence().structure.levels[0].units
    scan = calculate_three_core_segment_centers(units, 0)

    assert len(scan.centers) >= 2
    assert scan.centers[0].initial_units == units[:3]
    assert any(center.state is CenterState.SUPERSEDED for center in scan.centers)
    for previous, successor in zip(scan.centers, scan.centers[1:]):
        assert not {unit.unit_id for unit in previous.body_units} & {
            unit.unit_id for unit in successor.body_units
        }
        if previous.state is CenterState.SUPERSEDED:
            assert previous.supersession_bridge_units
            assert successor.entry_unit is None
            assert previous.supersession_bridge_units[-1].end_tick == (
                successor.body_units[0].start_tick
            )
            assert previous.supersession_bridge_units[-1].market_end == (
                successor.body_units[0].market_start
            )

    declared = build_declared_three_core_signal_view(
        units, view_id="recorded-five-minute-selected",
        view_start_unit_id=units[0].unit_id,
        as_of=runtime.cd._strict_as_of(), price_quantum=Decimal("0.01"),
    )
    assert declared.trend_identity_scope == "recomposable_general_view"
    assert declared.structure.levels[0].trend_types
    assert all(
        not trend.locked for trend in declared.structure.levels[0].trend_types
    )
    with pytest.raises(ValueError, match="locked and causally available"):
        qualify_locked_trend_type(
            declared.structure.levels[0].trend_types[0],
            view_id="must-stay-provisional", as_of=runtime.cd._strict_as_of(),
        )

    locked = tuple(unit for unit in units if unit.locked)
    previous_center_ids = ()
    previous_locked_trend_ids = ()
    for size in range(1, len(locked) + 1):
        prefix = locked[:size]
        current = calculate_three_core_segment_centers(prefix, 0)
        center_ids = tuple(center.center_id for center in current.centers)
        assert center_ids[:len(previous_center_ids)] == previous_center_ids
        assembly = assemble_trend_types(current.centers, prefix, 0)
        locked_trend_ids = tuple(
            trend.trend_id for trend in assembly.current_trends if trend.locked
        )
        assert locked_trend_ids[:len(previous_locked_trend_ids)] == (
            previous_locked_trend_ids
        )
        previous_center_ids = center_ids
        previous_locked_trend_ids = locked_trend_ids


def test_recorded_open_history_center_keeps_type_partition_pending():
    frame = pd.read_parquet("tests/fixtures/SZ.002299_1m.parquet").head(3000)
    frame.attrs.update(
        structure_price_quantum="0.01", price_basis_revision="recorded-raw"
    )
    runtime = build_strict_chart_cd(
        market="a", code="SZ.002299", frequency="1m", frame=frame
    )
    units = runtime.cd.get_strict_evidence().structure.levels[0].units
    view = build_declared_three_core_signal_view(
        units, view_id="recorded-one-minute-selected",
        view_start_unit_id=units[0].unit_id,
        as_of=runtime.cd._strict_as_of(), price_quantum=Decimal("0.01"),
    )

    assert view.structure.levels[0].center_result.centers
    assert view.structure.levels[0].trend_types == ()
    assert view.trend_partition_status == "pending_source_ownership"
    payload = declared_general_signal_view_payload(view)
    assert payload["trend_identity_scope"] == "recomposable_general_view"
    assert payload["trend_partition_reason"] == (
        "trend must contain every center body and bridge unit"
    )
    assert not payload["full_path_partition_certified"]
    assert not payload["tradable"]
