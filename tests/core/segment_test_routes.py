"""Isolate supporting certificate routes without changing production policy."""
from chanlun.core.xd_calculator import XdCalculator
from chanlun.core.segment_evidence import FirstPenBreakEvidence, PendingBoundary


class WithoutEffectiveOnlyCompletion(XdCalculator):
    """Suppress the new earlier route only to exercise return/reverse proofs.

    This is not an alternative default algorithm. Production uses the new
    effective interval; separate production regressions verify it wins when
    earlier or simultaneous. Supporting certificates still require tests of
    source ownership and circularity when that earlier route is unavailable.
    """
    def _first_pen_break_completion(self, values, first, direction):
        result = super()._first_pen_break_completion(values, first, direction)
        if isinstance(result, FirstPenBreakEvidence):
            end = values[result.extension_index].end.val
            physical_exit = end < first.end.val if direction == 'up' else end > first.end.val
            if not physical_exit:
                return PendingBoundary('waiting-first-feature', first.index - 1)
        return result
