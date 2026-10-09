"""Causal native operational views; selection is an engineering policy."""
from dataclasses import replace
from pathlib import Path

import pandas as pd
import pytest

from chanlun.cl_utils.strict_chart_runtime import build_strict_chart_cd
from chanlun.core.strict_structure.center_machine import calculate_three_core_segment_centers
from chanlun.core.strict_structure.models import CenterState
from chanlun.core.strict_structure.native_center_view import calculate_native_center_views
from tests.core.strict_structure.helpers import unit


def nested_sequence(*, mirror=False):
    prices = [220, 100, 200, 90, 180, 130, 170, 140, 190, 180, 240, 220]
    values = tuple(unit(i, "up" if b > a else "down", a, b)
                   for i, (a, b) in enumerate(zip(prices, prices[1:])))
    if mirror:
        values = tuple(replace(u, direction="down" if u.direction == "up" else "up",
                               start_tick=400-u.start_tick, end_tick=400-u.end_tick,
                               low_tick=400-u.high_tick, high_tick=400-u.low_tick)
                       for u in values)
    return values


@pytest.mark.parametrize("mirror", [False, True])
def test_inner_view_requires_independent_locked_return_and_preserves_original_identity(mirror):
    values = nested_sequence(mirror=mirror)
    before = calculate_native_center_views(values[:8])
    original = before.centers[0]
    assert before.view_selections == ()
    assert original.initial_units == values[:3]
    live = (*values[:8], replace(values[8], locked=False, confirmed_at=None))
    assert calculate_native_center_views(live).view_selections == ()

    after = calculate_native_center_views(values[:9])
    selected, = after.centers
    record, = after.view_selections
    assert selected.initial_units == values[4:7]
    assert selected.state is CenterState.COMPLETED
    assert selected.completion_leave_unit == values[7]
    assert selected.completion_return_unit == values[8]
    assert selected.center_id != original.center_id
    assert record["original_center_id"] == original.center_id
    assert record["original_core_unit_ids"] == tuple(u.unit_id for u in values[:3])
    assert record["connection_unit_ids"] == (values[3].unit_id,)
    assert record["available_at"] == int(values[8].available_at.timestamp())
    assert selected.view_available_at == values[8].available_at
    assert selected.view_id == record["view_id"]
    assert all(e.available_at >= selected.view_available_at for e in after.events)
    assert calculate_three_core_segment_centers(values[:9], 0).centers[0].state is CenterState.ONGOING


@pytest.mark.parametrize("mirror", [False, True])
def test_later_original_completion_cannot_erase_prior_view_selection(mirror):
    values = nested_sequence(mirror=mirror)
    before = calculate_native_center_views(values[:9])
    baseline = calculate_three_core_segment_centers(values, 0)
    assert baseline.centers[0].state is CenterState.COMPLETED
    assert baseline.centers[0].completed_at > before.centers[0].completed_at
    later = calculate_native_center_views(values)
    assert later.view_selections == before.view_selections
    assert later.centers[0] == before.centers[0]


def test_equal_core_boundary_is_not_a_strictly_nested_view():
    # Candidate lower boundary equals the original lower boundary.
    prices = [220, 100, 200, 90, 180, 100, 170, 100, 190, 180]
    values = tuple(unit(i, "up" if b > a else "down", a, b)
                   for i, (a, b) in enumerate(zip(prices, prices[1:])))
    result = calculate_native_center_views(values)
    assert result.view_selections == ()
    assert result == calculate_three_core_segment_centers(values, 0)


def test_inner_view_does_not_ignore_a_return_internal_extreme():
    values = list(nested_sequence())
    values[8] = replace(values[8], low_tick=160)
    result = calculate_native_center_views(tuple(values[:9]))
    assert result.view_selections == ()
    assert result.centers[0].initial_units == tuple(values[:3])


def test_unchanged_empty_and_short_input_preserve_base_scan():
    values = nested_sequence()
    for size in range(4):
        assert calculate_native_center_views(values[:size]) == calculate_three_core_segment_centers(values[:size], 0)


@pytest.fixture(scope="module")
def recorded_frame():
    return pd.read_parquet(Path(__file__).parents[2] / "fixtures/SH.513100_5m_center_20261009.parquet")


def _recorded_units(frame):
    runtime = build_strict_chart_cd(market="a", code="SH.513100", frequency="5m",
                                    frame=frame, last_bar_closed=True)
    assert runtime.cd is not None
    return tuple(runtime.cd.get_segment_units())


def _target_selection(result):
    return next((r for r in result.view_selections
                 if (r["original_zd_tick"], r["original_zg_tick"]) == (2150, 2270)), None)


def test_recorded_inner_view_closes_at_real_return_without_moving_its_extreme(recorded_frame):
    values = _recorded_units(recorded_frame)
    result = calculate_native_center_views(values)
    record = _target_selection(result)
    assert record is not None
    selected = next(c for c in result.centers if c.center_id == record["selected_center_id"])
    assert (selected.zd_tick, selected.zg_tick) == (2154, 2232)
    assert selected.initial_units == values[33:36]
    assert selected.completion_return_unit == values[41]
    ret = selected.completion_return_unit
    assert (ret.end_tick, ret.high_tick) == (2141, 2153)
    assert ret.market_end == pd.Timestamp("2026-07-23 13:35", tz="Asia/Shanghai")
    assert ret.high_market_time == pd.Timestamp("2026-07-22 09:55", tz="Asia/Shanghai")
    assert selected.view_available_at == pd.Timestamp("2026-07-28 13:55", tz="Asia/Shanghai")
    assert selected.available_at >= selected.view_available_at
    assert selected.state is CenterState.COMPLETED
    assert all(c.center_id != record["original_center_id"] for c in result.centers)
    assert any(c.initial_units == values[45:48] for c in result.centers)


def test_recorded_superseded_predecessor_points_to_selected_successor(recorded_frame):
    values = _recorded_units(recorded_frame)
    result = calculate_native_center_views(values)
    record = _target_selection(result)
    selected_offset = next(i for i, c in enumerate(result.centers) if c.center_id == record["selected_center_id"])
    previous = result.centers[selected_offset - 1]
    selected = result.centers[selected_offset]
    assert previous.state is CenterState.SUPERSEDED
    assert previous.superseded_by_center_id == selected.center_id
    assert previous.superseded_at == selected.established_at
    assert previous.supersession_bridge_units[-1] == values[32]
    assert previous.view_available_at == selected.view_available_at
    assert previous.view_id == selected.view_id
    assert previous.available_at >= selected.view_available_at


def test_recorded_full_and_bar_prefix_selection_clocks_agree(recorded_frame):
    full = calculate_native_center_views(_recorded_units(recorded_frame))
    expected = _target_selection(full)
    for cutoff, must_exist in [("2026-07-28 13:50", False), ("2026-07-28 13:55", True)]:
        stamp = pd.Timestamp(cutoff, tz="Asia/Shanghai")
        frame = recorded_frame.loc[recorded_frame.date <= stamp].copy()
        frame.attrs.update(recorded_frame.attrs)
        result = calculate_native_center_views(_recorded_units(frame))
        record = _target_selection(result)
        if must_exist:
            assert record == expected
        else:
            assert record is None
