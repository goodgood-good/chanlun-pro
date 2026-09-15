"""Regressions from recorded bars; expected full partitions are not invented.

The fixture manifest records exact source hashes and raw-prefix limits.
Original sources: D:/缠论/chanlun_lesson_corpus, L062 37–100 (geometry),
L065 166–214 and L077 247–283 (adjacency/selection). The shared-path
constraint is an engineering interpretation, not a universal finality proof.
"""

import copy
from pathlib import Path
import pickle

import pandas as pd
import pytest

from chanlun.core.bi_calculator import BiCalculator
from chanlun.core.cl import CL
from chanlun.core.cl_kline_process import CL_Kline_Process
from chanlun.core.types import Kline
from tests.core.test_stroke_unresolved_connections import state, update


FIXTURES = Path(__file__).parents[1] / "fixtures/stroke_v13"


def raw_bars(frame):
    return [
        Kline(
            index=i,
            date=row.date.to_pydatetime(),
            h=row.high,
            l=row.low,
            o=row.open,
            c=row.close,
            a=row.volume,
        )
        for i, row in enumerate(frame.itertuples(index=False))
    ]


def cold(raw, direction=None):
    merged, calc = CL_Kline_Process(_initial_direction=direction), BiCalculator()
    merged.process_cl_klines(raw)
    calc.calculate_batch(merged.cl_klines)
    return merged, calc


def geometry(lines):
    return [(b.start.k.date, b.end.k.date, b.start.val, b.end.val) for b in lines]


def cl(frame, frequency="1m"):
    result = CL(
        "TST",
        frequency,
        {"price_basis_revision": "recorded-raw", "structure_price_quantum": "0.01"},
        market="a",
    )
    return result.process_klines_batch(frame, last_bar_closed=False)


@pytest.mark.parametrize("mirror", [False, True])
def test_initial_geometry_consensus_keeps_the_shared_left_shoulder(mirror):
    raw = raw_bars(pd.read_parquet(FIXTURES / "SH.600189_5m.parquet"))
    if mirror:
        for k in raw:
            k.h, k.l, k.o, k.c = -k.l, -k.h, -k.o, -k.c
    merged, result = cold(raw[:5])
    _, up = cold(raw[:5], "up")
    _, down = cold(raw[:5], "down")
    common = {(f.k.date, f.type, f.val) for f in up.fxs} & {
        (f.k.date, f.type, f.val) for f in down.fxs
    }
    assert common == {
        (raw[3].date, "di" if mirror else "ding", -7.40 if mirror else 7.40)
    }
    assert {(f.k.date, f.type, f.val) for f in result.fxs} == common
    assert result.fxs[0].k.k_index == 3
    alternatives = merged.initial_source_alternatives
    assert len(alternatives) == 1
    assert {alternatives[0].up_sources, alternatives[0].down_sources} == {
        (2,),
        (0, 1, 2),
    }
    # Explicit left contexts can expose an earlier competing fractal. Only
    # shared physical facts, not the resulting whole selections, must agree.
    _, result = cold(raw)
    _, up = cold(raw, "up")
    _, down = cold(raw, "down")
    common = {(f.k.date,f.type,f.val) for f in up.fxs} & {(f.k.date,f.type,f.val) for f in down.fxs}
    assert {(f.k.date,f.type,f.val) for f in result.fxs} == common
    assert result.bis
    assert all((f.k.date,f.type,f.val) in common for b in result.bis for f in (b.start,b.end))


def test_initial_provenance_resets_when_previous_context_is_corrected():
    raw = raw_bars(pd.read_parquet(FIXTURES / "SH.600189_5m.parquet").head(5))
    merged, _ = cold(raw)
    assert merged.initial_source_alternatives
    corrected = copy.deepcopy(raw)
    corrected[0].h, corrected[0].l = 7.42, 7.40
    merged.process_cl_klines(corrected)
    expected, _ = cold(corrected)
    assert not merged.initial_source_alternatives
    assert not merged.initial_context_unresolved
    assert [BiCalculator._kline_sig(k) for k in merged.cl_klines] == [
        BiCalculator._kline_sig(k) for k in expected.cl_klines
    ]


def test_different_left_shoulders_can_confirm_the_same_first_fractal():
    raw = raw_bars(pd.read_parquet(FIXTURES / "SH.600088_30m.parquet"))
    merged, result = cold(raw[:5])
    assert len(merged.cl_klines[0].initial_context_alternatives) == 2
    assert [(f.k.k_index, f.type, f.val) for f in result.fxs] == [(3, "di", 16.31)]
    assert all(f.k.index > 0 for f in result.fxs)
    up, down = merged.cl_klines[0].initial_context_alternatives
    assert (up.h, down.h) == (16.46, 16.45)


def test_a_single_initial_direction_cannot_supply_a_consensus_fractal():
    from chanlun.core.stroke_rules import fractal_kind
    from tests.core.strict_structure.test_source_stroke_revision import _bars

    # Test the boundary contract directly with two actual possible shoulders.
    # One permits a bottom, the other does not: it is not shared evidence.
    p = CL_Kline_Process(_initial_direction="up")
    p.process_cl_klines(_bars([(15, 11), (13, 9), (14, 10)]))
    left, middle, right = p.cl_klines
    other = copy.copy(left)
    other.h, other.l = 12, 8
    left.initial_context_alternatives = (copy.copy(left), other)
    assert fractal_kind(left, middle, right) is None


@pytest.mark.parametrize("filename", ["SH.600088_30m.parquet", "SH.600519_5m.parquet"])
def test_rejected_parent_cannot_erase_a_retained_turn_on_the_next_fractal(filename):
    raw = raw_bars(pd.read_parquet(FIXTURES / filename))
    merged, live = CL_Kline_Process(), BiCalculator()
    previous_done = set()
    for count in range(1, len(raw) + 1):
        update(merged, live, raw[:count])
        _, expected = cold(raw[:count])
        assert state(live) == state(expected)
        assert previous_done <= set(geometry(live.bis))
        previous_done = set(geometry(live.confirmed_bis))
    assert live.bis and len(live.stroke_components) == 1
    assert geometry(live.contiguous_bis) == geometry(live.bis)
    assert not live.unresolved_regions
    assert len(live._resolver.nodes) == len(live.fxs)
    assert all(not decision.revises_confirmed_selection for decision in live.selection_revisions)
    if filename == "SH.600519_5m.parquet":
        assert previous_done and live._resolver.rejected_paths
    else:
        # This shorter fixture contains fewer than three selected edges.
        assert len(live.bis) < 3 and not previous_done


@pytest.mark.parametrize("filename", ["SH.600519_1m.parquet", "SZ.002299_1m.parquet"])
def test_continuous_segments_update_with_the_same_formal_source_identity(filename):
    from chanlun.core.strict_structure.models import SourceKind
    from chanlun.core.strict_structure.unit_adapter import adapt_lines

    frame = pd.read_parquet(FIXTURES / filename)
    live = cl(frame.iloc[:-1])
    done_before = set(geometry(x for x in live.get_xds() if x.is_done()))
    live.process_klines(frame.iloc[-1:])
    cold_result = cl(frame)
    assert done_before <= set(geometry(live.get_xds()))
    assert state(live.bi_calculator) == state(cold_result.bi_calculator)
    assert geometry(live.get_chart_xds()) == geometry(cold_result.get_chart_xds())
    observations = live.stroke_observations.observations
    assert observations == ()
    assert geometry(live.get_chart_xds()) == geometry(live.get_xds())
    segments=live.get_xds()
    units=adapt_lines(segments,0,SourceKind.SEGMENT,live._strict_price_quantum(),
                      live._strict_as_of(),live._strict_registry(),constituent_lines=live.get_bis())
    assert units and len(units)==len(segments)
    for xd,unit in zip(segments,units):
        assert not getattr(xd,'selection_pending',False) and unit.locked==xd.is_done()
        assert xd.start_line in live.get_bis() and xd.end_line in live.get_bis()
        assert xd.start_line.component_index==xd.end_line.component_index==0
    # Serialization, historical replacement, then continuation have one result.
    restored = pickle.loads(pickle.dumps(live))
    assert geometry(restored.get_chart_xds()) == geometry(live.get_chart_xds())
    restored.process_klines_batch(frame.head(8), last_bar_closed=False)
    assert not restored.stroke_observations.observations
    restored.process_klines(frame)
    assert geometry(restored.get_chart_xds()) == geometry(live.get_chart_xds())
    assert restored.get_conditional_centers() == cold_result.get_conditional_centers()


def test_pending_scope_is_removed_when_a_real_connection_resumes():
    from tests.core.test_bi_source_updates import _frame

    values = [
        (19, 15),
        (20, 16),
        (18, 14),
        (16, 12),
        (14, 10),
        (12, 8),
        (14, 10),
        (16, 12),
        (18, 14),
        (22, 18),
        (15, 9),
        (10, 6),
        (12, 8),
        (14, 10),
        (16, 12),
        (21, 17),
        (17, 13),
        (14, 10),
        (11, 7),
        (9, 5),
        (10, 6),
    ]
    frame = _frame(values, False)
    frame["date"] = frame.date.dt.tz_localize("UTC")
    result = cl(frame.head(17))
    assert not result.stroke_observations.observations
    assert len(result.get_bis()) == 2 and not result.bi_calculator.confirmed_bis
    result.process_klines(frame)
    assert not result.stroke_observations.observations
    assert not result.stroke_observations._scopes
    assert result.get_conditional_centers() == ()
