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
    for count in (778, 792, 793, 900):
        cd = CL("SZ.002299", "1m", strict_config(), market="a")
        cd.process_klines(frame.head(count))
        line = next(
            xd for xd in cd.get_xds()
            if (xd.start.k.k_index, xd.end.k.k_index) == (387, 444)
        )
        assert line.is_done() is (count >= 793)
        confirmations.append(line.locked_at)
    # The lesson 81 feature rule changes the later cascade witness. Earlier
    # prefixes must remain unconfirmed, instead of borrowing the future lock.
    assert confirmations == [None, None, frame.iloc[792].date, frame.iloc[792].date]


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
        # The source figure establishes geometry; it does not satisfy this
        # implementation's later audit lock buffer.
        assert not first.is_done() and first.locked_at is None


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
