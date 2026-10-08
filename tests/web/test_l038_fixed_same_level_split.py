"""The chart exposes L038's fixed view without changing lifecycle centers."""

from dataclasses import replace
from datetime import timedelta
from types import SimpleNamespace

from pathlib import Path

import pandas as pd

from chanlun.cl_utils.strict_chart import _l038_six_member_split_observations
from chanlun.core.strict_structure.same_level_decomposition import (
    fixed_three_member_partition_payload,
    review_fixed_three_member_partition,
)
from chanlun.core.strict_structure.models import SourceKind
from tests.core.strict_structure.helpers import unit
from chanlun.screening.runner import snapshot_for


def _connected_lower_types(prices):
    return tuple(
        replace(
            unit(
                index, "up" if end > start else "down", start, end,
                source_kind=SourceKind.TREND_TYPE, structural_level=1,
            ),
            child_ids=(f"completed-5m-type-{index}",),
        )
        for index, (start, end) in enumerate(zip(prices, prices[1:]))
    )


def test_declared_30m_view_forms_one_type_from_three_completed_lower_types():
    values = _connected_lower_types((100, 120, 110, 118))
    early = review_fixed_three_member_partition(
        values, view_id="30m-fixed-test", operating_type_label="30m",
        as_of=values[-1].available_at - timedelta(seconds=1),
    )
    assert not early.candidates
    assert early.pending_source_unit_ids == tuple(unit.unit_id for unit in values[:2])
    assert early.unavailable_future_count == 1

    review = review_fixed_three_member_partition(
        values, view_id="30m-fixed-test", operating_type_label="30m",
        as_of=values[-1].available_at,
    )
    payload = fixed_three_member_partition_payload(review)
    assert payload["target_structural_level"] == 1
    assert payload["lower_trend_structural_level"] == 0
    assert payload["status"] == "source_covered_by_local_three_member_candidates"
    assert payload["candidates"][0]["member_unit_ids"] == [unit.unit_id for unit in values]
    assert payload["candidates"][0]["core_ticks"] == [110, 118]
    assert payload["candidates"][0]["known_at"] == int(values[-1].available_at.timestamp())
    assert not payload["whole_path_partition_certified"]
    assert not payload["candidates"][0]["formal_type_issued"]
    assert not payload["candidates"][0]["lower_type_qualification_pending"]
    assert payload["candidates"][0]["fixed_view_local_type_completed"]

    protected = review_fixed_three_member_partition(
        values, view_id="30m-fixed-test", operating_type_label="30m",
        as_of=values[-1].available_at,
        pending_lower_type_ids=frozenset({values[1].unit_id}),
    )
    assert fixed_three_member_partition_payload(protected)["candidates"][0][
        "lower_type_qualification_pending"
    ]
    assert not fixed_three_member_partition_payload(protected)["candidates"][0][
        "fixed_view_local_type_completed"
    ]


def test_declared_30m_view_connects_two_three_member_types_without_reusing_members():
    values = _connected_lower_types((100, 120, 110, 118, 112, 117, 113))
    review = review_fixed_three_member_partition(
        values, view_id="30m-fixed-test", operating_type_label="30m",
        as_of=values[-1].available_at,
    )
    assert len(review.candidates) == 2
    first, second = review.candidates
    assert first.member_unit_ids == tuple(unit.unit_id for unit in values[:3])
    assert second.member_unit_ids == tuple(unit.unit_id for unit in values[3:])
    assert first.market_end == second.market_start
    assert first.end_tick == second.start_tick
    assert not review.pending_source_unit_ids


def test_declared_30m_view_keeps_disjoint_followup_for_trend_or_repartition():
    values = _connected_lower_types((100, 120, 110, 118, 80, 130, 125))
    review = review_fixed_three_member_partition(
        values, view_id="30m-fixed-test", operating_type_label="30m",
        as_of=values[-1].available_at,
    )
    assert len(review.candidates) == 1
    assert review.status == "pending_nonoverlap_or_connector_repartition"
    assert review.pending_source_unit_ids == tuple(unit.unit_id for unit in values[3:])


def test_declared_30m_view_keeps_later_local_center_visible_after_first_gap():
    values = _connected_lower_types((100, 150, 80, 90, 70))
    review = review_fixed_three_member_partition(
        values, view_id="30m-fixed-post-gap", operating_type_label="30m",
        as_of=values[-1].available_at,
    )
    payload = fixed_three_member_partition_payload(review)
    assert not payload["candidates"]
    assert payload["status"] == "pending_nonoverlap_or_connector_repartition"
    later, = payload["post_gap_center_candidates"]
    assert later["member_unit_ids"] == [unit.unit_id for unit in values[1:]]
    assert later["core_ticks"] == [80, 90]
    assert later["post_gap_observation"]
    assert not later["fixed_view_local_type_completed"]
    assert not later["formal_type_issued"]
    assert not payload["whole_path_partition_certified"]


def test_declared_30m_view_does_not_count_a_combination_as_one_lower_type():
    values = _connected_lower_types((100, 120, 110, 118))
    combined = replace(
        values[1], same_level_combination=True,
        child_ids=("completed-5m-type-a", "completed-5m-type-b"),
    )
    review = review_fixed_three_member_partition(
        (values[0], combined, values[2]), view_id="30m-fixed-test",
        operating_type_label="30m", as_of=values[-1].available_at,
    )
    assert review.status == "pending_combined_lower_type_membership_review"
    assert not review.candidates


def test_restored_5m_source_does_not_publish_old_geometric_30m_view():
    frame = pd.read_parquet(Path(__file__).parents[1] / "fixtures/SH.600519_5m.parquet")
    frame.attrs.update(
        structure_price_quantum="0.01", price_basis_revision="recorded-raw",
        screening_market="a",
    )
    snapshot = snapshot_for(frame, "SH.600519", "5m")
    assert snapshot["declared_30m_fixed_view"] is None
    assert snapshot["levels"][0]["centers"]
    assert all(center["entry_unit_id"] and center["establishment_leave_unit_id"]
               for center in snapshot["levels"][0]["centers"])


def test_fixed_six_member_split_is_causal_and_keeps_exact_child_lineage():
    prices = (100, 120, 110, 118, 112, 117, 113)
    values = tuple(
        replace(
            unit(
                index, "up" if end > start else "down", start, end,
                source_kind=SourceKind.TREND_TYPE, structural_level=1,
            ),
            child_ids=(f"completed-child-{index}",),
        )
        for index, (start, end) in enumerate(zip(prices, prices[1:]))
    )
    level = SimpleNamespace(structural_level=1, units=values)
    assert _l038_six_member_split_observations(
        level, values[-1].available_at - timedelta(seconds=1)
    ) == []
    observed, = _l038_six_member_split_observations(
        level, values[-1].available_at
    )
    assert observed["source_unit_ids"] == [item.unit_id for item in values]
    assert observed["left_member_ids"] == [item.unit_id for item in values[:3]]
    assert observed["right_member_ids"] == [item.unit_id for item in values[3:]]
    assert observed["boundary_market_at"] == int(values[2].market_end.timestamp())
    assert observed["known_at"] == int(values[-1].available_at.timestamp())
    assert observed["whole_path_partition_certified"] is False
    assert observed["formal_point_from_this_observation"] is False


def test_restored_recursive_source_does_not_fabricate_bounded_splits():
    frame = pd.read_parquet(
        Path(__file__).parents[1] / "fixtures/segment_local/SZ.001309_1m.parquet"
    )
    frame.attrs.update(
        structure_price_quantum="0.01",
        price_basis_revision="recorded-raw",
        screening_market="a",
    )
    snapshot = snapshot_for(frame, "SZ.001309", "1m")
    assert snapshot["analysis_scope"] == "native_centers"
    assert len(snapshot["levels"]) == 1
    assert snapshot["levels"][0]["trend_types"] == []
