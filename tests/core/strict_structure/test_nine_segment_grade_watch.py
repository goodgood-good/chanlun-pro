"""L096: nine physical segments request review; they do not form a parent center."""

from datetime import timedelta
from pathlib import Path

import pandas as pd
import pytest

from chanlun.cl_utils.strict_chart import build_center_snapshot
from chanlun.cl_utils.strict_chart_runtime import build_strict_chart_cd
from chanlun.core.strict_structure.nine_segment_grade_watch import (
    observe_u180_nine_segment_grade_watches,
)


@pytest.fixture(scope="module")
def recorded_source():
    path = Path(__file__).parents[2] / "fixtures/segment_origin/SH.688377_5m.parquet"
    frame = pd.read_parquet(path)
    frame.attrs.update(structure_price_quantum="0.01", price_basis_revision="recorded-raw")
    runtime = build_strict_chart_cd(
        market="a", code="SH.688377", frequency="5m",
        frame=frame, last_bar_closed=True,
    )
    assert runtime.cd is not None
    return runtime.cd


def test_recorded_nine_segment_lifecycle_is_a_review_not_an_upgrade(recorded_source):
    cd = recorded_source
    evidence = cd.get_strict_evidence()
    levels = evidence.structure.levels
    watches = observe_u180_nine_segment_grade_watches(
        levels[0], levels[1] if len(levels) > 1 else None,
        as_of=cd._strict_as_of(),
    )
    watch, = watches
    assert watch["completed_segment_count"] == 12
    assert len(watch["segment_unit_ids"]) == 12
    assert watch["ninth_segment_unit_id"] == watch["segment_unit_ids"][8]
    assert watch["status"] == "nine_segment_higher_grade_review_required"
    assert watch["higher_level_visible_center_ids"]
    parent_review, = watch["higher_level_member_reviews"]
    assert parent_review["parent_three_locked_child_types_verified"]
    assert parent_review["relation"] == "no_shared_segment_sources"
    assert parent_review["shared_first_nine_segment_unit_ids"] == ()
    assert not parent_review["first_nine_contained_in_three_members"]
    assert not parent_review["nine_segment_source_in_three_locked_types_proven"]
    assert watch["higher_level_center_ids_with_shared_nine_segments"] == ()
    assert watch["nine_segment_source_contained_formal_parent_center_ids"] == ()
    assert watch["related_rule_ids"] == ("U180", "U308")
    assert watch["nine_segment_review_known_at"] == watch["review_known_at"]
    assert watch["higher_grade_source_link_status"] == "no_same_source_formal_parent_yet"
    assert watch["higher_grade_source_link_known_at"] is None
    assert watch["all_completed_watch_segments_in_formal_parent_center_ids"] == ()
    assert not watch["current_higher_grade_reclassified_by_this_watch"]
    assert not watch["author_exact_nine_segment_pattern_certified"]
    assert not watch["three_completed_child_types_certified_by_this_watch"]
    assert not watch["matched_parent_center_identity_proven"]
    assert not watch["one_minute_third_buy_status_changed"]
    assert not watch["tradable"]

    snapshot = build_center_snapshot(
        cd, interval="5m",
        display_bar_closed_at=(int(cd._strict_as_of().timestamp()),),
    )
    published, = snapshot["u180_nine_segment_grade_watch_states"]
    assert published["ninth_segment_unit_id"] == watch["ninth_segment_unit_id"]
    assert snapshot["u180_nine_segment_grade_watch_review"]["candidate_count"] == 1
    assert snapshot["u180_nine_segment_grade_watch_review"]["source_linked_parent_center_count"] == 0
    assert snapshot["u180_nine_segment_grade_watch_review"]["nine_segment_source_contained_parent_center_count"] == 0
    assert published["higher_level_member_reviews"][0]["relation"] == "no_shared_segment_sources"
    assert not snapshot["u180_nine_segment_grade_watch_review"]["higher_center_membership_certified_by_watch"]
    assert snapshot["u180_nine_segment_grade_watch_review"][
        "all_completed_watch_source_contained_parent_center_count"
    ] == 0
    assert published["higher_grade_source_link_known_at"] is None


def test_review_does_not_appear_before_the_ninth_confirmed_segment(recorded_source):
    cd = recorded_source
    levels = cd.get_strict_evidence().structure.levels
    watch, = observe_u180_nine_segment_grade_watches(
        levels[0], levels[1] if len(levels) > 1 else None,
        as_of=cd._strict_as_of(),
    )
    ninth = next(unit for unit in levels[0].units
                 if unit.unit_id == watch["ninth_segment_unit_id"])
    assert not observe_u180_nine_segment_grade_watches(
        levels[0], levels[1] if len(levels) > 1 else None,
        as_of=ninth.available_at - timedelta(microseconds=1),
    )
    assert not observe_u180_nine_segment_grade_watches(
        levels[0], levels[1] if len(levels) > 1 else None,
        as_of=watch["review_known_at"] - timedelta(microseconds=1),
    )
    with pytest.raises(ValueError, match="aware cutoff"):
        observe_u180_nine_segment_grade_watches(
            levels[0], levels[1] if len(levels) > 1 else None,
            as_of=ninth.available_at.replace(tzinfo=None),
        )


def test_recorded_parent_source_can_contain_nine_segments_without_three_raw_groups():
    path = Path(__file__).parents[2] / "fixtures/segment_origin/SZ.300867_5m.parquet"
    frame = pd.read_parquet(path)
    frame.attrs.update(structure_price_quantum="0.01", price_basis_revision="recorded-raw")
    runtime = build_strict_chart_cd(
        market="a", code="SZ.300867", frequency="5m",
        frame=frame, last_bar_closed=True,
    )
    assert runtime.cd is not None
    cd = runtime.cd
    levels = cd.get_strict_evidence().structure.levels
    watches = observe_u180_nine_segment_grade_watches(
        levels[0], levels[1], as_of=cd._strict_as_of(),
    )
    watch = next(w for w in watches if w["completed_segment_count"] == 10)
    parent, = watch["higher_level_member_reviews"]
    physical_center = next(
        center for center in levels[0].center_result.centers
        if center.center_id == watch["center_id"]
    )
    assert max(unit.available_at for unit in physical_center.initial_units) < (
        watch["nine_segment_review_known_at"]
    )
    assert physical_center.available_at > watch["nine_segment_review_known_at"]
    assert watch["completed_segment_count"] == 10
    assert parent["parent_three_locked_child_types_verified"]
    assert parent["relation"] == "nine_segment_source_in_three_locked_types"
    assert len(parent["shared_first_nine_segment_unit_ids"]) == 9
    assert [len(group) for group in parent["parent_member_segment_groups"]] == [33, 7, 7]
    assert watch["nine_segment_source_contained_formal_parent_center_ids"] == (
        parent["parent_center_id"],
    )
    assert parent["all_completed_watch_segments_in_three_locked_types_proven"]
    assert watch["all_completed_watch_segments_in_formal_parent_center_ids"] == (
        parent["parent_center_id"],
    )
    assert watch["higher_grade_source_link_status"] == (
        "same_source_formal_parent_observed_after_ninth_segment"
    )
    assert parent["parent_established_at"] < parent["parent_grade_source_known_at"]
    assert parent["parent_grade_source_known_at"] == watch[
        "higher_grade_source_link_known_at"
    ]
    assert watch["review_known_at"] < watch["higher_grade_source_link_known_at"]
    assert not watch["current_higher_grade_reclassified_by_this_watch"]
    assert not parent["author_case_identity_proven"]
    assert not watch["author_exact_nine_segment_pattern_certified"]
    assert not watch["one_minute_third_buy_status_changed"]

    snapshot = build_center_snapshot(
        cd, interval="5m",
        display_bar_closed_at=(int(cd._strict_as_of().timestamp()),),
    )
    assert snapshot["u180_nine_segment_grade_watch_review"]["nine_segment_source_contained_parent_center_count"] == 2
    assert snapshot["u180_nine_segment_grade_watch_review"][
        "all_completed_watch_source_contained_parent_center_count"
    ] == 2
    published = next(w for w in snapshot["u180_nine_segment_grade_watch_states"] if w["center_id"] == watch["center_id"])
    assert published[
        "higher_grade_source_link_known_at"
    ] == int(watch["higher_grade_source_link_known_at"].timestamp())
    assert published[
        "higher_level_member_reviews"
    ][0]["parent_grade_source_known_at"] == int(
        parent["parent_grade_source_known_at"].timestamp()
    )
    assert not published["higher_level_member_reviews"][0]["author_case_identity_proven"]

    before_parent = observe_u180_nine_segment_grade_watches(
        levels[0], levels[1],
        as_of=parent["parent_grade_source_known_at"] - timedelta(microseconds=1),
    )
    earlier_watch = next(w for w in before_parent if w["center_id"] == watch["center_id"])
    assert earlier_watch["higher_level_visible_center_ids"] == ()
    assert earlier_watch["higher_level_member_reviews"] == ()
    assert earlier_watch["higher_grade_source_link_known_at"] is None
    at_parent = observe_u180_nine_segment_grade_watches(
        levels[0], levels[1], as_of=parent["parent_grade_source_known_at"],
    )
    linked_watch = next(w for w in at_parent if w["center_id"] == watch["center_id"])
    assert linked_watch["higher_grade_source_link_known_at"] == parent[
        "parent_grade_source_known_at"
    ]
    assert linked_watch["all_completed_watch_segments_in_formal_parent_center_ids"] == (
        parent["parent_center_id"],
    )
