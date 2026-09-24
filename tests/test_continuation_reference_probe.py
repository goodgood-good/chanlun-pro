"""Positive controls for an audit experiment, not production-rule acceptance."""

import pytest

from script.probe_all_first_break_routes import FullFirstBreakProbe
from script.probe_continuation_reference_scope import RawReferenceContinuationProbe
from tests.core.test_segment_source_rules import strokes


@pytest.mark.parametrize("mirror", [False, True])
def test_experimental_strict_turn_is_actually_reached(mirror):
    values = strokes([0, 10, 6, 14, 4, 12, 2, 11, 1, 13, 4, 14], mirror)
    direction = "up" if mirror else "down"
    old = FullFirstBreakProbe()
    old.calculate(values)
    predecessor = old.evidence[0]
    assert predecessor.key[:2] == (0, 2)
    assert (
        old._try_established_reverse_break(values, 3, direction, 8, predecessor) is None
    )
    effective = RawReferenceContinuationProbe()
    effective.calculate(values)
    result = effective._try_established_reverse_break(
        values, 3, direction, 8, effective.evidence[0]
    )
    assert isinstance(result, tuple) and result[0] == 7
    event = effective.scope_events[-1]
    assert (
        event["strict_turn_reached"] and event["certificate"] and event["witness"] == 10
    )
    proof = effective._proofs[(3, 7, direction)]
    assert proof.first_pen_continuation.reference_context == "local"
    assert proof.first_pen_continuation.reference.source_indices == (6,)
