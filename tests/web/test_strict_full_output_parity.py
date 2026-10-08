"""Recorded-source parity for the one-period center reminder pipeline."""

import json
from pathlib import Path
import re

import pandas as pd
import pytest

from chanlun.cl_utils.strict_chart_runtime import build_strict_chart_cd
from chanlun.core.strict_structure.center_relation import classify_center_relation
from chanlun.core.strict_structure.models import CenterRelation
from chanlun.screening.runner import snapshot_for


FIXTURES = Path(__file__).parents[1] / "fixtures"
RECORDED_SOURCES = tuple(sorted(FIXTURES.rglob("*.parquet")))


def _symbol_and_frequency(path):
    name = path.stem.replace("_US_", ".US_").replace("SH_", "SH.").replace("SZ_", "SZ.")
    match = re.fullmatch(r"(.+?)_(1m|5m|30m)(?:_.+)?", name)
    assert match is not None, path
    code, frequency = match.groups()
    return code, frequency, "us" if ".US" in code else "a"


@pytest.mark.parametrize("path", RECORDED_SOURCES, ids=lambda path: str(path.relative_to(FIXTURES)))
def test_native_chart_and_screening_keep_center_and_reminder_source_ids(path):
    code, frequency, market = _symbol_and_frequency(path)
    frame = pd.read_parquet(path)
    frame.attrs.update(
        structure_price_quantum="0.01",
        price_basis_revision="recorded-raw",
        screening_market=market,
    )
    runtime = build_strict_chart_cd(
        market=market, code=code, frequency=frequency,
        frame=frame, last_bar_closed=True,
    )
    assert runtime.cd is not None
    snapshot = snapshot_for(frame, code, frequency)
    json.dumps(snapshot, allow_nan=False)
    cutoff = runtime.cd._strict_as_of()
    assert snapshot["source_closed_at"] == int(cutoff.timestamp())
    assert snapshot["source_frequency"] == snapshot["display_frequency"] == frequency
    assert snapshot["analysis_scope"] == "native_centers"
    assert snapshot["native_member_policy"]["source_frequency"] == frequency
    assert snapshot["native_member_policy"]["member_basis"] == "native_locked_segment"
    assert snapshot["native_member_policy"]["recursive_type_proven"] is False
    assert snapshot["declared_30m_fixed_view"] is None
    assert snapshot["formal_point_issued"] is bool(snapshot["levels"][0]["points"])
    assert len(snapshot["levels"]) == 1
    level = snapshot["levels"][0]
    assert level["trend_types"] == level["trend_shapes"] == level["divergences"] == []
    expected = {center.center_id for center in runtime.cd.get_native_centers().centers
                if center.available_at <= cutoff}
    actual = {item["center_id"] for item in level["centers"]}
    assert actual == expected
    centers = tuple(runtime.cd.get_native_centers().centers)
    center_index = {item.center_id: index for index, item in enumerate(centers)}
    units = tuple(runtime.cd.get_segment_units())
    unit_index = {unit.unit_id: index for index, unit in enumerate(units)}
    point_index = {item["point_id"]: item for item in level["points"]}
    assert len(point_index) == len(level["points"])
    assert {item["unit_id"] for item in snapshot["screening_segments"]} == {
        unit.unit_id for unit in runtime.cd.get_segment_units()
    }
    for item in level["points"]:
        assert item["center_id"] in actual
        assert item["source_kind"] == "segment"
        assert item["evidence_scope"] == "native_center_signal"
        assert item["status"] == "confirmed"
        assert item["formal_point_issued"] is True
        assert item["available_at"] <= snapshot["source_closed_at"]
        center = centers[center_index[item["center_id"]]]
        if item["point_type"].startswith("1"):
            assert center_index[center.center_id] > 0
            previous = centers[center_index[center.center_id] - 1]
            relation = classify_center_relation(previous, center)
            assert relation is (CenterRelation.DOWN_TREND if item["side"] == "buy"
                                else CenterRelation.UP_TREND)
            divergence = item["divergence"]
            assert divergence["histogram_area_decayed"] is True
            assert divergence["compare_leg_unit_ids"][-1] == center.entry_unit.unit_id
            assert len(divergence["signal_leg_unit_ids"]) == 3
            assert center.completion_leave_unit.unit_id in divergence["signal_leg_unit_ids"]
            assert center.completion_return_unit.unit_id in divergence["signal_leg_unit_ids"]
            assert (item["anchor_tick"] < center.zd_tick if item["side"] == "buy"
                    else item["anchor_tick"] > center.zg_tick)
        elif item["point_type"].startswith("2"):
            parent = point_index[item["parent_point_id"]]
            assert parent["point_type"] == "1" + item["side"]
            assert parent["available_at"] <= item["confirmed_at"] <= item["available_at"]
            assert parent["anchor_at"] < item["anchor_at"]
            assert item["related_unit_ids"][0] == parent["price_anchor_unit_id"]
            assert len(item["related_unit_ids"]) == 3
            assert (item["anchor_tick"] >= parent["anchor_tick"] if item["side"] == "buy"
                    else item["anchor_tick"] <= parent["anchor_tick"])
        else:
            leave, ret = center.completion_leave_unit, center.completion_return_unit
            assert leave is not None and ret is not None
            assert item["related_unit_ids"] == [leave.unit_id, ret.unit_id]
            assert unit_index[ret.unit_id] == unit_index[leave.unit_id] + 1
            assert item["center_ordinal"] >= 1
            assert (ret.low_tick >= center.zg_tick if item["side"] == "buy"
                    else ret.high_tick <= center.zd_tick)
    assert all(field not in snapshot for field in (
        "selected_user_center_policy", "selected_five_segment_centers",
        "selected_center_type_partition", "user_same_level_type_view",
    ))
