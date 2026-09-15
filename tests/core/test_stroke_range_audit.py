"""Detect violations of the author's L066 reply without inventing a repair."""

import pytest

from chanlun.core.stroke_audit import audit_bi_ranges
from chanlun.core.types import BI
from tests.core.strict_structure.test_source_stroke_revision import _bars, _calculate


SECONDARY = [(11, 7), (12, 8), (10, 6), (8, 4), (9, 5), (7, 4.5),
             (8.5, 6), (10, 7), (11, 8), (12.5, 9), (11.5, 8)]


@pytest.mark.parametrize("mirror", [False, True])
def test_audit_still_detects_the_old_invalid_stroke_after_selection_is_repaired(mirror):
    bars = _bars(SECONDARY, mirror)
    merged, calc = _calculate(bars)
    fxs = {fx.k.index: fx for fx in calc.fxs}
    # Reconstruct the old implementation's actual invalid BI from physical FXs.
    invalid = BI(fxs[1], fxs[5], "up" if mirror else "down", 0)
    invalid.locked_at = bars[-1].date
    before = (id(invalid.start), id(invalid.end), invalid.is_done(), invalid.locked_at)
    issues = audit_bi_ranges([invalid], merged.cl_klines)
    assert len(issues) == 1
    issue = issues[0]
    assert (issue.bi_index, issue.start_cl_index, issue.end_cl_index) == (0, 1, 5)
    assert issue.done
    if mirror:
        assert (issue.endpoint_high, issue.actual_high) == (95.5, 96)
        assert issue.high_cl_index == 3
        assert issue.high_source_indices == (3,)
    else:
        assert (issue.endpoint_low, issue.actual_low) == (4.5, 4)
        assert issue.low_cl_index == 3
        assert issue.low_source_indices == (3,)
    assert before == (id(invalid.start), id(invalid.end), invalid.is_done(), invalid.locked_at)
    assert [(bi.start.k.index, bi.end.k.index) for bi in calc.bis] == [(3, 9)]
    assert calc.audit_endpoint_ranges() == []


@pytest.mark.parametrize("mirror", [False, True])
def test_range_audit_allows_equal_interior_extremes(mirror):
    values = list(SECONDARY)
    values[5] = (7, 4)
    merged, calc = _calculate(_bars(values, mirror))
    fxs = {fx.k.index: fx for fx in calc.fxs}
    # Audit the interval independently of whether this pair is selected.
    pair = BI(fxs[1], fxs[5], "up" if mirror else "down", 0)
    assert audit_bi_ranges([pair], merged.cl_klines) == []
    assert [(b.start.k.index,b.end.k.index) for b in calc.bis] == [(3, 9)]
    assert calc.audit_endpoint_ranges() == []


@pytest.mark.parametrize("mirror", [False, True])
def test_range_audit_excludes_the_end_fractals_right_shoulder(mirror):
    _, calc = _calculate(_bars(
        [(12, 8), (10, 6), (11, 7), (12, 8), (13, 9), (14, 10), (13, 5)], mirror,
    ))
    assert len(calc.bis) == 1
    assert calc.bis[0].end.k.index == 5
    assert calc.audit_endpoint_ranges() == []


@pytest.mark.parametrize("mirror", [False, True])
def test_range_audit_uses_merged_prices_after_inclusion(mirror):
    bars = _bars(
        [(12, 8), (10, 6), (11, 7), (12, 4), (13, 8), (14, 9), (15, 10), (14, 9)], mirror,
    )
    _, calc = _calculate(bars)
    assert len(calc.bis) == 1
    bi = calc.bis[0]
    if mirror:
        assert bars[3].h > bi.high
    else:
        assert bars[3].l < bi.low
    assert calc.audit_endpoint_ranges() == []


def test_missing_or_misindexed_interval_cannot_pass_the_audit():
    merged, calc = _calculate(_bars(SECONDARY))
    with pytest.raises(ValueError, match="interval is unavailable"):
        audit_bi_ranges(calc.bis, [])
    merged.cl_klines[4].index = 100
    with pytest.raises(ValueError, match="coordinates are inconsistent"):
        calc.audit_endpoint_ranges()
