"""Audit a completed standard reference at the CURRENT pending candidate.

The existing interior search starts at the next candidate. It can therefore
miss a qualified standard proof at the very candidate whose alternate local
context is still pending. Keep first-fractal/F2 and origin-return deadlines.
"""
from chanlun.core.xd_calculator import _CandidateExtension, _GapConfirmationInvalidated
from chanlun.core.segment_evidence import PendingBoundary
from script.probe_closed_gap_continuation import ClosedGapContinuationProbe


class SameCandidateStandardProbe(ClosedGapContinuationProbe):
    def _resolve_first_search_outcome(self, values, start, direction, first, result,
                                      standard, context, reference, pivot_origins=None,
                                      predecessor=None):
        chosen = super()._resolve_first_search_outcome(
            values, start, direction, first, result, standard, context, reference,
            pivot_origins, predecessor,
        )
        retained = None
        if isinstance(chosen, PendingBoundary) and chosen.reason == 'waiting-first-feature':
            limit = len(values) - 1
        elif (isinstance(chosen, PendingBoundary) and chosen.reason == 'waiting-second-feature'
              or isinstance(chosen, _GapConfirmationInvalidated)):
            if not chosen.first_sequence:
                return chosen
            limit = max(chosen.first_sequence[-1].source_indices) - 1
        elif isinstance(chosen, _CandidateExtension):
            limit = chosen.witness_index - 1
        elif isinstance(chosen, tuple):
            retained = self._proofs[(start, chosen[0], direction)]
            limit = (max(retained.first_sequence[-1].source_indices) - 1
                     if retained.second_sequence else retained.witness_index - 1)
        else:
            return chosen
        if first + 2 > limit:
            return chosen
        alternate = self._try_end_standard_local(
            values, start, direction, first, standard, allow_type2=False,
        )
        if isinstance(alternate, tuple):
            proof = self._proofs[(start, alternate[0], direction)]
            if proof.witness_index <= limit:
                return alternate
        # Trying the same physical boundary can overwrite its proof. Retain
        # the actual winner, even when the discarded proof has the same key.
        if retained is not None:
            self._proofs[retained.key] = retained
        return chosen
