"""L078 segment ends remain physical while centers use whole-segment ranges."""

from pathlib import Path

import pandas as pd

from chanlun.cl_utils.strict_chart_runtime import build_strict_chart_cd


def test_recorded_nonextreme_segment_ends_keep_physical_ranges_and_head_tail():
    # L078:280-313 permits extreme-to-extreme price projection only when a
    # segment is used as an elementary upper-level object. Its actual market
    # endpoint remains the next segment's shared structural boundary.
    frame = pd.read_parquet(
        Path(__file__).parents[2] / "fixtures" / "segment_clock" / "SH.600658_5m.parquet"
    )
    frame.attrs.update(structure_price_quantum="0.01", price_basis_revision="recorded-raw")
    runtime = build_strict_chart_cd(
        market="a", code="SH.600658", frequency="5m",
        frame=frame, last_bar_closed=True,
    )
    assert runtime.cd is not None
    level = runtime.cd.get_strict_evidence().structure.levels[0]
    examples = (
        ("7f7ea43e4bf6de0c8628d8f1a9aec07054f475c7aa2cfd8f40c4d04bebe89651",
         537, 565, 524, 565),
        ("86113329e01ac9c1ae76f744a6932fd28c6d381297ac8cecb4a397c4f947858e",
         512, 530, 512, 537),
    )
    positions = {unit.unit_id: index for index, unit in enumerate(level.units)}
    for unit_id, start_tick, end_tick, low_tick, high_tick in examples:
        unit = level.units[positions[unit_id]]
        assert (unit.start_tick, unit.end_tick, unit.low_tick, unit.high_tick) == (
            start_tick, end_tick, low_tick, high_tick
        )
        assert low_tick < min(start_tick, end_tick) or high_tick > max(start_tick, end_tick)
        if positions[unit_id] + 1 < len(level.units):
            following = level.units[positions[unit_id] + 1]
            assert (unit.market_end, unit.end_tick) == (
                following.market_start, following.start_tick
            )
    for center in level.center_result.centers:
        assert center.zd_tick == max(member.low_tick for member in center.initial_units)
        assert center.zg_tick == min(member.high_tick for member in center.initial_units)
