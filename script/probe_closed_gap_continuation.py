"""Audit-only L065/L071 hypothesis for a qualified ordinary reference.

L065 226-244 defines stroke destruction with d_j <= g_i / g_j >= d_i.
This is entry into the qualified preceding feature interval, not necessarily
passage through its far edge. L071 100-124 supplies the following continuation
condition. Keep the user's existing effective-end convention unchanged.

Only the already qualified record-reference route is varied here. Local and
established non-extreme contexts require their own eligibility review; this
experiment is not a claimed complete implementation or a deployed correction.
"""

from chanlun.core.xd_calculator import (
    XdCalculator,
    _overlap,
    _classify_first_feature_gap,
)
from chanlun.core.segment_evidence import (
    FeatureEvidence,
    FirstPenBreakEvidence,
    FirstPenContinuationEvidence,
    SegmentEvidence,
)


class ClosedGapContinuationProbe(XdCalculator):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.closed_gap_events = []

    def _standalone_first_proof(self, values, first, limit):
        # The production implementation constructs its own calculator here.
        # Apply the same diagnostic policy to independent child certificates.
        if limit < first + 5:
            return None
        progress = self._reverse_feature_progress[first + 3]
        turn = self._reverse_feature_turns[first + 3]
        if progress is None or progress > limit or turn is None or turn > limit:
            return None
        key = (first, limit)
        if key not in self._reverse_probe_cache:
            probe = type(self)()
            probe._feature_scan_cache = {}
            probe._build_segments(
                values[: limit + 1],
                first,
                first_proof_only=True,
                pivot_origins=self._reverse_pivot_origins,
                next_extensions=self._reverse_extensions,
                feature_progress=self._reverse_feature_progress,
                feature_turns=self._reverse_feature_turns,
            )
            self._reverse_probe_cache[key] = (
                probe.evidence[0] if probe.evidence else None
            )
        return self._reverse_probe_cache[key]

    def _first_continuation_proof(
        self,
        values,
        start,
        direction,
        first,
        reference,
        *,
        reference_context="record",
        predecessor=None,
        pivot_start=None,
    ):
        original = super()._first_continuation_proof(
            values,
            start,
            direction,
            first,
            reference,
            reference_context=reference_context,
            predecessor=predecessor,
            pivot_start=pivot_start,
        )
        if (
            original is not None
            or reference_context != "record"
            or predecessor is not None
            or pivot_start is not None
        ):
            return original
        if first < start + 3 or first + 2 >= len(values):
            return None
        raw = values[first]
        up = direction == "up"
        turns = raw.high > reference["high"] if up else raw.low < reference["low"]
        net = (
            raw.start.val > values[start].start.val
            if up
            else raw.start.val < values[start].start.val
        )
        if not (
            turns
            and net
            and _overlap(raw, reference)
            and _overlap(raw, values[first + 2])
        ):
            return None
        strong = raw.low < reference["low"] if up else raw.high > reference["high"]
        if strong:
            return None
        receipt = self._first_pen_break_completion(values, raw, direction)
        if not isinstance(receipt, FirstPenBreakEvidence):
            return None
        # L067's normalized gap classification remains mandatory. Closing the
        # raw gap earlier does not authorize a first-case shortcut after an
        # ordinary middle has become gapped through same-side inclusion.
        middle = {
            "low": receipt.effective_first.low,
            "high": receipt.effective_first.high,
        }
        gap = _classify_first_feature_gap(reference, raw, middle, direction)
        if gap.effective_gap:
            return None
        self.closed_gap_events.append(
            {
                "start": start,
                "first": first,
                "direction": direction,
                "witness": receipt.extension_index,
                "gap_basis": gap.basis,
                "reference": FeatureEvidence.from_element(reference),
            }
        )
        return SegmentEvidence(
            start,
            first - 1,
            direction,
            "first-pen-continuation",
            receipt.extension_index,
            first_pen_continuation=FirstPenContinuationEvidence(
                first,
                first + 2,
                receipt.extension_index,
                FeatureEvidence.from_element(reference),
                "record",
            ),
            first_break_witness_index=receipt.extension_index,
            first_break_evidence=receipt,
        )
