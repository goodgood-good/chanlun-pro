"""Expose exact raw pen membership of physical segment units without promotion.

L093:259-277 distinguishes a pen rebound from a broken segment. L094:190-214
and L095:217-250 then distinguish a segment-class center from a later formal
one-minute center. This sidecar preserves raw membership; it does not decide
that any class center and formal center have the same structural identity.
"""

from __future__ import annotations

from datetime import datetime

from chanlun.core.strict_structure.identity import stable_structure_id
from chanlun.core.strict_structure.models import ConstituentUnit, SourceKind


def observe_physical_segment_stroke_lineage(
    segment_lines,
    segment_units,
    stroke_lines,
    stroke_units,
) -> tuple[dict, ...]:
    """Return non-trading pen ID spans for the original physical segments.

    Both unit streams must be built from the same raw lines, cutoff and price
    basis. A synthetic segment without its raw pen indices remains pending.
    The observation neither modifies ConstituentUnit.child_ids nor certifies
    a formal center, type, divergence or buy/sell point.
    """

    raw_segments, segments = tuple(segment_lines), tuple(segment_units)
    raw_strokes, strokes = tuple(stroke_lines), tuple(stroke_units)
    if len(raw_segments) != len(segments) or len(raw_strokes) != len(strokes):
        raise ValueError("physical lineage requires one adapted unit per source line")
    if any(not isinstance(unit, ConstituentUnit) for unit in (*segments, *strokes)):
        raise TypeError("physical lineage requires constituent units")
    if any(unit.source_kind is not SourceKind.SEGMENT for unit in segments):
        raise ValueError("physical segment lineage received another source kind")
    if any(unit.source_kind is not SourceKind.STROKE_OBSERVATION for unit in strokes):
        raise ValueError("physical stroke lineage received another source kind")
    bases = {unit.price_basis_revision for unit in (*segments, *strokes)}
    if len(bases) > 1:
        raise ValueError("physical lineage cannot cross price bases")
    by_index = {}
    for line, unit in zip(raw_strokes, strokes):
        index = getattr(line, "index", None)
        if type(index) is not int or index in by_index:
            raise ValueError("stroke indices must be unique integers")
        if (
            line.start.k.date != unit.market_start
            or line.end.k.date != unit.market_end
            or line.type != unit.direction
        ):
            raise ValueError("stroke unit does not match its physical source")
        by_index[index] = unit

    output = []
    for line, unit in zip(raw_segments, segments):
        if (
            line.start.k.date != unit.market_start
            or line.end.k.date != unit.market_end
            or line.type != unit.direction
        ):
            raise ValueError("segment unit does not match its physical source")
        start_line = getattr(line, "start_line", None)
        end_line = getattr(line, "end_line", None)
        if start_line is None or end_line is None:
            output.append({
                "segment_unit_id": unit.unit_id,
                "segment_available_at": unit.available_at,
                "source_start_stroke_index": None,
                "source_end_stroke_index": None,
                "stroke_unit_ids": (),
                "latest_stroke_available_at": None,
                "status": "pending_raw_stroke_indices",
                "formal_center_identity_claim": False,
                "tradable": False,
            })
            continue
        first, last = getattr(start_line, "index", None), getattr(end_line, "index", None)
        if type(first) is not int or type(last) is not int or first > last:
            raise ValueError("segment stroke indices must be ordered integers")
        try:
            children = tuple(by_index[index] for index in range(first, last + 1))
        except KeyError as exc:
            raise ValueError("segment stroke membership has a source gap") from exc
        if (
            children[0].market_start != unit.market_start
            or children[-1].market_end != unit.market_end
            or children[0].start_tick != unit.start_tick
            or children[-1].end_tick != unit.end_tick
            or min(child.low_tick for child in children) != unit.low_tick
            or max(child.high_tick for child in children) != unit.high_tick
            or any(left.market_end != right.market_start
                   for left, right in zip(children, children[1:]))
            or len({child.unit_id for child in children}) != len(children)
        ):
            raise ValueError("segment stroke membership is not a contiguous physical chain")
        output.append({
            "segment_unit_id": unit.unit_id,
            "segment_available_at": unit.available_at,
            "source_start_stroke_index": first,
            "source_end_stroke_index": last,
            "stroke_unit_ids": tuple(child.unit_id for child in children),
            "latest_stroke_available_at": max(child.available_at for child in children),
            "status": "exact_raw_stroke_membership",
            "formal_center_identity_claim": False,
            "tradable": False,
        })
    return tuple(output)


def observe_class_formal_component_candidates(
    stroke_centers,
    formal_centers,
    segment_memberships,
    *,
    as_of: datetime,
) -> dict:
    """Find prior class-center core pens inside later formal-center bodies.

    This is a many-to-many component observation. Even complete pen overlap
    and price-zone overlap do not prove the same center identity or permit a
    class center to become a formal signal. Missing segment membership leaves
    the affected formal center pending instead of guessing a relationship.
    """

    if (not isinstance(as_of, datetime) or as_of.tzinfo is None
            or as_of.utcoffset() is None):
        raise ValueError("class/formal correspondence requires an aware cutoff")
    observed = tuple(center for center in stroke_centers if center.available_at <= as_of)
    formal = tuple(center for center in formal_centers if center.available_at <= as_of)
    if any(center.source_kind is not SourceKind.STROKE_OBSERVATION
           for center in observed):
        raise ValueError("class-center candidates require pen observation sources")
    if any(center.source_kind is not SourceKind.SEGMENT for center in formal):
        raise ValueError("formal-center candidates require physical segment sources")
    if any(center.structural_level != 0 for center in (*observed, *formal)):
        raise ValueError("class/formal component pairing is only the physical base grade")
    if len({center.center_id for center in observed}) != len(observed) or len({
        center.center_id for center in formal
    }) != len(formal):
        raise ValueError("center identities must be unique within each source track")
    if len({center.price_basis_revision for center in (*observed, *formal)}) > 1:
        raise ValueError("class/formal pairing cannot cross price bases")
    by_segment = {}
    for item in segment_memberships:
        identifier = item["segment_unit_id"]
        if identifier in by_segment:
            raise ValueError("duplicate physical segment membership")
        if item["status"] == "exact_raw_stroke_membership":
            members = tuple(item["stroke_unit_ids"])
            if not members or len(set(members)) != len(members):
                raise ValueError("exact segment membership needs unique pen IDs")
            by_segment[identifier] = frozenset(members)
        else:
            by_segment[identifier] = None

    formal_by_id = {}
    formal_pen_ids = {}
    missing_formal = []
    pen_to_formal = {}
    for center in formal:
        segment_ids = tuple(unit.unit_id for unit in center.body_units)
        if any(by_segment.get(identifier) is None for identifier in segment_ids):
            missing_formal.append(center.center_id)
            continue
        pen_ids = frozenset().union(*(by_segment[identifier]
                                      for identifier in segment_ids))
        formal_by_id[center.center_id] = center
        formal_pen_ids[center.center_id] = pen_ids
        for identifier in pen_ids:
            pen_to_formal.setdefault(identifier, set()).add(center.center_id)

    pairs = []
    later_observation_overlap_count = 0
    for class_center in observed:
        core_ids = tuple(unit.unit_id for unit in class_center.core_units)
        body_ids = tuple(unit.unit_id for unit in class_center.body_units)
        if len(set(core_ids)) != len(core_ids):
            raise ValueError("class center core has duplicate pen IDs")
        matches = set.intersection(*(pen_to_formal.get(identifier, set())
                                     for identifier in core_ids))
        for formal_id in matches:
            candidate = formal_by_id[formal_id]
            if class_center.available_at > candidate.available_at:
                later_observation_overlap_count += 1
                continue
            price_overlap = max(class_center.zd_tick, candidate.zd_tick) <= min(
                class_center.zg_tick, candidate.zg_tick
            )
            pairs.append({
                "candidate_id": stable_structure_id(
                    "u194-class-formal-component-candidate-v1",
                    class_center.price_basis_revision,
                    class_center.center_id, class_center.body_revision,
                    candidate.center_id, candidate.body_revision,
                ),
                "class_center_id": class_center.center_id,
                "formal_center_id": candidate.center_id,
                "class_core_pen_unit_ids": core_ids,
                "class_body_pen_unit_ids": body_ids,
                "formal_body_segment_unit_ids": tuple(
                    unit.unit_id for unit in candidate.body_units
                ),
                "all_class_body_pens_contained": set(body_ids) <= formal_pen_ids[formal_id],
                "formal_body_pen_count": len(formal_pen_ids[formal_id]),
                "class_core_pen_count": len(core_ids),
                "class_core_zone": (class_center.zd_tick, class_center.zg_tick),
                "formal_core_zone": (candidate.zd_tick, candidate.zg_tick),
                "price_core_overlap": price_overlap,
                "class_available_at": class_center.available_at,
                "formal_available_at": candidate.available_at,
                "status": (
                    "component_and_price_candidate" if price_overlap
                    else "component_only_price_disjoint"
                ),
                "same_center_identity_proven": False,
                "formal_point_eligibility_changed": False,
                "tradable": False,
            })
    counts = {}
    for pair in pairs:
        if pair["price_core_overlap"]:
            identifier = pair["formal_center_id"]
            counts[identifier] = counts.get(identifier, 0) + 1
    for pair in pairs:
        pair["price_overlap_candidate_count_for_formal"] = counts.get(
            pair["formal_center_id"], 0
        )
    pairs.sort(key=lambda item: (
        item["formal_available_at"], item["formal_center_id"],
        item["class_available_at"], item["class_center_id"],
    ))
    return {
        "rule_id": "U194",
        "as_of": as_of,
        "status": (
            "pending_segment_membership" if missing_formal
            else "component_candidates_only" if pairs
            else "no_component_candidate"
        ),
        "class_center_count": len(observed),
        "formal_center_count": len(formal),
        "component_pair_count": len(pairs),
        "price_overlap_pair_count": sum(item["price_core_overlap"] for item in pairs),
        "formal_center_ids_missing_segment_membership": tuple(sorted(missing_formal)),
        "later_class_observation_overlap_count": later_observation_overlap_count,
        "pairs": tuple(pairs),
        "same_center_identity_proven": False,
        "tradable": False,
    }
