"""Independently search alternatives through the actual confirmation horizon.

The old body-only scan stopped at the drawn endpoint. A first or second
fractal can finish beyond that endpoint, so an alternative must be evaluated
through the pens actually available at confirmation. Candidates are questions
until reference ownership, prior hypotheses and real clocks are checked.
"""

from collections import Counter

from script.review_segment_geometry import standard_append
from script.review_segment_gapped_candidates import second_outcome
from script.explain_interior_segment_candidates import (
    protected_suffix,
    unfinished_second,
)
from script.check_segment_candidates import local_first_fractal, check_gap_invalidation
from script.check_segment_model import pen


def full_standard_candidates(pens, start, horizon, up):
    standard = []
    for i in range(start + 1, horizon + 1, 2):
        p = pens[i]
        lo, hi = sorted((p["start_tick"], p["end_tick"]))
        if not standard_append(standard, (lo, hi, (i,), i, i), up) or len(standard) < 3:
            continue
        a, m, z = standard[-3:]
        turn = (
            m[0] > max(a[0], z[0]) and m[1] > max(a[1], z[1])
            if up
            else m[0] < min(a[0], z[0]) and m[1] < min(a[1], z[1])
        )
        if not turn:
            continue
        pivot = m[4 if up else 3]
        if pivot < start + 3:
            continue
        yield {
            "family": "standard",
            "pivot": pivot,
            "end_pen": pivot - 1,
            "first_witness": i,
            "first_sequence": [a, m, z],
            "has_gap": max(a[0], m[0]) > min(a[1], m[1]),
        }


def closed_prefix_horizon(pens, when):
    horizon = -1
    for i, p in enumerate(pens):
        if (
            not p.get("locked")
            or p.get("selection_pending")
            or p.get("confirmed_at") is None
        ):
            break
        if max(p["confirmed_at"], p.get("selected_at") or 0) > when:
            break
        horizon = i
    return horizon


def inspect_row(row, component, actual_pens, previous=None):
    pens = actual_pens
    up = row["direction"] == "up"
    when = row.get("saved_confirmed_at")
    horizon = closed_prefix_horizon(pens, when) if row["confirmed"] else len(pens) - 1
    expected_witness = (row.get("proof") or {}).get("witness_index")
    errors = []
    if row["confirmed"] and (expected_witness is None or expected_witness > horizon):
        errors.append("native_confirmation_uses_unavailable_pen")
    start, native_end = row["start_pen"], row["end_pen"]
    if horizon < start + 2:
        return {
            "number": row["number"],
            "horizon": horizon,
            "errors": errors,
            "candidates": [],
            "counts": {},
        }
    values = [pen(i, p["start_tick"], p["end_tick"]) for i, p in enumerate(pens)]
    candidates = list(full_standard_candidates(pens, start, horizon, up))
    last_end = native_end - 1 if row["confirmed"] else horizon - 1
    for pivot in range(start + 3, min(last_end + 2, horizon - 1), 2):
        local = local_first_fractal(values, start, pivot, horizon)
        if local is None:
            continue
        candidates.append(
            {
                "family": "local_first_break",
                "pivot": pivot,
                "end_pen": pivot - 1,
                "first_witness": local.witness,
                "first_sequence": [local.left, local.middle, local.right],
                "has_gap": local.has_gap,
                "pivot_sources": list(local.pivot_sources),
            }
        )
    output = []
    seen = set()
    for c in candidates:
        if c["end_pen"] > last_end or c["end_pen"] < start + 2:
            continue
        identity = (c["family"], c["end_pen"], c["first_witness"])
        if identity in seen:
            continue
        seen.add(identity)
        pivot = c["pivot"]
        endpoint = pens[c["end_pen"]]["end_tick"]
        suffix = protected_suffix(pens, pivot, c["first_witness"], up)
        same = suffix["status"] == "independent_right"
        if c["family"] == "standard" and same:
            m, z = c["first_sequence"][1:]
            same = suffix["middle"] == [m[0], m[1], list(m[2])] and suffix["right"] == [
                z[0],
                z[1],
                list(z[2]),
            ]
        outcome = (
            second_outcome(pens, pivot, horizon, up)
            if c["has_gap"]
            else {"status": "not_required"}
        )
        witness = max(c["first_witness"], outcome.get("witness", -1))
        dependencies = pens[start : witness + 1]
        closed = bool(dependencies) and all(
            p.get("locked")
            and not p.get("selection_pending")
            and p.get("confirmed_at") is not None
            for p in dependencies
        )
        available = (
            max(
                (
                    max(p["confirmed_at"], p.get("selected_at") or 0)
                    for p in dependencies
                ),
                default=0,
            )
            if closed
            else None
        )
        c.update(
            second_outcome=outcome,
            same_side_sequence=suffix,
            own_boundary_matches=same,
            witness=witness,
            dependencies_closed=closed,
            dependencies_available_at=available,
            first_witness_after_native_endpoint=c["first_witness"] > native_end,
            second_witness_after_native_endpoint=outcome.get("witness", -1)
            > native_end,
            candidate_after_native_projection=c["end_pen"] >= native_end,
        )
        reason, context = None, []
        if not (endpoint > row["start_tick"] if up else endpoint < row["start_tick"]):
            reason = "candidate_net_direction_invalid"
        elif not same:
            reason = "boundary_side_inclusion_changes_claimed_fractal"
        elif c["has_gap"] and outcome["status"] != "second_fractal_formed":
            reason = outcome["status"]
        elif not closed or row["confirmed"] and available > when:
            reason = "candidate_evidence_not_closed_at_observation"
        elif previous is not None and not previous["confirmed"]:
            reason = "previous_segment_not_confirmed"
        elif (row.get("proof") or {}).get("parent_key"):
            parent = (previous or {}).get("proof") or {}
            linked = (
                previous is not None
                and parent.get("second_sequence")
                and [
                    parent.get("start_index"),
                    parent.get("end_index"),
                    parent.get("direction"),
                ]
                == row["proof"]["parent_key"]
                and parent["second_sequence"] == row["proof"].get("first_sequence")
            )
            reason = (
                "governed_by_parent_second_sequence"
                if linked
                else "needs_individual_reference_and_priority_review"
            )
            context = [
                {
                    "parent_key": row["proof"]["parent_key"],
                    "parent_second_sequence": parent.get("second_sequence"),
                    "parent_link_checked": bool(linked),
                }
            ]
        else:
            for event in component.get("decisions", []):
                if event["start"] != start or event["check"] > pivot:
                    continue
                stop = event.get("witness_index")
                if stop is not None and pivot <= stop <= horizon:
                    try:
                        if event["kind"] == "_GapConfirmationInvalidated":
                            check_gap_invalidation(
                                values, event["candidate_index"], row["direction"], stop
                            )
                        elif event["kind"] == "_CandidateExtension":
                            assert (
                                protected_suffix(pens, event["check"], stop, up)[
                                    "status"
                                ]
                                == "old_direction_extension"
                            )
                        else:
                            continue
                        context.append(
                            {"event": event, "independently_reconstructed": True}
                        )
                    except AssertionError:
                        continue
                if (
                    event["kind"] == "PendingBoundary"
                    and event.get("reason") == "waiting-second-feature"
                    and event["candidate_index"] <= c["end_pen"]
                ):
                    replay = unfinished_second(
                        pens[: horizon + 1], event["candidate_index"] + 1, up
                    )
                    if replay["status"] == "still_waiting_for_second_fractal":
                        context.append(
                            {
                                "event": event,
                                "independently_reconstructed": True,
                                "unfinished_second": replay,
                            }
                        )
            if context:
                reason = "earlier_hypothesis_controls_this_candidate"
            elif (
                not row["confirmed"]
                and row.get("proof")
                and row["end_pen"] < c["end_pen"]
            ):
                first_last = max(
                    i
                    for e in row["proof"]["first_sequence"]
                    for i in e["source_indices"]
                )
                first_dependencies = pens[start : first_last + 1]
                first_ready = all(
                    p.get("locked")
                    and p.get("confirmed_at") is not None
                    and max(p["confirmed_at"], p.get("selected_at") or 0) <= available
                    for p in first_dependencies
                )
                reason = (
                    "earlier_native_geometric_boundary_awaits_locked_pens"
                    if first_ready
                    else "needs_individual_reference_and_priority_review"
                )
                context = [
                    {
                        "earlier_boundary": row["end_pen"],
                        "witness": row["proof"]["witness_index"],
                        "earlier_first_fractal_available_before_candidate": first_ready,
                    }
                ]
            else:
                reason = "needs_individual_reference_and_priority_review"
        c.update(resolution=reason, context=context)
        output.append(c)
    return {
        "number": row["number"],
        "horizon": horizon,
        "native_witness": expected_witness,
        "errors": errors,
        "candidates": output,
        "counts": dict(Counter(c["resolution"] for c in output)),
    }
