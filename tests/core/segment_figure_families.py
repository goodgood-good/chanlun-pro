"""Order families derived from original diagrams, without a feature algorithm.

Original: D:/缠论/chanlun_lesson_corpus/L079_分型的辅助操作与一些问题的再解答(2007-09-10.md,
lines 247-295, PDF pp1991-1992. These are synthetic legal-pen realizations of
explicit diagram constraints, not a reconstruction of market prices. Extra
equalities concern unconstrained pairs; consecutive pen endpoints stay strict.
"""


def lesson79_family(kind, touching_left=False):
    # Edges mean P[a] < P[b]. In particular P1 < P6 ensures the STANDARD
    # [P1,P2] and merged [P3,P6] overlap. Omitting this constraint produces
    # a type-2 problem with a different expected outcome (L067).
    edges = [
        (11, 3),
        (3, 1),
        (1, 2),
        (2, 0),
        (0, 4),
        (3, 5),
        (5, 6),
        (6, 4),
        (3, 7),
        (7, 6),
        (6, 2),
        (1, 6),
        (7, 8),
        (6, 8),
        (8, 4),
        (3, 9),
        (9, 10),
        (10, 8),
    ]
    if kind == "upper":
        edges.append((9, 7))
    elif kind == "lower":
        edges.append((7, 9))
    elif kind != "equal9":
        raise ValueError(kind)

    aliases = {i: i for i in range(12)}
    if kind == "equal9":
        aliases[9] = 7
    if touching_left:
        aliases[6] = 1
    yield from _order_family(12, edges, aliases)


def lesson79_lower_continuation_family(kind, touching_left=False):
    """L079's pending lower/equal-9 figure followed by a strong reverse triple.

    The shifted endpoint must remain above P6. Future P12 is placed between
    P11 and P3; P13 falls below P11. These are stated L071/L079 deductions,
    not an enumeration of every possible future continuation.
    """
    from fractions import Fraction

    if kind not in ("lower", "equal9"):
        raise ValueError(kind)
    for points in lesson79_family(kind, touching_left):
        if points[10] > points[6]:
            yield points + [Fraction(points[11] + points[3], 2), points[11] - 1]


def lesson81_family(kind, second_gap="overlap"):
    """L081 lines 46-73, PDF pp2026-2027: the 5/7 comparison.

    The gap/touch cases additionally lower P7 to/below P4. Those are L067/
    L077 deductions, not literal prices or extra pictures in L081. The fixed
    edges keep the first fractal gapped and each successor's first 3 pens
    overlapping. Other equalities/orderings are enumerated, not assumed.
    """
    edges = [
        (0, 2),
        (2, 1),
        (1, 6),
        (6, 4),
        (4, 5),
        (5, 3),
        (3, 9),
        (6, 8),
        (8, 7),
        (7, 9),
    ]
    aliases = {i: i for i in range(10)}
    if kind == "lower7":
        edges.append((7, 5))
    elif kind == "higher7":
        edges.append((5, 7))
    elif kind == "equal7":
        aliases[7] = 5
    else:
        raise ValueError(kind)
    if second_gap == "gap":
        if kind != "lower7":
            raise ValueError("an own gap requires P7 below P5")
        edges.append((7, 4))
    elif second_gap == "touch":
        if kind != "lower7":
            raise ValueError("touching requires P7 below P5")
        aliases[7] = 4
    elif second_gap == "overlap":
        edges.append((4, 7))
    else:
        raise ValueError(second_gap)
    yield from _order_family(10, edges, aliases)


def _order_family(size, edges, aliases):
    edges = {(aliases[a], aliases[b]) for a, b in edges if aliases[a] != aliases[b]}
    nodes = frozenset(aliases.values())

    def visit(remaining, ranks, rank):
        if not remaining:
            yield [ranks[aliases[i]] for i in range(size)]
            return
        ready = sorted(
            n for n in remaining if all(a not in remaining for a, b in edges if b == n)
        )
        # Any nonempty subset of minimal nodes may share the next value.
        # This enumerates every total preorder satisfying the strict edges,
        # including equality of price pairs not fixed by the diagram rules.
        for mask in range(1, 1 << len(ready)):
            take = {n for i, n in enumerate(ready) if mask & (1 << i)}
            yield from visit(
                remaining - take, {**ranks, **dict.fromkeys(take, rank)}, rank + 1
            )

    yield from visit(nodes, {}, 0)
