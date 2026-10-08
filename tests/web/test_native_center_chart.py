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
    ("SH.600519_5m.parquet", "5m", 10000),
    ("SH.600519_30m.parquet", "30m", 3000),
])
def test_recorded_market_bars_publish_native_centers_without_recursive_types(
    name, frequency, rows, monkeypatch,
):
    frame = pd.read_parquet(Path(__file__).parents[1] / "fixtures" / name).head(rows)
    frame.attrs.update(structure_price_quantum="0.01", price_basis_revision="recorded-raw")
    code = name.split("_")[0]
    runtime = build_strict_chart_cd(market="a", code=code, frequency=frequency, frame=frame)
    assert runtime.error_code is None
    monkeypatch.setattr(runtime.cd, "get_strict_structure_levels",
                        lambda: pytest.fail("recursive type assembly reached the live chart"))
    monkeypatch.setattr(runtime.cd, "get_strict_evidence",
                        lambda: pytest.fail("formal signal engine reached the live chart"))

    result = cl_data_to_tv_chart(
        frame, {"chart_show_bi": "1", "chart_show_xd": "1"},
        market="a", code=code, frequency=frequency, strict_runtime=runtime,
    )
    assert result["strict_structure_mode"] == "replace"
    snapshot = result["strict_structure"]
    assert snapshot["analysis_scope"] == "native_centers"
    assert snapshot["source_frequency"] == snapshot["display_frequency"] == frequency
    assert snapshot["source_closed_at"] == int(frame.iloc[-1].date.timestamp())
    assert snapshot["formal_point_issued"] is bool(snapshot["levels"][0]["points"])
    assert snapshot["tradable"] is bool(snapshot["levels"][0]["points"])
    assert len(snapshot["levels"]) == 1
    level = snapshot["levels"][0]
    assert level["origin"] == "native_segments"
    assert level["trend_types"] == level["trend_shapes"] == level["divergences"] == []
    assert len(level["centers"]) == len(runtime.cd.get_native_centers().centers)
    assert all(center["source_kind"] == "segment" and center["tradable"] is False
               for center in level["centers"])
    assert all(point["evidence_scope"] == "native_center_signal"
               and point["formal_point_issued"] is True
               and point["status"] == "confirmed"
               and point["point_type"] in {"1buy", "2buy", "3buy", "1sell", "2sell", "3sell"}
               and point["available_at"] <= snapshot["source_closed_at"]
               for point in level["points"])
    assert set(snapshot["point_exit_plans"]) == {point["point_id"] for point in level["points"]}
    for point in level["points"]:
        plan = snapshot["point_exit_plans"][point["point_id"]]
        assert plan["stop_loss"]["price"] == point["invalidation_price"]
        assert plan["take_profit"]["basis"] == "opposite_point"
        assert plan["profit_observation"] is None
    if frequency == "5m":
        assert {point["point_type"][0] for point in level["points"]} == {"3"}


def test_live_and_frozen_native_exit_references_agree_for_all_six_signal_classes():
    from copy import deepcopy
    from chanlun.screening.runner import snapshot_for
    from chanlun.cl_utils.point_exits import build_point_exit_plans

    frame = pd.read_parquet(Path("tests/fixtures/SZ.002299_1m.parquet"))
    frame.attrs.update(structure_price_quantum="0.01", price_basis_revision="recorded-raw")
    snapshot = snapshot_for(frame, "SZ.002299", "1m")
    original = deepcopy(snapshot)
    live = snapshot["point_exit_plans"]
    frozen = build_point_exit_plans(snapshot)
    assert frozen == live
    assert {point["point_type"] for point in snapshot["levels"][0]["points"]} == {
        "1buy", "2buy", "3buy", "1sell", "2sell", "3sell",
    }
    assert any(plan["take_profit"].get("point_type") in {"1buy", "1sell"} for plan in live.values())
    assert snapshot == original

    missing = deepcopy(snapshot)
    point = missing["levels"][0]["points"][-1]
    missing["screening_segments"] = [unit for unit in missing["screening_segments"]
                                     if unit["unit_id"] != point["price_anchor_unit_id"]]
    assert point["point_id"] not in build_point_exit_plans(missing)


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
    assert result["strict_structure"]["analysis_scope"] == "native_centers"
    assert result["strict_structure"]["levels"][0]["centers"] == []
    hidden = cl_data_to_tv_chart(
        frame, {}, market="a", code="TST", frequency="1m", strict_runtime=runtime,
    )
    assert not hidden["bis"] and hidden["strict_structure"]["levels"][0]["centers"] == []


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
        get_src_klines=lambda: [SimpleNamespace(date=cutoff)],
        _strict_as_of=lambda: cutoff, _strict_price_quantum=lambda: Decimal("0.01"),
        _strict_price_basis_revision=lambda: TEST_PRICE_BASIS,
        _strict_config_revision=lambda: "test", get_strict_evidence=lambda: evidence,
    )
    return build_center_snapshot(cd, interval="1m", display_bar_closed_at=(int(cutoff.timestamp()),))


def test_five_locked_roles_establish_center_after_short_previews():
    units = valid_five_up_exit()
    three = _snapshot_for_units(units[:3])["levels"][0]
    four = _snapshot_for_units(units[:4])["levels"][0]
    live = _snapshot_for_units((*units[:4], replace(units[4], locked=False, confirmed_at=None)))["levels"][0]
    locked = _snapshot_for_units(units)["levels"][0]
    assert three["centers"] == four["centers"] == live["centers"] == []
    assert four["center_previews"][0]["preview_status"] == "awaiting_leave"
    assert live["center_previews"][0]["preview_status"] == "awaiting_segment_confirmation"
    center, = locked["centers"]
    assert center["formation_rule"] == "five_role"
    assert center["entry_unit_id"] == units[0].unit_id
    assert center["establishment_leave_unit_id"] == units[4].unit_id
    assert center["core_unit_ids"] == [unit.unit_id for unit in units[1:4]]
    assert locked["center_previews"] == []


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
def test_failed_departure_preserves_five_role_seed_and_stops_at_core_break(mirror, locked_tail):
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
    owner, = layer["centers"]
    frame = owner if locked_tail else layer["center_previews"][0]
    assert owner["formation_rule"] == "five_role"
    assert frame["entry_unit_id"] == units[0].unit_id
    assert frame["establishment_leave_unit_id"] == units[4].unit_id
    assert frame["failed_departure_unit_ids"] == [units[4].unit_id]
    assert frame["body_unit_ids"] == [unit.unit_id for unit in (*units[1:4], *units[5:7])]
    assert units[7].unit_id not in frame["body_unit_ids"]
    if not locked_tail:
        assert frame["owner_center_id"] == owner["center_id"]
        assert frame["draw_geometry"] and not frame["third_class_confirmed"]
