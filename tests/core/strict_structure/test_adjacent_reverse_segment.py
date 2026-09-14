"""A later reverse movement cannot replace an earlier feature-sequence proof."""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from chanlun.core.xd_calculator import XdCalculator
from tests.core.strict_structure.real_history import load_frame, strict_config
from chanlun.core.cl import CL


BASE = datetime(2026, 1, 5, tzinfo=timezone.utc)


def _strokes(points, mirror=False):
    if mirror:
        points = [100 - value for value in points]
    return [
        SimpleNamespace(
            index=i, start=SimpleNamespace(val=a), end=SimpleNamespace(val=b),
            type="up" if b > a else "down", high=max(a, b), low=min(a, b),
            locked_at=BASE + timedelta(minutes=i),
        )
        for i, (a, b) in enumerate(zip(points, points[1:]))
    ]


@pytest.mark.parametrize("mirror", [False, True])
def test_only_immediate_three_strokes_qualify_for_reverse_break_fallback(mirror):
    calc = XdCalculator()
    valid = _strokes([0, 10, 5, 12, -2, 3, -4], mirror)
    direction = valid[0].type
    high, low = (100, 88) if mirror else (12, 0)
    assert calc._try_end_r34(valid, 0, direction, high, low, 3) == (
        2, 3, 5, valid[5].locked_at,
    )
    # The distant break is after a separate, potentially completed movement.
    later = _strokes([0, 10, 5, 12, 8, 11, 7, 10, -2, 3, -4], mirror)
    assert calc._try_end_r34(later, 0, direction, high, low, 3) is None


def test_real_segment_confirmation_survives_a_later_reverse_break():
    frame = load_frame("SZ.002299_1m.parquet", 900)
    confirmations = []
    for count in (489, 778, 792, 793, 900):
        cd = CL("SZ.002299", "1m", strict_config(), market="a")
        cd.process_klines(frame.head(count))
        line = next(
            xd for xd in cd.get_xds()
            if (xd.start.k.k_index, xd.end.k.k_index) == (387, 444)
        )
        assert line.is_done()
        confirmations.append(line.locked_at)
    # The physical feature witness is row 488, not the fourth later segment.
    assert confirmations == [frame.iloc[488].date] * 5
    before = CL("SZ.002299", "1m", strict_config(), market="a")
    before.process_klines(frame.head(488))
    assert not any(xd.is_done() and (xd.start.k.k_index, xd.end.k.k_index) == (387, 444)
                   for xd in before.get_xds())


@pytest.mark.parametrize("mirror", [False, True])
def test_lesson_81_non_extreme_turn_does_not_manufacture_a_feature_gap(mirror):
    # PDF 2036/2038: 3 < 1, 6 < 1, 5 > 7, 8 > 4. The author's replies
    # explicitly use 12, 56, 78; 34 is not a new pivot of the original segment.
    values = _strokes([0, 100, 40, 90, 20, 150, 95, 130, 70, 180], mirror)
    calc = XdCalculator()
    direction = values[0].type
    high, low = (100, -50) if mirror else (150, 0)
    results = [
        calc._try_end(
            values[:count], 0, 4, direction, high, low, 5,
            seg_cs_bis_cache=[values[1], values[3]],
        )
        for count in (8, 9)
    ]
    assert results == [(4, 5, 7, values[7].locked_at)] * 2


@pytest.mark.parametrize("mirror", [False, True])
def test_lesson_81_public_calculation_reaches_the_valid_initial_segment(mirror):
    values = _strokes([0, 100, 40, 90, 20, 150, 95, 130, 70, 180], mirror)
    for count in (8, 9):
        calc = XdCalculator()
        segments = calc.calculate(values[:count])
        assert calc._find_strict_start(values[:count]) == 0
        assert segments
        first = segments[0]
        assert first.start_line is values[0]
        assert first.end_line is values[4]
        assert first.type == values[0].type
        assert first.formed_at == values[7].locked_at
        # Lesson 67/71 requires the actual feature proof, without waiting for
        # four additional completed segments after the proof already exists.
        assert first.is_done() and first.locked_at == values[7].locked_at


@pytest.mark.parametrize("mirror", [False, True])
def test_included_reverse_strokes_do_not_turn_a_pen_break_into_segment_confirmation(mirror):
    # Lesson 78 figure 1: the first reverse stroke breaks the prior stroke,
    # but the following reverse stroke is contained; the old segment extends.
    values = _strokes([0, 10, 6, 15, 11, 20, 14, 18, 16, 21], mirror)
    for count in (7, 8, 9):
        lines = XdCalculator().calculate(values[:count])
        assert not any(line.is_done() for line in lines)


@pytest.mark.parametrize("mirror", [False, True])
def test_non_extreme_boundary_preserves_the_reverse_segment_origin(mirror):
    # Real failure reduced to ticks: the first stroke is the internal extreme.
    # Forcing the first segment to end at 3 strands stroke 3-4, because 3-4
    # and 5-6 do not overlap. The reverse origin must remain valid as well.
    values = _strokes([3860, 3910, 3866, 3877, 3863, 3895, 3880,
                      3891, 3853, 3864, 3848, 3888, 3872, 3905], mirror)
    lines = XdCalculator().calculate(values)
    assert len(lines) >= 2
    for a, b in zip(lines, lines[1:]):
        assert a.end_line.index + 1 == b.start_line.index
        assert a.type != b.type
    for line in lines:
        start = line.start_line.index
        first, third = values[start], values[start + 2]
        assert max(first.low, third.low) <= min(first.high, third.high)


@pytest.mark.parametrize("mirror", [False, True])
def test_observation_origin_uses_only_an_available_overlapping_three_stroke_prefix(mirror):
    values = _strokes([0, 100, 40, 90, 20, 150], mirror)
    calc = XdCalculator()
    assert calc._find_strict_start(values[:2]) == -1
    assert calc._find_strict_start(values[:3]) == 0
    assert calc._find_strict_start(values) == 0
    no_overlap = _strokes([0, 10, -5, -1], mirror)
    assert calc._find_strict_start(no_overlap) == -1
    earlier_extreme_missing = _strokes([20, 100, 0, 90], mirror)
    assert calc._find_strict_start(earlier_extreme_missing) == -1
