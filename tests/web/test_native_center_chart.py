"""Native chart payloads from recorded market bars must keep one source identity."""

from pathlib import Path

import pandas as pd
import pytest

from chanlun.cl_utils.strict_chart_runtime import build_strict_chart_cd
from chanlun.cl_utils.tv_chart import cl_data_to_tv_chart


@pytest.mark.parametrize("name,frequency", [
    ("SZ.002299_1m.parquet", "1m"),
    ("SH.600519_5m.parquet", "5m"),
    ("SH.600519_30m.parquet", "30m"),
])
def test_recorded_market_bars_render_native_centers_without_recursive_analysis(name, frequency):
    frame = pd.read_parquet(Path(__file__).parents[1] / "fixtures" / name).head(3000)
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
    assert snapshot["analysis_scope"] == "native_centers"
    assert snapshot["source_frequency"] == snapshot["display_frequency"] == frequency
    assert snapshot["source_closed_at"] == int(frame.iloc[-1].date.timestamp())
    assert len(snapshot["levels"]) == 1
    level = snapshot["levels"][0]
    assert set(level) == {"structural_level", "label", "origin", "centers"}
    assert level["origin"] == "native_segments"
    if frequency == "30m":
        assert not level["centers"]
        assert sum(xd.is_done() for xd in runtime.cd.get_xds()) < 5
    else:
        assert level["centers"]
    assert result["bis"] and result["xds"]
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
