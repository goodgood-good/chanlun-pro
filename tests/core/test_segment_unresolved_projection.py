"""A display fallback must not become a segment or selection carrier."""

from pathlib import Path
from datetime import datetime
from zoneinfo import ZoneInfo
import hashlib
import json

import pandas as pd
import pytest

from chanlun.core.xd_calculator import XdCalculator
from chanlun.cl_utils.strict_chart_runtime import build_strict_chart_cd
from chanlun.cl_utils.tv_chart import cl_data_to_tv_chart
from tests.core.test_segment_source_rules import strokes


@pytest.mark.parametrize('mirror', [False, True])
def test_tail_past_the_origin_does_not_borrow_a_historical_endpoint(mirror):
    values = strokes([12, 7, 10, 8, 13, 12.5, 14], mirror)
    calc = XdCalculator()
    calc._emit_pending(values, 0, values[0].type)
    assert calc.xds == []
    assert calc.tail_state.projection_reason == 'tail-end-direction-conflict'
    assert calc.tail_state.projection_index is None
    assert calc.tail_state.observed_end_index == len(values) - 1
    # A subsequent genuine directional extreme may supply a normal preview.
    grown = strokes([12, 7, 10, 8, 13, 12.5, 14, 6], mirror)
    calc._emit_pending(grown, 0, grown[0].type)
    assert len(calc.xds) == 1 and not calc.xds[0].done
    assert calc.xds[0].end_line.index == 6
    assert calc.tail_state.projection_reason == 'directional-extreme'


@pytest.mark.parametrize('mirror', [False, True])
def test_non_extreme_current_tail_still_has_a_directional_preview(mirror):
    values = strokes([12, 7, 10, 8, 13, 9, 14], mirror)
    calc = XdCalculator()
    calc._emit_pending(values, 0, values[0].type)
    assert len(calc.xds) == 1 and not calc.xds[0].done
    assert calc.xds[0].end_line.index == 4
    assert calc.tail_state.projection_reason == 'latest-same-direction'


@pytest.mark.parametrize('mirror', [False, True])
def test_qualified_first_break_keeps_its_named_hypothesis_before_the_third_pen(mirror):
    points = [30, 20, 29, 12, 32, 14, 26, 18, 30, 21, 28, 21, 28, 8, 10]
    calc = XdCalculator()
    before = calc.calculate(strokes(points, mirror))
    assert len(calc.evidence) == 1
    assert len(before) == 2 and not before[-1].done
    assert before[-1].end_line.index == 11
    assert calc.tail_state.projection_reason == 'boundary-hypothesis'
    assert calc.tail_state.candidate_index == 11
    after = calc.calculate(strokes(points + [5], mirror))
    assert after[1].done and after[1].end_line.index == 11


@pytest.fixture(scope='module')
def sh600183():
    root = Path(__file__).parents[1] / 'fixtures/segment_projection'
    path = root / 'SH.600183_5m_20250922_20260918.parquet'
    proof = json.loads((root / 'SH.600183_5m_20250922_20260918.provenance.json').read_text(encoding='utf-8'))
    assert hashlib.sha256(path.read_bytes()).hexdigest() == proof['sha256']
    frame = pd.read_parquet(path)
    runtime = build_strict_chart_cd(market='a', code='SH.600183', frequency='5m', frame=frame, last_bar_closed=True)
    assert runtime.error_code is None
    return frame, runtime


def test_authentic_contained_first_break_does_not_freeze_the_parent_at_8368(sh600183):
    frame, runtime = sh600183
    cd = runtime.cd
    lines = cd.get_xds()
    assert len(lines) == 103 and sum(x.done for x in lines) == 102
    # The normalized first-break policy now completes the March boundary at
    # 61.39 using the effective 62.85 edge. The independent May F2 rejection
    # at 83.68 remains valid under the same unchanged second-sequence rules.
    march=[x for x in lines if x.start_line.index in {336,347,352}]
    assert [(x.start.val,x.end.val) for x in march]==[(65.45,61.39),(61.39,63.36),(63.36,54.75)]
    assert march[0].construction_evidence.first_break_evidence.effective_first.high==62.85
    chain = [x for x in lines if x.start_line.index in {442, 445, 464, 469}]
    assert [(x.start.val, x.end.val) for x in chain] == [
        (80.42, 75.9), (75.9, 99.94), (99.94, 87.16), (87.16, 106.9)]
    when = datetime(2026, 5, 15, 10, 10, tzinfo=ZoneInfo('Asia/Shanghai'))
    assert chain[0].locked_at == chain[1].locked_at == when
    assert all(u.locked for u in cd.get_segment_units()[:-1])
    parent = chain[0].construction_evidence
    rejected, = parent.second_sequence_breaks
    assert (rejected.first_pen_index, rejected.fractal_witness_index,
            rejected.outcome, rejected.outcome_witness_index) == (456, 458, 'origin-return', 459)
    state = cd.get_segment_construction_state()
    assert state['status'] == 'forming'
    assert state['tail']['start_price'] == 156.93
    assert state['tail']['pen_count'] == 11
    assert state['tail']['observed_through'] == int(frame.date.iloc[-1].timestamp())
    assert state['tail']['preview_end_pen'] == 736


def test_repaired_market_history_is_covered_by_segments_instead_of_a_long_unassigned_region(sh600183):
    frame, runtime = sh600183
    chart = cl_data_to_tv_chart(frame, {'chart_show_xd':'1', 'chart_show_bi':'1'},
                               market='a', code='SH.600183', frequency='5m', strict_runtime=runtime)
    assert len(chart['xds']) == 103 and all(s['locked'] for s in chart['xds'][:-1])
    assert not chart['xds'][-1]['locked']
    snapshot = chart['strict_structure']
    assert snapshot['unresolved_segment_ranges'] == []
    assert snapshot['segment_construction']['status'] == 'forming'
    assert all(left['points'][-1] == right['points'][0]
               for left, right in zip(chart['xds'], chart['xds'][1:]))
    assert all(c['render_kind'] != 'segment_unresolved_range' for level in snapshot['levels'] for c in level['centers'])
