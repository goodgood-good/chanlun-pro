"""Window-origin recovery must not swallow history or backdate confirmation."""

from pathlib import Path

import pandas as pd
import pytest

from chanlun.core.cl import CL
from chanlun.core.xd_calculator import XdCalculator
from script.check_segment_model import check_evidence, signature
from tests.core.test_segment_source_rules import geometry, strokes


@pytest.mark.parametrize("mirror", [False, True])
def test_unestablished_origin_recovers_after_opposite_crossing(mirror):
    values = strokes([0, 10, 2, 8, -1, 6, -3, 5, -2, 7], mirror)
    calc = XdCalculator()
    assert calc._find_strict_start(values[:3]) == 0
    assert calc._find_strict_start(values[:4]) == 1
    calc.calculate(values)
    assert calc.xds[0].start_line.index == 1
    assert calc.evidence[0].origin_evidence.initial_index == 0
    assert calc.evidence[0].origin_evidence.witness_index == 3
    check_evidence(calc, values)


@pytest.mark.parametrize("mirror", [False, True])
@pytest.mark.parametrize("points", [
    [0, 10, 6, 14, -2, 12, -4],
    [12, 32, 14, 26, 18, 30, 15, 24, 8],
    [0, 10, 6, 18, 13, 16, 11, 16, 12, 20],
])
def test_directional_advance_or_existing_proof_protects_the_origin(points, mirror):
    values = strokes(points, mirror)
    calc = XdCalculator()
    assert calc._find_strict_start(values) == 0
    calc.calculate(values)
    assert all(p.origin_evidence is None for p in calc.evidence)


@pytest.mark.parametrize("mirror", [False, True])
def test_origin_recovery_preserves_every_confirmed_prefix(mirror):
    values = strokes([0, 10, 2, 8, 1, 9, -1, 7, -2, 8, 0, 12, 4, 11, -3], mirror)
    calc, previous = XdCalculator(), {}
    for n in range(3, len(values)+1):
        calc.calculate(values[:n])
        current = signature(calc.xds)
        assert all(current.get(k) == t for k,t in previous.items())
        assert all(t == values[n-1].locked_at for k,t in current.items() if k not in previous)
        check_evidence(calc, values[:n])
        previous = current


@pytest.mark.parametrize("code,frequency,expected_start", [
    ("SZ.300867", "5m", 3), ("SH.688377", "5m", 5), ("SZ.300069", "1m", 1),
])
def test_actual_window_origin_does_not_swallow_months(code, frequency, expected_start):
    frame = pd.read_parquet(Path(__file__).parents[1]/"fixtures/segment_origin"/f"{code}_{frequency}.parquet")
    calc = CL(code, frequency, {}, market="a")
    calc.process_klines(frame, last_bar_closed=True)
    lines = calc.get_xds()
    assert lines[0].start_line.index == expected_start
    assert lines[0].end_line.index < expected_start + 35
    assert len(lines) > 20
    check_evidence(calc.xd_calculator, calc.get_contiguous_bis())


def test_unchanged_original_overlap_prefix_still_has_a_preview():
    assert geometry(XdCalculator().calculate(strokes([0, 10, 2, 8]))) == [(0, 3, False)]


@pytest.mark.parametrize("mirror", [False, True])
@pytest.mark.parametrize("pending", [False, True])
def test_discarded_origin_is_still_a_confirmation_dependency(mirror, pending):
    values = strokes([0, 10, 2, 8, -1, 6, -3, 5, -2, 7], mirror)
    if pending:
        values[0].selection_pending = True
    else:
        values[0].locked_at = None
    calc = XdCalculator()
    calc.calculate(values)
    assert calc.evidence[0].origin_evidence.initial_index == 0
    assert all(not x.done and x.locked_at is None for x in calc.xds)
