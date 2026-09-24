"""Immutable construction evidence; intervals never become synthetic pens.

L067 defines the two feature proofs. L071 protects the assumed boundary;
L077-L079 permit endpoints different from internal price extremes. References
to the locally read original and the implementation conventions are in
docs/segment_rules.md.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class PendingBoundary:
    reason: str
    candidate_index: int
    # F2 priority starts when this FIRST fractal exists, not at the older
    # provisional pivot. Keep its sources so earlier completions can be checked.
    first_sequence: tuple["FeatureEvidence", ...] = ()


@dataclass(frozen=True)
class SegmentTail:
    start_index: int | None
    direction: str | None
    reason: str
    candidate_index: int | None = None
    # Display state is not a boundary proof and never confirms a segment.
    projection_index: int | None = None
    projection_reason: str | None = None
    observed_end_index: int | None = None
    # A predecessor's direct first-case proof can establish a normalized
    # continuation spanning multiple physical pens. A preview must retain it.
    minimum_end_index: int | None = None


@dataclass(frozen=True)
class SourcePens:
    """Persistent concatenation of pen sources, with no quadratic list copies."""

    left: object
    right: object
    count: int

    @classmethod
    def join(cls, left, right):
        return cls(left, right, len(left) + len(right))

    def __len__(self):
        return self.count

    def __iter__(self):
        stack = [self]
        while stack:
            item = stack.pop()
            if isinstance(item, SourcePens):
                stack.extend((item.right, item.left))
            else:
                yield from item


@dataclass(frozen=True)
class FeatureEvidence:
    low: object
    high: object
    source_indices: tuple[int, ...]
    low_source_index: int
    high_source_index: int

    @classmethod
    def from_element(cls, element):
        sources = element.get("merged_bis", [element["bi"]])
        # Exact ties retain the earlier price source. For a middle feature,
        # that source supplies its boundary under the declared convention.
        # A pivot_stem instead explains the left reference: its earlier price
        # source must not override a later middle feature's actual boundary.
        low = element.get("_low_source_index")
        high = element.get("_high_source_index")
        if low is None:
            low = next(b.index for b in sources if b.low == element["low"])
        if high is None:
            high = next(b.index for b in sources if b.high == element["high"])
        return cls(
            element["low"], element["high"], tuple(b.index for b in sources), low, high
        )


@dataclass(frozen=True)
class FeatureGapEvidence:
    """Keep the raw reversal and standard gap separate from the chosen rule.

    The boundary exception is limited to a first reversal covering the left
    reference. Ordinary overlap uses the standard middle (L067); protection
    of that covering reversal is the L071/L078 deduction constrained by the
    reviewed strong-break figure. L075 includes a shared edge in containment.
    """

    raw_first: FeatureEvidence
    raw_gap: bool
    standard_gap: bool
    basis: str

    @property
    def effective_gap(self):
        return self.raw_gap if self.basis == "protected-first-reversal" else self.standard_gap


@dataclass(frozen=True)
class ObservationOriginEvidence:
    """The first provisional observation origin failed before any segment proof.

    These are selection facts, not a synthetic pen or an author-defined extra
    segment. The first accepted proof must wait for the selection witness.
    """

    initial_index: int
    selected_index: int
    witness_index: int


@dataclass(frozen=True)
class SecondFeatureBreakEvidence:
    """L071's raw first-break decision behind a normalized second fractal.

    Inclusion can hide the first physical pen's opposite extreme. Retain the
    candidate fractal and the observed exit from that physical pen, including
    rejected turns. This is a construction deduction from L071/L077/L078,
    not a request for a third feature sequence.
    """

    fractal: tuple[FeatureEvidence, ...]
    first_pen_index: int
    reference_index: int
    fractal_witness_index: int
    outcome: str
    outcome_witness_index: int


@dataclass(frozen=True)
class FirstPenBreakEvidence:
    """User-selected normalized continuation boundary (2026-09-21).

    Keep the physical first pen and the effective same-side interval used
    immediately BEFORE the extending pen. Price suppliers and all merged pen
    indices are retained; original BI geometry is never rewritten.
    """

    first_pen_index: int
    effective_first: FeatureEvidence
    extension_index: int


@dataclass(frozen=True)
class DirectFirstBreakEvidence:
    """L071 100-124 first-pen continuation, distinct from a normal fractal.

    The raw first pen must break the reference and its first/third pens must
    overlap. The effective ending edge follows chronological same-side
    inclusion under the user's selected rule; SegmentEvidence retains it.
    L078's established predecessor is retained as a separate prerequisite.
    Indices refer to original physical pens, never to an adjusted interval.
    """

    first_pen_index: int
    reference_index: int
    overlap_third_index: int
    extension_index: int
    predecessor_key: tuple[int, int, str]


@dataclass(frozen=True)
class ReverseSegmentEvidence:
    """L078 58-67: a complete reverse segment transmits its break backwards.

    The successor is proved from its fixed physical origin WITHOUT assuming
    the parent boundary. It must be emitted unchanged immediately after the
    parent. This is distinct from both a first-pen extension and an F2 fractal.
    """

    first_pen_index: int
    reference_index: int
    overlap_third_index: int
    predecessor_key: tuple[int, int, str]
    successor: "SegmentEvidence"


@dataclass(frozen=True)
class ReturnSegmentEvidence:
    """L078 160-211: a complete return fails to cross the breaking pen origin.

    Unlike ReverseSegmentEvidence, this fixed-origin return is an internal
    witness in the original direction, not the next emitted reverse segment.
    No supporting proof may assume the boundary it is being used to establish.
    Include an inherited F2 successor when needed to validate its own chain.
    """

    first_pen_index: int
    reference_index: int
    overlap_third_index: int
    predecessor_key: tuple[int, int, str]
    return_proofs: tuple["SegmentEvidence", ...]


@dataclass(frozen=True)
class FirstPenContinuationEvidence:
    """First-break continuation, including an equal-origin contained exit.

    This certificate is not a three-element feature fractal. Retain the
    eligible pre-boundary reference and physical first/third/extension pens.
    The enclosing SegmentEvidence records the effective interval, so later
    inclusion cannot erase a completion already witnessed at that boundary.
    """

    first_pen_index: int
    third_pen_index: int
    extension_index: int
    reference: FeatureEvidence
    reference_context: str


@dataclass(frozen=True)
class SegmentEvidence:
    start_index: int
    end_index: int
    direction: str
    rule: str
    witness_index: int
    first_sequence: tuple[FeatureEvidence, ...] = ()
    second_sequence: tuple[FeatureEvidence, ...] = ()
    # Legacy public name: the effective first/second-case classification.
    # gap_context separately preserves the raw and normalized interval facts.
    initial_gap: bool = False
    # A parent's second fractal is the successor's completed break (L077).
    # Preserve that context instead of requesting a third feature sequence.
    parent_key: tuple[int, int, str] | None = None
    # L079 lower-figure continuation: a same-direction contained stem can
    # move the provisional pivot while retaining its outside left reference.
    # These are original body pens, never replacement physical pens.
    # Its price suppliers need not coincide with the actual segment endpoint.
    pivot_stem: FeatureEvidence | None = None
    # Inherited second fractals keep their parent's context, without applying
    # the ordinary first-reversal exception or requesting a third sequence.
    gap_context: FeatureGapEvidence | None = None
    origin_evidence: ObservationOriginEvidence | None = None
    reference_mode: str = "record"
    second_sequence_breaks: tuple[SecondFeatureBreakEvidence, ...] = ()
    first_break_witness_index: int | None = None
    # L078 133-211: reverse continuation can complete an already-established
    # segment. This is distinct from inheriting a parent's second fractal.
    predecessor_key: tuple[int, int, str] | None = None
    # A nonordinary three-element context is not called a top/bottom fractal.
    # It needs this independently checkable raw first-break certificate.
    direct_first_break: DirectFirstBreakEvidence | None = None
    reverse_segment: ReverseSegmentEvidence | None = None
    return_segment: ReturnSegmentEvidence | None = None
    first_pen_continuation: FirstPenContinuationEvidence | None = None
    first_break_evidence: FirstPenBreakEvidence | None = None

    @property
    def key(self):
        return self.start_index, self.end_index, self.direction


def confirmation_time(strokes, start, end):
    """All original pens in the connected proof must provide closed evidence.

    The last pen by position need not have the latest confirmation time. An
    unlocked auxiliary/contained pen prevents confirmation just like a body pen.
    """
    times = []
    for i in range(start, end + 1):
        stroke = strokes[i]
        when = getattr(stroke, "locked_at", None)
        if when is None or getattr(stroke, "selection_pending", False):
            return None
        times.append(when)
    return max(times) if times else None
