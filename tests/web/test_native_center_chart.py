"""Native chart payloads from recorded market bars must keep one source identity."""

from pathlib import Path
from decimal import Decimal
from types import SimpleNamespace
from dataclasses import replace

import pandas as pd
import pytest

from chanlun.cl_utils.strict_chart_runtime import build_strict_chart_cd
from chanlun.cl_utils.strict_chart import build_center_snapshot
from chanlun.cl_utils.tv_chart import cl_data_to_tv_chart
from chanlun.core.strict_structure.center_machine import calculate_centers
from chanlun.core.strict_structure.models import SourceKind
from tests.core.strict_structure.helpers import TEST_PRICE_BASIS, unit, valid_five_up_exit


@pytest.mark.parametrize("name,frequency,rows", [
    ("SZ.002299_1m.parquet", "1m", 3000),
    # Long enough to exercise centers beyond historical short conflicts.
    ("SH.600519_5m.parquet", "5m", 10000),
    ("SH.600519_30m.parquet", "30m", 3000),
])
def test_recorded_market_bars_render_centers_and_signals_from_one_source(name, frequency, rows):
    frame = pd.read_parquet(Path(__file__).parents[1] / "fixtures" / name).head(rows)
    frame.attrs.update(structure_price_quantum="0.01", price_basis_revision="recorded-raw")
    code = name.split("_")[0]
    runtime = build_strict_chart_cd(market="a", code=code, frequency=frequency, frame=frame)
    assert runtime.error_code is None
    result = cl_data_to_tv_chart(
        frame, {"chart_show_bi": "1", "chart_show_xd": "1"},
        market="a", code=code, frequency=frequency, strict_runtime=runtime,
    )
    assert result["strict_structure_mode"] == "replace"
    snapshot = result["strict_structure"]
    assert snapshot["analysis_scope"] == "centers_and_signals"
    assert snapshot["source_frequency"] == snapshot["display_frequency"] == frequency
    assert snapshot["source_closed_at"] == int(frame.iloc[-1].date.timestamp())
    assert len(snapshot["levels"]) >= 1
    items = [item for layer in snapshot["levels"]
             for field in ("centers", "center_previews", "points", "divergences") for item in layer[field]]
    items.extend(snapshot["stroke_center_observations"])
    for item in items:
        assert item["price_basis_revision"] == snapshot["price_basis_revision"]
        assert type(item["available_at"]) is int
        assert item["available_at"] <= snapshot["source_closed_at"]
    level = snapshot["levels"][0]
    assert set(level) == {"structural_level", "label", "origin", "centers", "center_previews", "points", "divergences"}
    assert level["origin"] == "native_segments"
    assert not result["stroke_construction"]["unresolved_regions"]
    assert not snapshot["conditional_centers"]
    continuous = runtime.cd.get_contiguous_bis()
    assert len(continuous) == len(runtime.cd.get_bis())
    assert all(x.end_line.index < len(continuous) for x in runtime.cd.get_xds())
    assert all(not item["tradable"] for item in level["center_previews"])
    assert result["bis"] and result["xds"]
    assert all(item["state"] in {"locked", "forming"} for item in result["bis"])
    assert any(item["locked"] for item in result["bis"])
    assert all(item["locked"] == item["completion_is_final"] == (item["state"] == "locked")
               for item in result["bis"])
    assert all(item["component_index"] == 0 and not item["selection_pending"] for item in result["bis"])
    assert "stroke_center_observations" in snapshot
    for item in snapshot["conditional_centers"]:
        assert item["render_kind"] == "conditional_center"
        assert item["selection_pending"] and item["component_index"] > 0
        assert not item["tradable"] and not item["locked"] and not item["third_class_confirmed"]
        assert item["center_id"] not in {c["center_id"] for c in level["centers"]}
    for item in result["xds"]:
        if item["component_index"] > 0:
            assert item["state"] == "forming" and item["selection_pending"] and not item["locked"]
            assert item["observation_scope_id"]
    for item in snapshot["stroke_center_observations"]:
        assert item["source_kind"] == "stroke_observation" and not item["tradable"]
    for item in level["points"]:
        assert item["point_type"] in {"1buy", "2buy", "3buy", "1sell", "2sell", "3sell"}
        assert item["available_at"] <= snapshot["source_closed_at"]
    for item in level["divergences"]:
        assert item["metrics"]["is_divergent"] and item["available_at"] <= snapshot["source_closed_at"]
    for center in level["centers"]:
        assert center["formation_rule"] == "five_role"
        assert center["entry_unit_id"] not in center["core_unit_ids"]
        assert center["establishment_leave_unit_id"] not in center["core_unit_ids"]
        assert center["core"]["zd_tick"] < center["core"]["zg_tick"]
        assert center["available_at"] <= snapshot["source_closed_at"]
    # Re-reading the same completed bars must neither search alternatives nor change identity.
    repeated = cl_data_to_tv_chart(frame, {}, market="a", code=code,
                                   frequency=frequency, strict_runtime=runtime)
    assert repeated["strict_structure"] == snapshot


def test_initial_reselection_renders_one_pending_pen_without_a_future_lock():
    from script.review_stroke_rule_logic import COMPLETION_RETRACTION
    from tests.core.test_bi_source_updates import _frame

    frame = _frame(COMPLETION_RETRACTION, False)
    frame["date"] = frame["date"].dt.tz_localize("UTC")
    frame.attrs.update(structure_price_quantum="0.01", price_basis_revision="test-raw")
    runtime = build_strict_chart_cd(market="a", code="TST", frequency="1m", frame=frame)
    assert runtime.error_code is None
    result = cl_data_to_tv_chart(
        frame, {"chart_show_bi": "1", "chart_show_xd": "1"},
        market="a", code="TST", frequency="1m", strict_runtime=runtime,
    )
    assert [item["component_index"] for item in result["bis"]] == [0]
    assert [item["state"] for item in result["bis"]] == ["forming"]
    assert not result["bis"][0]["completion_is_final"]
    assert result["stroke_construction"]["status"] == "tail_pending"
    assert "processing_mode" not in result["stroke_construction"]
    assert not result["stroke_construction"]["unresolved_regions"]
    assert not result["xds"]
    assert result["strict_structure_mode"] == "replace"
    assert not result["strict_structure"]["stroke_connection_pending"]
    hidden = cl_data_to_tv_chart(
        frame, {}, market="a", code="TST", frequency="1m", strict_runtime=runtime,
    )
    assert not hidden["bis"] and not hidden["strict_structure"]["stroke_connection_pending"]


def _snapshot_for_units(units):
    result = calculate_centers(units, 0, SourceKind.SEGMENT)
    level = SimpleNamespace(structural_level=0, units=units, center_result=result)
    evidence = SimpleNamespace(
        structure=SimpleNamespace(levels=(level,)), confirmed_points=(),
        approaching_points=(), divergences=(),
        stroke_center_observations=SimpleNamespace(centers=()),
    )
    cutoff = max(value.available_at for value in units)
    cd = SimpleNamespace(
        get_frequency=lambda: "1m", get_code=lambda: "test",
        _strict_as_of=lambda: cutoff, _strict_price_quantum=lambda: Decimal("0.01"),
        _strict_price_basis_revision=lambda: TEST_PRICE_BASIS,
        _strict_config_revision=lambda: "test", get_strict_evidence=lambda: evidence,
    )
    return build_center_snapshot(cd, interval="1m", display_bar_closed_at=(int(cutoff.timestamp()),))


def test_forming_core_promotes_only_after_independent_exit_locks():
    units = valid_five_up_exit()
    four = _snapshot_for_units(units[:4])["levels"][0]
    live = _snapshot_for_units((*units[:4], replace(units[4], locked=False, confirmed_at=None)))["levels"][0]
    locked = _snapshot_for_units(units)["levels"][0]
    assert not four["centers"] and not live["centers"]
    assert len(four["center_previews"]) == len(live["center_previews"]) == 1
    first, second = four["center_previews"][0], live["center_previews"][0]
    assert first["preview_status"] == "awaiting_leave"
    assert first["establishment_segment_ids"] == [value.unit_id for value in units[:4]]
    assert second["preview_status"] == "awaiting_segment_confirmation"
    assert len(locked["centers"]) == 1 and locked["center_previews"] == []
    assert first["points"] == second["points"] == locked["centers"][0]["points"]
    assert second["center_id"] == locked["centers"][0]["center_id"]
    assert not first["tradable"] and not second["third_class_confirmed"]


def test_unlocked_return_does_not_duplicate_or_complete_the_formal_owner():
    units = (*valid_five_up_exit(), unit(5, "down", 130, 120, locked=False))
    layer = _snapshot_for_units(units)["levels"][0]
    assert len(layer["centers"]) == len(layer["center_previews"]) == 1
    owner, preview = layer["centers"][0], layer["center_previews"][0]
    assert preview["owner_center_id"] == owner["center_id"]
    assert preview["preview_status"] == "awaiting_completion_confirmation"
    assert preview["draw_geometry"] is False
    assert not owner["third_class_confirmed"] and not preview["third_class_confirmed"]
    assert not layer["points"]


def test_unconfirmed_extension_starts_at_confirmed_frame_end_without_moving_core():
    units = (*valid_five_up_exit(), unit(5, "down", 130, 110, locked=False))
    before = _snapshot_for_units(units[:5])["levels"][0]["centers"][0]
    layer = _snapshot_for_units(units)["levels"][0]
    owner, preview = layer["centers"][0], layer["center_previews"][0]
    assert owner == before
    assert preview["owner_center_id"] == owner["center_id"]
    assert preview["draw_geometry"] is True
    assert preview["points"][0]["time"] == owner["points"][-1]["time"]
    assert preview["points"][-1]["time"] == int(units[-1].market_end.timestamp())
    assert preview["core"] == owner["core"]


@pytest.mark.parametrize("mirror", [False, True])
@pytest.mark.parametrize("locked_tail", [False, True])
def test_opposite_departure_preserves_extensions_and_excludes_the_current_exit(mirror, locked_tail):
    units = (*valid_five_up_exit(),
             unit(5, "down", 130, 110, locked=locked_tail),
             unit(6, "up", 110, 114, locked=locked_tail),
             unit(7, "down", 114, 100, locked=locked_tail),
             unit(8, "up", 100, 104, locked=locked_tail))
    if mirror:
        units = tuple(replace(value, direction="down" if value.direction == "up" else "up",
                              start_tick=260-value.start_tick, end_tick=260-value.end_tick,
                              low_tick=260-value.high_tick, high_tick=260-value.low_tick)
                      for value in units)
    layer = _snapshot_for_units(units)["levels"][0]
    owner = layer["centers"][0]
    frame = owner if locked_tail else layer["center_previews"][0]
    assert frame["points"][-1]["time"] == int(units[7].market_start.timestamp())
    assert frame["leaving_segment"]["unit_id"] == units[7].unit_id
    assert frame["establishment_leave_unit_id"] == units[4].unit_id
    assert frame["failed_departure_unit_ids"] == [units[4].unit_id]
    assert units[7].unit_id not in frame["body_unit_ids"]
    assert units[8].unit_id not in frame["body_unit_ids"]
    if locked_tail:
        pending = _snapshot_for_units(units[:-1])["levels"][0]["centers"][0]
        # Confirming the opposite return must not shrink an already drawn body.
        assert frame["points"] == pending["points"]
        assert set(frame["frame_body_unit_ids"]) == {value.unit_id for value in units[1:7]}
    else:
        assert frame["owner_center_id"] == owner["center_id"]
        assert frame["draw_geometry"] and not frame["third_class_confirmed"]
        assert frame["points"][0]["time"] == owner["points"][-1]["time"]
