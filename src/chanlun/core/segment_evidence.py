"""Immutable construction evidence; intervals never become synthetic pens.

L067 defines the two feature proofs. L071 protects the assumed boundary;
L077-L079 permit endpoints different from internal price extremes. References
to the locally read original and the implementation conventions are in
docs/segment_construction_audit.md.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class PendingBoundary:
    reason: str
    candidate_index: int


@dataclass(frozen=True)
class SegmentTail:
    start_index: int | None
    direction: str | None
    reason: str
    candidate_index: int | None = None


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
class SegmentEvidence:
    start_index: int
    end_index: int
    direction: str
    rule: str
    witness_index: int
    first_sequence: tuple[FeatureEvidence, ...] = ()
    second_sequence: tuple[FeatureEvidence, ...] = ()
    initial_gap: bool = False
    # A parent's second fractal is the successor's completed break (L077).
    # Preserve that context instead of requesting a third feature sequence.
    parent_key: tuple[int, int, str] | None = None
    # L079 lower-figure continuation: a same-direction contained stem can
    # move the provisional pivot while retaining its outside left reference.
    # These are original body pens, never replacement physical pens.
    # Its price suppliers need not coincide with the actual segment endpoint.
    pivot_stem: FeatureEvidence | None = None

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
