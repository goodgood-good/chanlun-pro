"""Diagnostic ONLY: expose strict-turn raw-reference alternatives.

The historical FullFirstBreakProbe removed an outer strict-turn restriction
but called _first_continuation_proof with context='established', which rejects
that same turn again. Its apparent broader coverage therefore was not broader.

This experiment uses the existing LOCAL strict-turn certificate instead. It
deliberately tests alternative reference eligibility, not merely optimization.
Differences must be reviewed against the local original material; passing a
certificate checker does not establish that its reference is globally eligible.
Production selection and all post-boundary inclusion rules stay untouched.
"""
from chanlun.core.xd_calculator import XdCalculator, _bi_to_cs_elem


class RawReferenceContinuationProbe(XdCalculator):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.scope_events = []

    def _try_established_reverse_break(self, values, start, direction, check, predecessor):
        result = super()._try_established_reverse_break(values, start, direction, check, predecessor)
        if isinstance(result, tuple):
            return result
        if predecessor is None or predecessor.end_index + 1 != start or check < start + 3 or check + 2 >= len(values):
            return result
        first, left = values[check], values[check - 2]
        strong = first.low < left.low if direction == 'up' else first.high > left.high
        turns = first.high > left.high if direction == 'up' else first.low < left.low
        if not (strong and turns):
            return result
        # LOCAL is the existing declared context for a strict turn. Giving it
        # priority here is the hypothesis being tested, not an accepted rule.
        proof = self._first_continuation_proof(
            values, start, direction, check, _bi_to_cs_elem(left), reference_context='local',
        )
        self.scope_events.append({'start': start, 'candidate': check, 'direction': direction,
                                  'predecessor_key': predecessor.key,
                                  'strict_turn_reached': True,
                                  'certificate': proof is not None,
                                  'witness': proof.witness_index if proof is not None else None})
        return self._commit_first_continuation(values, proof) if proof is not None else result
