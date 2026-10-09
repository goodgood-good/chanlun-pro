"""SH.513100 5m: F2 ends at pen 284, not a later extension at pen 288.

The frozen market sample is not an author's illustration. The rule basis is
D:/缠论/chanlun_lesson_corpus/L067...85-109 and L077...388-418. Confirmed
physical pens remain required; removing an extra extension wait must neither
backdate the clock to the price pivot nor turn a single pen into a segment.
"""

from pathlib import Path

import pandas as pd
import pytest

from chanlun.cl_utils.strict_chart_runtime import build_strict_chart_cd
from script.check_segment_model import check_evidence


@pytest.fixture(scope="module")
def market_frame():
    return pd.read_parquet(
        Path(__file__).parents[1] / "fixtures/SH.513100_5m_center_20261009.parquet"
    )


def runtime(frame, cutoff):
    prefix = frame.loc[frame.date <= pd.Timestamp(cutoff, tz="Asia/Shanghai")].copy()
    result = build_strict_chart_cd(
        market="a", code="SH.513100", frequency="5m", frame=prefix,
        last_bar_closed=True,
    )
    assert result.cd is not None
    return result.cd


@pytest.mark.parametrize("cutoff,locked", [
    ("2026-07-23 13:50", False),
    ("2026-07-24 10:15", False),
    ("2026-07-24 10:20", True),
    ("2026-10-09 15:00", True),
])
def test_recorded_second_fractal_uses_its_last_required_pen_clock(market_frame, cutoff, locked):
    cd = runtime(market_frame, cutoff)
    pens = cd.get_contiguous_bis()
    by_start = {xd.start_line.index: xd for xd in cd.get_xds()}
    parent, child = by_start[268], by_start[271]
    assert (parent.end_line.index, child.end_line.index) == (270, 281)
    assert parent.construction_evidence.witness_index == 284
    assert child.construction_evidence.parent_key == parent.construction_evidence.key
    assert parent.is_done() is child.is_done() is locked
    if locked:
        expected = pd.Timestamp("2026-07-24 10:20", tz="Asia/Shanghai")
        assert parent.locked_at == child.locked_at == expected == pens[284].locked_at
    else:
        assert parent.locked_at is child.locked_at is None
    check_evidence(cd.xd_calculator, pens)


def test_recorded_following_return_keeps_its_real_range_and_confirmation(market_frame):
    cd = runtime(market_frame, "2026-10-09 15:00")
    by_start = {xd.start_line.index: xd for xd in cd.get_xds()}
    ret = by_start[282]
    assert ret.end_line.index == 284
    assert ret.end.val == 2.141
    assert ret.high == 2.153
    assert ret.locked_at == pd.Timestamp("2026-07-28 13:55", tz="Asia/Shanghai")
    # Advancing F2's causal clock cannot invent a separate segment for either
    # of the one-pen moves at 283 and 284.
    assert 283 not in by_start and 284 not in by_start
