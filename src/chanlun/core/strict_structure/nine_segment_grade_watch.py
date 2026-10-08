"""Non-trading U180 review when a physical center lifecycle spans nine segments.

L096:172-181 asks for a higher-grade check after nine segment extensions; it
does not say nine segments alone prove a five-minute center or invalidate every
one-minute third buy. L080:1582-1603 answers a reader's *assumed* completed
five-minute center with a five-minute oscillation reading, not proof of the
reader's nine-segment premise. L066:532-541 groups nine lower *trend types*
3+3+3, not nine raw segments. L092:52-58's >9 Zn hint is another observation
and must not be used as this segment count.
"""

from __future__ import annotations

from datetime import datetime

from chanlun.core.strict_structure.center_frame import initial_center_known_at
from chanlun.core.strict_structure.models import SourceKind, StrictLevelResult


def observe_u180_nine_segment_grade_watches(
    physical_level: StrictLevelResult,
    higher_level: StrictLevelResult | None,
    *,
    as_of: datetime,
) -> tuple[dict, ...]:
    """Flag completed physical center-lifecycle runs of nine or more segments.

    This is a bounded source-scope proxy for the author's case, not proof that
    its exact nine-segment grouping, parent center or third point is complete.
    """

    if (not isinstance(as_of, datetime) or as_of.tzinfo is None
            or as_of.utcoffset() is None):
        raise ValueError("U180 watch requires an aware cutoff")
    if not isinstance(physical_level, StrictLevelResult):
        raise TypeError("U180 watch requires a strict physical level")
    if physical_level.structural_level != 0:
        raise ValueError("U180 physical segment watch starts at structural level zero")
    if higher_level is not None and (
        not isinstance(higher_level, StrictLevelResult)
        or higher_level.structural_level != 1
    ):
        raise ValueError("U180 higher context must be the next structural level")
    units = tuple(physical_level.units)
    if any(unit.source_kind is not SourceKind.SEGMENT for unit in units):
        raise ValueError("U180 physical input requires segment units")
    index = {unit.unit_id: offset for offset, unit in enumerate(units)}
    if len(index) != len(units):
        raise ValueError("U180 segment stream has duplicate unit IDs")
    higher_ids = tuple(sorted(
        center.center_id
        for center in (() if higher_level is None else higher_level.center_result.centers)
        if initial_center_known_at(center) <= as_of
    ))
    locked_types = {}
    for trend in (*physical_level.trend_types, *physical_level.completed_trends):
        if not trend.locked or trend.available_at > as_of:
            continue
        unit_ids = tuple(unit.unit_id for unit in trend.constituent_units)
        previous = locked_types.setdefault(trend.trend_id, unit_ids)
        if previous != unit_ids:
            raise ValueError("U180 locked lower type changed its segment members")
    output = []
    for center in physical_level.center_result.centers:
        center_known_at = initial_center_known_at(center)
        if center_known_at > as_of:
            continue
        if center.structural_level != 0 or center.source_kind is not SourceKind.SEGMENT:
            raise ValueError("U180 center source differs from the physical segment stream")
        source = tuple((*center.body_units, *center.failed_departure_units))
        try:
            ordered = tuple(sorted(source, key=lambda unit: index[unit.unit_id]))
        except KeyError as exc:
            raise ValueError("U180 center references a missing segment unit") from exc
        positions = tuple(index[unit.unit_id] for unit in ordered)
        if len(set(positions)) != len(positions) or positions != tuple(range(
            positions[0], positions[-1] + 1
        )):
            raise ValueError("U180 center lifecycle segment source is not contiguous")
        completed = tuple(
            unit for unit in ordered if unit.locked and unit.available_at <= as_of
        )
        if len(completed) < 9:
            continue
        ninth = completed[8]
        first_nine_ids = tuple(unit.unit_id for unit in completed[:9])
        nine_segment_review_known_at = max(center_known_at, ninth.available_at)
        parent_reviews = []
        for parent in (() if higher_level is None else higher_level.center_result.centers):
            parent_initial_known_at = initial_center_known_at(parent)
            if parent_initial_known_at > as_of:
                continue
            if (
                parent.structural_level != 1
                or parent.source_kind is not SourceKind.TREND_TYPE
                or parent.price_basis_revision != center.price_basis_revision
            ):
                raise ValueError("U180 higher center uses another structural context")
            members = tuple(parent.initial_units)
            if len(members) != 3:
                raise ValueError("U180 higher center needs three initial lower types")
            groups = tuple(tuple(unit.child_ids) for unit in members)
            lower_types_verified = all(
                unit.locked and unit.available_at <= as_of
                and locked_types.get(unit.unit_id) == group
                for unit, group in zip(members, groups)
            )
            member_ids = {identifier for group in groups for identifier in group}
            shared = tuple(identifier for identifier in first_nine_ids
                           if identifier in member_ids)
            first_nine_contained = set(first_nine_ids) <= member_ids
            watch_span_contained = set(unit.unit_id for unit in completed) <= member_ids
            source_linked = lower_types_verified and first_nine_contained
            all_watch_source_linked = source_linked and watch_span_contained
            parent_grade_source_known_at = (
                max(
                    nine_segment_review_known_at,
                    parent_initial_known_at,
                    *(unit.available_at for unit in completed),
                    *(unit.available_at for unit in members),
                )
                if all_watch_source_linked else None
            )
            parent_reviews.append({
                "parent_center_id": parent.center_id,
                "parent_established_at": parent.established_at,
                "parent_member_type_unit_ids": tuple(unit.unit_id for unit in members),
                "parent_member_segment_groups": groups,
                "parent_three_locked_child_types_verified": lower_types_verified,
                "shared_first_nine_segment_unit_ids": shared,
                "first_nine_contained_in_three_members": first_nine_contained,
                "all_completed_watch_segments_contained": watch_span_contained,
                "nine_segment_source_in_three_locked_types_proven": source_linked,
                "all_completed_watch_segments_in_three_locked_types_proven": (
                    all_watch_source_linked
                ),
                "parent_grade_source_known_at": parent_grade_source_known_at,
                "relation": (
                    "nine_segment_source_in_three_locked_types" if source_linked
                    else "partial_segment_source_overlap" if shared
                    else "no_shared_segment_sources"
                ),
                "author_case_identity_proven": False,
                "third_buy_identity_changed": False,
                "tradable": False,
            })
        parent_reviews.sort(key=lambda item: (
            item["parent_established_at"], item["parent_center_id"]
        ))
        same_source_reviews = tuple(
            item for item in parent_reviews
            if item["all_completed_watch_segments_in_three_locked_types_proven"]
        )
        output.append({
            "rule_id": "U180",
            "related_rule_ids": ("U180", "U308"),
            "source_refs": (
                "L096:172-181", "L096:313-316",
                "L066:532-541", "L080:1603-1603", "L018:31-55",
                "L100:733-754",
            ),
            "center_id": center.center_id,
            "source_kind": center.source_kind.value,
            "structural_level": center.structural_level,
            "price_basis_revision": center.price_basis_revision,
            "source_scope": "one_formal_center_body_plus_failed_departures",
            "completed_segment_count": len(completed),
            "segment_unit_ids": tuple(unit.unit_id for unit in completed),
            "ninth_segment_unit_id": ninth.unit_id,
            "ninth_segment_market_at": ninth.market_end,
            "review_known_at": nine_segment_review_known_at,
            "nine_segment_review_known_at": nine_segment_review_known_at,
            "higher_level_visible_center_ids": higher_ids,
            "higher_level_member_reviews": tuple(parent_reviews),
            "higher_level_center_ids_with_shared_nine_segments": tuple(
                item["parent_center_id"] for item in parent_reviews
                if item["shared_first_nine_segment_unit_ids"]
            ),
            "nine_segment_source_contained_formal_parent_center_ids": tuple(
                item["parent_center_id"] for item in parent_reviews
                if item["nine_segment_source_in_three_locked_types_proven"]
            ),
            "all_completed_watch_segments_in_formal_parent_center_ids": tuple(
                item["parent_center_id"] for item in same_source_reviews
            ),
            "higher_grade_source_link_known_at": (
                min(item["parent_grade_source_known_at"]
                    for item in same_source_reviews)
                if same_source_reviews else None
            ),
            "higher_grade_source_link_status": (
                "same_source_formal_parent_observed_after_ninth_segment"
                if same_source_reviews else
                "first_nine_source_only_review"
                if any(item["nine_segment_source_in_three_locked_types_proven"]
                       for item in parent_reviews) else
                "no_same_source_formal_parent_yet"
            ),
            "status": "nine_segment_higher_grade_review_required",
            "author_exact_nine_segment_pattern_certified": False,
            "three_completed_child_types_certified_by_this_watch": False,
            "matched_parent_center_identity_proven": False,
            "current_higher_grade_reclassified_by_this_watch": False,
            "one_minute_third_buy_status_changed": False,
            "tradable": False,
        })
    output.sort(key=lambda item: (
        item["review_known_at"], item["center_id"]
    ))
    return tuple(output)
