"""A center pair cannot be known before both initial cores are certified."""

from pathlib import Path

import pandas as pd

from chanlun.cl_utils.strict_chart import build_center_snapshot
from chanlun.cl_utils.strict_chart_runtime import build_strict_chart_cd


def test_recorded_parent_pair_requires_two_formal_centers():
    path = Path(__file__).parents[2] / "fixtures/SH.600519_5m.parquet"
    frame = pd.read_parquet(path)
    frame.attrs.update(structure_price_quantum="0.01", price_basis_revision="recorded-raw")
    runtime = build_strict_chart_cd(
        market="a", code="SH.600519", frequency="5m",
        frame=frame, last_bar_closed=True,
    )
    assert runtime.cd is not None
    cd = runtime.cd
    higher = cd.get_strict_evidence().structure.levels[1]
    assert len(higher.center_result.centers) == 1
    snapshot = build_center_snapshot(
        cd, interval="5m",
        display_bar_closed_at=(int(cd._strict_as_of().timestamp()),),
    )
    assert snapshot["levels"][1]["center_pair_rule_states"] == []
