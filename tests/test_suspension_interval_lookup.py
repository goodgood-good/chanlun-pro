"""Interval lookups retain exact bar boundaries and all evidence conflicts."""
from copy import deepcopy

import pandas as pd
import pytest

from chanlun.screening import sessions


def fixture(intervals, *, frequency="5m", stamps=None, volumes=None):
    stamps = [900, 1300, 1700, 2000, 2100] if stamps is None else stamps
    frame = pd.DataFrame({"code": "SH.601615", "date": pd.to_datetime(stamps, unit="s", utc=True),
                          "volume": [0.] * len(stamps) if volumes is None else volumes})
    proof = {"schema": sessions.SCHEMA, "symbol": "SH.601615", "source_url": sessions.SOURCE_URL,
             "checked_through": 3000, "intervals": intervals}
    frame.attrs["screening_suspensions"] = {**proof, "revision": sessions._digest(proof)}
    return frame, {"symbol": "SH.601615", "frequency": frequency, "cutoff": 3000}


@pytest.mark.parametrize("frequency", ["1m", "5m", "30m"])
def test_expected_closes_are_not_rescanned_for_every_interval(frequency):
    class CountedSet(set):
        iterations = 0
        def __iter__(self):
            self.iterations += 1
            return super().__iter__()
    intervals = [[i, 2500 - i] for i in range(0, 200, 5)]
    expected = CountedSet(range(0, 3001, 10))
    step = int(frequency[:-1]) * 60
    frame, context = fixture(intervals, frequency=frequency)
    original = frame.copy(deep=True)
    wanted = {at for at in range(0, 3001, 10) if step <= at <= 2500}
    assert sessions.exempt_closes(frame, context, expected) == wanted
    assert expected.iterations <= 1
    pd.testing.assert_frame_equal(frame, original)
    assert frame.attrs == original.attrs


@pytest.mark.parametrize("intervals", [
    [[1000, 2000]], [[1000, 2000], [1000, 2000]],
    [[1500, 2300], [1000, 2000]], [[1000, 1200]], [],
])
def test_overlaps_duplicates_short_and_empty_intervals(intervals):
    frame, context = fixture(intervals)
    expected = {2000, 1000, 1300, 1299, 2001, 2300, 3000}
    wanted = {at for at in expected if any(first <= at - 300 and at <= last for first, last in intervals)}
    assert sessions.exempt_closes(frame, context, expected) == wanted


@pytest.mark.parametrize("stamp,conflicts", [(1000, False), (1299, False), (1300, True), (2000, True), (2001, False)])
@pytest.mark.parametrize("empty_expected", [False, True])
def test_trade_conflicts_keep_both_inclusive_boundaries_even_without_expected_bars(stamp, conflicts, empty_expected):
    frame, context = fixture([[1000, 2000]], stamps=[2100, stamp, 900], volumes=[0., 100., 0.])
    expected = set() if empty_expected else {1300, 2000}
    if conflicts:
        with pytest.raises(ValueError, match="conflicts"):
            sessions.exempt_closes(frame, context, expected)
    else:
        assert sessions.exempt_closes(frame, context, expected) == expected


@pytest.mark.parametrize("pair", [[2000, 1000], [1000, 3001], [1000, True], [1000], "bad"])
def test_invalid_later_interval_is_not_hidden_after_an_earlier_match(pair):
    frame, context = fixture([[1000, 2000], pair])
    with pytest.raises(ValueError, match="interval"):
        sessions.exempt_closes(frame, context, {1300, 2000})


@pytest.mark.parametrize("field,value", [("symbol", "SH.600000"), ("checked_through", 2999), ("source_url", "unknown")])
def test_identity_and_coverage_checks_still_precede_interval_exemptions(field, value):
    frame, context = fixture([[1000, 2000]])
    proof = deepcopy(frame.attrs["screening_suspensions"])
    proof[field] = value
    proof["revision"] = sessions._digest({k:v for k,v in proof.items() if k != "revision"})
    frame.attrs["screening_suspensions"] = proof
    with pytest.raises(ValueError, match="identity"):
        sessions.exempt_closes(frame, context, {1300, 2000})
