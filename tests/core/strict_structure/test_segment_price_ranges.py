"""Lesson 78 segment ranges must retain internal stroke extrema."""

from datetime import timedelta

import pytest

from chanlun.core.strict_structure.models import SourceKind
from chanlun.core.strict_structure.unit_adapter import UnitLockRegistry, adapt_lines
from tests.core.strict_structure.helpers import BASE
from tests.core.strict_structure.test_unit_adapter import FakeLine


def segment_with_strokes(values):
    strokes = [
        FakeLine(i, "up" if b > a else "down", a, b, done=True)
        for i, (a, b) in enumerate(zip(values, values[1:]))
    ]
    for i, stroke in enumerate(strokes):
        stroke.index = 20 + i
    segment = FakeLine(0, strokes[0].type, values[0], values[-1], done=True)
    segment.start = strokes[0].start
    segment.end = strokes[-1].end
    segment.start_line = strokes[0]
    segment.end_line = strokes[-1]
    segment.formed_at = segment.end.k.date + timedelta(minutes=5)
    segment.locked_at = segment.formed_at + timedelta(minutes=5)
    return segment, strokes


def adapt_segment(segment, strokes):
    return adapt_lines(
        (segment,),
        0,
        SourceKind.SEGMENT,
        "1",
        BASE + timedelta(hours=3),
        UnitLockRegistry("test-raw"),
        constituent_lines=strokes,
    )[0]


@pytest.mark.parametrize("mirror", [False, True])
@pytest.mark.parametrize("prices", [(140, 110, 130, 100, 150, 90), (140, 90, 125, 110)])
def test_actual_range_and_time_survive_an_internal_segment_extreme(prices, mirror):
    prices = tuple(300 - value for value in prices) if mirror else prices
    segment, strokes = segment_with_strokes(prices)
    unit = adapt_segment(segment, strokes)

    assert (unit.start_tick, unit.end_tick) == (prices[0], prices[-1])
    assert (unit.low_tick, unit.high_tick) == (min(prices), max(prices))
    assert unit.market_start == segment.start.k.date
    assert unit.market_end == segment.end.k.date
    for side, extreme in (("low", min(prices)), ("high", max(prices))):
        offset = max(i for i, value in enumerate(prices) if value == extreme)
        assert unit.extreme_market_time(side) == BASE + timedelta(minutes=offset * 5)
    assert unit.confirmed_at == segment.locked_at
    assert unit.formed_at == segment.formed_at
    assert unit.available_at >= unit.confirmed_at > unit.market_end


def test_equal_internal_extremes_keep_the_last_price_witness_not_the_segment_end():
    segment, strokes = segment_with_strokes((140, 90, 125, 90, 130, 110))
    unit = adapt_segment(segment, strokes)
    assert unit.extreme_market_time("low") == strokes[2].end.k.date
    assert unit.extreme_market_time("low") < unit.market_end < unit.confirmed_at


@pytest.mark.parametrize(
    "broken", ["absent", "gap", "discontinuous", "wrong_end", "duplicate"]
)
def test_declared_segment_strokes_require_complete_source_evidence(broken):
    segment, strokes = segment_with_strokes((140, 90, 125, 110))
    if broken == "absent":
        strokes = None
    elif broken == "gap":
        strokes = (strokes[0], strokes[2])
    elif broken == "discontinuous":
        strokes[1].start.val += 1
    elif broken == "wrong_end":
        segment.end_line = strokes[1]
    else:
        strokes.append(strokes[0])
    with pytest.raises(ValueError, match="segment|constituent"):
        adapt_segment(segment, strokes)


def test_internal_price_evidence_changes_identity_with_identical_line_endpoints():
    segment, strokes = segment_with_strokes((140, 90, 125, 110))
    first = adapt_segment(segment, strokes)
    strokes[0].end.val = 89
    strokes[1].start.val = 89
    second = adapt_segment(segment, strokes)
    assert first.start_tick == second.start_tick
    assert first.end_tick == second.end_tick
    assert first.unit_id != second.unit_id
