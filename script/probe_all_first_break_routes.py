"""Diagnostic extension: test the sufficient first-break route even when a
raw neighbour makes it look like an ordinary strict turn. The caller still
preserves second-case priority and earliest-witness deadlines.
"""

from script.probe_normalized_completion_coverage import CompletionCoverageProbe
from chanlun.core.xd_calculator import _bi_to_cs_elem, _overlap


class FullFirstBreakProbe(CompletionCoverageProbe):
    def _try_established_reverse_break(
        self, values, start, direction, check, predecessor
    ):
        result = super()._try_established_reverse_break(
            values, start, direction, check, predecessor
        )
        if isinstance(result, tuple):
            return result
        if (
            predecessor.end_index + 1 != start
            or check < start + 3
            or check + 2 >= len(values)
        ):
            return result
        first, left = values[check], values[check - 2]
        strong = first.low < left.low if direction == "up" else first.high > left.high
        net = (
            first.start.val > values[start].start.val
            if direction == "up"
            else first.start.val < values[start].start.val
        )
        if not (strong and net and _overlap(first, values[check + 2])):
            return result
        proof = self._first_continuation_proof(
            values,
            start,
            direction,
            check,
            _bi_to_cs_elem(left),
            reference_context="established",
            predecessor=predecessor,
        )
        return (
            self._commit_first_continuation(values, proof)
            if proof is not None
            else result
        )
