"""Later standard fractals must not be lost to a stale record reference."""

from pathlib import Path
from collections import Counter

import pandas as pd
import pytest

from chanlun.core.cl import CL
from chanlun.core.xd_calculator import XdCalculator
from script.check_segment_model import check_evidence
from script.check_segment_candidates import SearchAuditCalculator
from tests.core.test_segment_source_rules import CASES, geometry, strokes


@pytest.mark.parametrize("mirror", [False, True])
def test_complete_standard_local_bottom_with_ordinary_overlap(mirror):
    # SZ.000852 B16..25, scaled only for readable input. The chronological
    # up features are [28,43], [45,54] (merged), [38,50], [48,334].
    # The latter three form a bottom despite the earlier low of 28.
    points = [55,28,43,40,54,45,52,38,50,48,334]
    values = strokes(points, mirror)
    calc = XdCalculator()
    # Supply an established prior segment rather than asking this fixture to
    # decide the observation origin from its initially contained three pens.
    calc._build_segments(values, 0)
    assert geometry(calc.xds)[0] == (0, 7, True)
    proof = calc.evidence[0]
    assert proof.reference_mode == "standard-local"
    assert proof.first_sequence[0].source_indices == (3,5)
    check_evidence(calc, values)


@pytest.mark.parametrize("name", ["79_upper", "79_lower", "79_equal9and7", "81_reference"])
@pytest.mark.parametrize("mirror", [False, True])
def test_local_standard_fallback_preserves_original_figure_contexts(name, mirror):
    points, expected = CASES[name]
    assert geometry(XdCalculator().calculate(strokes(points,mirror))) == expected


@pytest.mark.parametrize("code,frequency,start,end,rule", [
    ("SZ.000852","5m",16,22,'standard-local'),
    ("SZ.001309","1m",27,37,'standard-local'),
])
def test_actual_internal_turn_has_the_earliest_complete_context(code, frequency, start, end, rule):
    frame = pd.read_parquet(Path(__file__).parents[1]/"fixtures/segment_local"/f"{code}_{frequency}.parquet")
    cl = CL(code,frequency,{},market="a")
    cl.process_klines(frame,last_bar_closed=True)
    proof = next(p for p in cl.xd_calculator.evidence if p.start_index==start)
    assert proof.end_index==end
    if rule == 'standard-local':
        assert proof.reference_mode == rule
    if code == 'SZ.001309':
        parent=next(p for p in cl.xd_calculator.evidence if p.key[:2]==(12,26))
        assert max(parent.first_sequence[-1].source_indices)==37
        assert proof.end_index>=37 and proof.witness_index==40
    check_evidence(cl.xd_calculator,cl.get_contiguous_bis())
    audit = SearchAuditCalculator(Counter())
    audit.calculate(cl.get_contiguous_bis())
    assert audit.evidence == cl.xd_calculator.evidence
