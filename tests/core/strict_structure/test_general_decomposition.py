"""Lesson 54's two declared associations of one physical segment path."""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import timedelta, datetime, timezone
from decimal import Decimal
from pathlib import Path

import pandas as pd
import pytest

from chanlun.cl_utils.strict_chart_runtime import build_strict_chart_cd
from chanlun.core.strict_structure.center_machine import (
    calculate_centers,
    establish_three_core_segment_center,
    replay_three_core_segment_center,
)
from chanlun.core.strict_structure.general_decomposition import (
    associative_view_payload,
    build_declared_three_core_signal_view,
    build_declared_parent_view,
    certify_declared_child_partition,
    declared_parent_view_payload,
    declared_upgrade_child_path_payload,
    declared_general_signal_view_payload,
    discover_entry_anchored_associations,
    GapBarEvidence,
    general_center_connector_observation_payload,
    materialize_associative_view,
    materialize_declared_upgrade_child_path,
    materialize_segmentized_center_path,
    observe_explicit_general_center_connector,
    observe_three_segment_cores,
    qualify_declared_child_type,
    qualify_locked_trend_type,
    review_declared_upgrade_split,
    replay_declared_upgrade_parent_geometry,
    segmentized_center_path_payload,
    three_segment_core_payload,
)
from chanlun.core.strict_structure.models import ConstituentUnit, SourceKind, TrendState
from chanlun.core.strict_structure.same_level_decomposition import combine_same_level_trends
from chanlun.core.strict_structure.unit_adapter import trend_type_to_unit
from chanlun.core.strict_structure.trend_assembler import assemble_trend_types
from tests.core.strict_structure.helpers import completed_up_center, engine_for, only_point, unit
from tests.core.strict_structure.test_first_class_points import (
    divergent_strength,
    structure_from_values,
)
from tests.core.strict_structure.test_recursive_engine import _two_completed_centers


def _l054_segments():
    # L054:181-196, 256-274, 322-331 in D:\缠论\chanlun_lesson_corpus.
    # d1=g2=80 and d2=g4=60; the chosen ticks preserve the source's ordering.
    vertices = (100, 80, 90, 60, 80, 50, 70, 40, 60, 45, 65, 35, 55, 30, 50)
    return tuple(
        unit(index, "up" if end > start else "down", start, end)
        for index, (start, end) in enumerate(zip(vertices, vertices[1:]))
    )


def _ids(values, groups):
    return tuple(tuple(values[index].unit_id for index in group) for group in groups)


def test_l099_explicit_gap_connector_is_observed_without_parent_type_promotion():
    # L099:37-56 allows a gap between centers in a general decomposition.
    # Synthetic bars prove only this submitted adjacent pair, not a market-wide
    # chart inference or a completed same-grade child type.
    previous = completed_up_center()
    current = completed_up_center(10, zd_tick=145, zg_tick=155)
    as_of = max(previous.available_at, current.available_at)
    left = previous.completion_return_unit
    right = current.entry_unit or current.body_units[0]
    bars = (
        GapBarEvidence("before", left.market_end, 115, 125, previous.price_basis_revision),
        GapBarEvidence("after", right.market_start, 130, 140, previous.price_basis_revision),
    )
    observed = observe_explicit_general_center_connector(
        previous, current, as_of=as_of, market_source_ref="controlled-bar-window",
        source_bars=bars, gap_after_bar_index=0,
    )
    payload = general_center_connector_observation_payload(observed)
    assert payload["connector_kind"] == "direct_price_gap"
    assert payload["connector_source_ids"] == ["before", "after"]
    assert payload["source_adjacency_scope"] == "adjacent_in_submitted_closed_bar_window"
    assert payload["rule_source_ref"] == "L099:37-56"
    assert not any(payload[key] for key in (
        "formal_parent_type_certified", "higher_center_certified",
        "third_point_certified", "tradable",
    ))

    with pytest.raises(ValueError, match="non-overlapping gap"):
        observe_explicit_general_center_connector(
            previous, current, as_of=as_of, market_source_ref="controlled-bar-window",
            source_bars=(bars[0], replace(bars[1], low_tick=125)), gap_after_bar_index=0,
        )
    with pytest.raises(ValueError, match="non-overlapping gap"):
        observe_explicit_general_center_connector(
            previous, current, as_of=as_of, market_source_ref="controlled-bar-window",
            source_bars=(bars[0], replace(bars[1], closed_at=right.market_start - timedelta(minutes=1))),
            gap_after_bar_index=0,
        )


def test_l099_explicit_lower_segment_connector_requires_full_boundary_coverage():
    previous = completed_up_center()
    current = completed_up_center(10, zd_tick=145, zg_tick=155)
    as_of = max(previous.available_at, current.available_at)
    left = previous.completion_return_unit
    right = current.entry_unit or current.body_units[0]
    bridge = ConstituentUnit(
        unit_id="lower-bridge", structural_level=0, source_kind=SourceKind.SEGMENT,
        price_basis_revision=previous.price_basis_revision, direction="up",
        start_tick=left.end_tick, end_tick=right.start_tick,
        low_tick=left.end_tick, high_tick=right.start_tick,
        market_start=left.market_end, market_end=right.market_start,
        confirmed_at=right.market_start, available_at=right.market_start,
        locked=True, child_ids=(),
    )
    observed = observe_explicit_general_center_connector(
        previous, current, as_of=as_of, market_source_ref="controlled-segment-window",
        lower_segments=(bridge,),
    )
    payload = general_center_connector_observation_payload(observed)
    assert payload["connector_kind"] == "lower_segment_path"
    assert payload["connector_source_ids"] == ["lower-bridge"]
    assert not payload["formal_parent_type_certified"] and not payload["tradable"]
    with pytest.raises(ValueError, match="entire center boundary"):
        observe_explicit_general_center_connector(
            previous, current, as_of=as_of, market_source_ref="controlled-segment-window",
            lower_segments=(replace(bridge, market_end=bridge.market_end - timedelta(minutes=1)),),
        )
    with pytest.raises(ValueError, match="independent locked source"):
        observe_explicit_general_center_connector(
            previous, current, as_of=as_of, market_source_ref="controlled-segment-window",
            lower_segments=(replace(bridge, locked=False, confirmed_at=None),),
        )


def test_declared_source_chains_share_market_endpoints_at_both_grades():
    # L038:1450-1456 makes the next completed type start at the prior end.
    # Matching price ticks with an unowned market-time gap is not that point.
    def gap_after(previous, current):
        start = previous.market_end + timedelta(seconds=1)
        return replace(
            current,
            market_start=start,
            low_market_time=(
                None if current.low_market_time is None
                else max(current.low_market_time, start)
            ),
            high_market_time=(
                None if current.high_market_time is None
                else max(current.high_market_time, start)
            ),
        )

    values = _l054_segments()
    gapped = (
        values[0],
        gap_after(values[0], values[1]),
        *values[2:],
    )
    assert gapped[0].end_tick == gapped[1].start_tick
    with pytest.raises(ValueError, match="alternate and connect"):
        observe_three_segment_cores(gapped, as_of=values[-1].available_at)

    groups = ((1, 2, 3), (4, 5, 6), (7, 8, 9))
    children = tuple(
        qualify_declared_child_type(
            tuple(values[index] for index in group),
            view_id="l054-shared-endpoint",
            evidence_ref="L054:256-274", as_of=values[-1].available_at,
        )
        for group in groups
    )
    parent_source = (
        children[0].unit,
        gap_after(children[0].unit, children[1].unit),
        children[2].unit,
    )
    assert parent_source[0].end_tick == parent_source[1].start_tick
    with pytest.raises(ValueError, match="market endpoints must connect"):
        certify_declared_child_partition(
            parent_source, (), view_id="l054-shared-endpoint",
            as_of=values[-1].available_at,
        )


def test_l054_two_views_keep_distinct_parent_members_ranges_and_clocks():
    values = _l054_segments()
    first = materialize_associative_view(
        values,
        view_id="l054-first",
        component_member_ids=_ids(values, ((1, 2, 3), (4, 5, 6), (7, 8, 9))),
        as_of=values[-1].available_at,
    )
    second = materialize_associative_view(
        values,
        view_id="l054-second",
        component_member_ids=_ids(values, ((3, 4, 5), (6, 7, 8), (9, 10, 11))),
        as_of=values[-1].available_at,
    )

    assert first.components[0].core_zd_tick == first.components[0].core_zg_tick == 80
    assert [(parent.zd_tick, parent.zg_tick) for parent in first.parent_centers] == [(60, 65)]
    assert [(parent.zd_tick, parent.zg_tick) for parent in second.parent_centers] == [(50, 65)]
    assert first.parent_centers[0].observed_at == values[9].available_at
    assert second.parent_centers[0].observed_at == values[11].available_at
    assert first.parent_centers[0].center_id != second.parent_centers[0].center_id
    assert first.source_structural_level == 0
    assert first.parent_centers[0].structural_level == 1
    assert first.prefix_unit_ids == (values[0].unit_id,)
    assert second.prefix_unit_ids == tuple(value.unit_id for value in values[:3])

    assert first.latest_parent_followup.departure_status == "segmentized_departure_candidate"
    assert first.latest_parent_followup.return_geometry == "outside_or_touching_lower_core_boundary"
    assert second.latest_parent_followup.departure_status == "pending_three_segment_child_structure"
    assert second.latest_parent_followup.return_geometry == "not_available"
    assert not first.latest_parent_followup.formal_third_point
    assert not second.latest_parent_followup.formal_third_point
    payload = associative_view_payload(first)
    assert json.loads(json.dumps(payload))["parent_centers"][0]["core"] == {
        "zd_tick": 60, "zg_tick": 65,
    }
    assert payload["tradable"] is False
    assert payload["parent_centers"][0]["structural_level"] == 1
    assert payload["latest_parent_followup"]["formal_third_point"] is False


def test_l054_second_view_is_causally_pending_at_first_views_g5():
    values = _l054_segments()
    first_prefix = materialize_associative_view(
        values[:10],
        view_id="l054-first",
        component_member_ids=_ids(values, ((1, 2, 3), (4, 5, 6), (7, 8, 9))),
        as_of=values[9].available_at,
    )
    second_prefix = materialize_associative_view(
        values[:10],
        view_id="l054-second",
        component_member_ids=_ids(values, ((3, 4, 5), (6, 7, 8))),
        as_of=values[9].available_at,
    )
    second_later = materialize_associative_view(
        values[:12],
        view_id="l054-second",
        component_member_ids=_ids(values, ((3, 4, 5), (6, 7, 8), (9, 10, 11))),
        as_of=values[11].available_at,
    )

    assert len(first_prefix.parent_centers) == 1
    assert second_prefix.parent_centers == ()
    assert second_prefix.components == second_later.components[:2]
    assert second_later.parent_centers[0].observed_at > first_prefix.parent_centers[0].observed_at


def test_entry_anchored_discovery_exposes_both_l054_hypotheses_without_promotion():
    values = _l054_segments()
    at_g5 = discover_entry_anchored_associations(
        values[:10], anchor_unit_id=values[0].unit_id,
        as_of=values[9].available_at,
    )
    at_g6 = discover_entry_anchored_associations(
        values[:12], anchor_unit_id=values[0].unit_id,
        as_of=values[11].available_at,
    )

    assert [view.components[0].member_unit_ids[0] for view in at_g5] == [
        values[1].unit_id, values[3].unit_id,
    ]
    assert [bool(view.parent_centers) for view in at_g5] == [True, False]
    by_first = {
        view.components[0].member_unit_ids[0]: view for view in at_g6
    }
    assert (by_first[values[1].unit_id].parent_centers[0].zd_tick,
            by_first[values[1].unit_id].parent_centers[0].zg_tick) == (60, 65)
    assert (by_first[values[3].unit_id].parent_centers[0].zd_tick,
            by_first[values[3].unit_id].parent_centers[0].zg_tick) == (50, 65)
    assert all(
        parent.formal_status == "requires_completed_child_type_certificates"
        for view in at_g6 for parent in view.parent_centers
    )


def test_l054_d2_has_three_complete_segment_core_before_five_role_owner():
    values = _l054_segments()
    observations = observe_three_segment_cores(
        values[:3], as_of=values[2].available_at
    )

    assert len(observations) == 1
    assert (observations[0].zd_tick, observations[0].zg_tick) == (80, 90)
    assert observations[0].member_unit_ids == tuple(unit.unit_id for unit in values[:3])
    assert observations[0].market_formed_at == values[2].market_end
    payload = three_segment_core_payload(observations[0])
    assert payload["formal_center_id"] is None and not payload["tradable"]


def test_l054_declared_elementary_types_form_two_distinct_three_member_centers():
    values = _l054_segments()
    first_at_d2 = materialize_segmentized_center_path(
        values[:3], view_id="l054-initial", view_start_unit_id=values[0].unit_id,
        as_of=values[2].available_at,
    )
    first_at_g3 = materialize_segmentized_center_path(
        values[:6], view_id="l054-initial", view_start_unit_id=values[0].unit_id,
        as_of=values[5].available_at,
    )
    second_at_g2 = materialize_segmentized_center_path(
        values[:4], view_id="l054-shifted", view_start_unit_id=values[1].unit_id,
        as_of=values[3].available_at,
    )

    first = first_at_d2.center_result.centers[0]
    later = first_at_g3.center_result.centers[0]
    shifted = second_at_g2.center_result.centers[0]
    assert (first.zd_tick, first.zg_tick) == (80, 90)
    assert first.established_at == values[2].confirmed_at
    assert first.center_id == later.center_id
    assert later.state.value == "completed"
    assert (shifted.zd_tick, shifted.zg_tick) == (80, 80)
    assert shifted.center_id != first.center_id
    payload = segmentized_center_path_payload(first_at_d2)
    assert payload["centers"][0]["initial_source_member_ids"] == [
        unit.unit_id for unit in values[:3]
    ]
    assert payload["source_policy"] == "l054_declared_segment_as_completed_child_type"
    assert not payload["canonical_same_level_center"] and not payload["tradable"]
    later_payload = segmentized_center_path_payload(first_at_g3)
    assert later_payload["centers"][0]["third_class_geometry"]["point_type"] == "3sell"
    assert later_payload["centers"][0]["third_class_geometry"]["anchor_source_unit_id"] == values[5].unit_id
    assert not later_payload["centers"][0]["third_class_geometry"]["formal_strict_point"]


def test_l054_native_segment_center_can_form_at_three_members_in_selected_view():
    values = _l054_segments()
    first = establish_three_core_segment_center(values[:3], 0)
    shifted = establish_three_core_segment_center(values[1:4], 0)
    later = replay_three_core_segment_center(values[:6], 0)

    assert first is not None and shifted is not None and later is not None
    assert first.source_kind.value == shifted.source_kind.value == "segment"
    assert first.formation_rule == shifted.formation_rule == "three_core"
    assert (first.zd_tick, first.zg_tick) == (80, 90)
    assert first.established_at == values[2].confirmed_at
    assert (shifted.zd_tick, shifted.zg_tick) == (80, 80)
    assert first.center_id != shifted.center_id
    assert later.center_id == first.center_id
    assert later.state.value == "completed"
    assert later.third_class_confirmed
    point = only_point(engine_for(later).third_class_points())
    assert point.point_type == "3sell"
    assert point.center_id == later.center_id
    assert point.anchor_unit_id == values[5].unit_id


def test_l054_declared_general_view_owns_formal_three_sell_without_canonical_promotion():
    values = _l054_segments()
    first_at_d2 = build_declared_three_core_signal_view(
        values[:3], view_id="l054:initial",
        view_start_unit_id=values[0].unit_id,
        as_of=values[2].available_at, price_quantum=Decimal("0.01"),
    )
    first_at_g3 = build_declared_three_core_signal_view(
        values[:6], view_id="l054:initial",
        view_start_unit_id=values[0].unit_id,
        as_of=values[5].available_at, price_quantum=Decimal("0.01"),
    )
    shifted_at_g2 = build_declared_three_core_signal_view(
        values[:4], view_id="l054:shifted",
        view_start_unit_id=values[1].unit_id,
        as_of=values[3].available_at, price_quantum=Decimal("0.01"),
    )

    assert first_at_d2.structure.levels[0].decomposition_mode == "general_declared"
    assert first_at_d2.structure.levels[0].view_id == "l054:initial"
    assert first_at_d2.third_class_points == ()
    assert len(first_at_g3.third_class_points) == 1
    point = first_at_g3.third_class_points[0]
    assert point.point_type == "3sell" and point.center_id == (
        first_at_g3.structure.levels[0].center_result.centers[0].center_id
    )
    assert point.anchor_unit_id == values[5].unit_id
    assert shifted_at_g2.structure.levels[0].center_result.centers[0].zd_tick == 80
    assert shifted_at_g2.structure.levels[0].center_result.centers[0].zg_tick == 80
    payload = declared_general_signal_view_payload(first_at_g3)
    assert payload["third_class_points"][0]["status"] == "confirmed"
    assert not payload["canonical_same_level_output"] and not payload["tradable"]


def test_l054_two_declared_parent_centers_keep_g5_g6_and_g7_roles_separate():
    values = _l054_segments()
    as_of = values[-1].available_at

    def certificates(view_id, groups, ref):
        return tuple(
            qualify_declared_child_type(
                tuple(values[index] for index in group),
                view_id=view_id, evidence_ref=ref, as_of=as_of,
            )
            for group in groups
        )

    first_members = certificates(
        "l054:first-parent", ((1, 2, 3), (4, 5, 6), (7, 8, 9)),
        "L054:256-274",
    )
    second_members = certificates(
        "l054:second-parent", ((3, 4, 5), (6, 7, 8), (9, 10, 11)),
        "L054:322-331",
    )
    departure = certificates(
        "l054:first-parent", ((10, 11, 12),), "L054:277-295"
    )[0]
    returned = certificates(
        "l054:first-parent", ((13,),), "L054:283-295"
    )[0]
    first_at_g5 = build_declared_parent_view(
        values[:10], view_id="l054:first-parent",
        initial_certificates=first_members, as_of=values[9].available_at,
        price_quantum=Decimal("0.01"),
    )
    first_at_g7 = build_declared_parent_view(
        values, view_id="l054:first-parent",
        initial_certificates=first_members, departure_certificate=departure,
        return_certificate=returned, as_of=as_of,
        price_quantum=Decimal("0.01"),
    )
    second_at_g6 = build_declared_parent_view(
        values[:12], view_id="l054:second-parent",
        initial_certificates=second_members, as_of=values[11].available_at,
        price_quantum=Decimal("0.01"),
    )
    second_at_g7 = build_declared_parent_view(
        values, view_id="l054:second-parent",
        initial_certificates=second_members, as_of=as_of,
        price_quantum=Decimal("0.01"),
    )
    first_against_known = build_declared_parent_view(
        values, view_id="l054:first-parent",
        initial_certificates=first_members, departure_certificate=departure,
        return_certificate=returned, as_of=as_of,
        price_quantum=Decimal("0.01"),
        protected_parent_centers=(first_at_g5.parent_center,),
    )
    second_against_known = build_declared_parent_view(
        values, view_id="l054:second-parent",
        initial_certificates=second_members, as_of=as_of,
        price_quantum=Decimal("0.01"),
        protected_parent_centers=(first_at_g5.parent_center,),
    )

    assert (first_at_g5.parent_center.zd_tick,
            first_at_g5.parent_center.zg_tick) == (60, 65)
    assert first_at_g5.parent_center.established_at == values[9].confirmed_at
    assert first_at_g5.parent_center.state.value == "ongoing"
    assert first_at_g5.third_class_points == ()
    assert first_at_g7.parent_center.center_id == first_at_g5.parent_center.center_id
    assert first_at_g7.parent_center.state.value == "completed"
    assert [point.point_type for point in first_at_g7.third_class_points] == ["3sell"]
    assert first_at_g7.third_class_points[0].structural_level == 1
    assert (second_at_g6.parent_center.zd_tick,
            second_at_g6.parent_center.zg_tick) == (50, 65)
    assert second_at_g6.parent_center.established_at == values[11].confirmed_at
    assert second_at_g7.parent_center.state.value == "ongoing"
    assert second_at_g7.third_class_points == ()
    assert second_at_g7.unqualified_tail_unit_ids == (
        values[12].unit_id, values[13].unit_id,
    )
    assert second_at_g7.unqualified_tail_geometry == "touches_parent_core"
    assert first_at_g7.partition_coverage.coverage_status == "prefix_unqualified"
    assert first_at_g7.partition_coverage.unqualified_prefix_unit_ids == (
        values[0].unit_id,
    )
    assert first_at_g7.partition_coverage.unqualified_tail_unit_ids == ()
    assert not first_at_g7.partition_coverage.strict_completed_only
    assert second_at_g7.partition_coverage.coverage_status == (
        "prefix_and_tail_unqualified"
    )
    assert second_at_g7.partition_coverage.unqualified_prefix_unit_ids == tuple(
        unit.unit_id for unit in values[:3]
    )
    first_payload = declared_parent_view_payload(first_at_g7)
    second_payload = declared_parent_view_payload(second_at_g7)
    assert first_payload["third_class_points"][0]["status"] == "confirmed"
    assert first_payload["third_class_points"][0]["anchor_source_unit_ids"] == [
        values[13].unit_id,
    ]
    assert second_payload["third_class_points"] == []
    assert second_payload["unqualified_tail_geometry"] == "touches_parent_core"
    assert not first_payload["source_partition"]["general_view_legality_proven"]
    assert second_payload["source_partition"]["coverage_status"] == (
        "prefix_and_tail_unqualified"
    )
    assert not first_payload["tradable"] and not second_payload["tradable"]
    assert first_against_known.protected_center_relation.status == "same_existing_center"
    assert first_against_known.parent_path_audit.parent_path_status == (
        "source_partition_pending"
    )
    assert [point.point_type for point in first_against_known.third_class_points] == ["3sell"]
    assert second_against_known.protected_center_relation.status == "overlap_requires_repartition"
    assert second_against_known.parent_path_audit.parent_path_status == (
        "protected_repartition_required"
    )
    assert second_against_known.protected_center_relation.overlapping_source_unit_ids
    assert second_against_known.third_class_points == ()


def test_declared_parent_rejects_missing_or_cross_view_child_certificates():
    values = _l054_segments()
    as_of = values[-1].available_at
    first = tuple(
        qualify_declared_child_type(
            values[start : start + 3], view_id="first",
            evidence_ref="L054:256-274", as_of=as_of,
        )
        for start in (1, 4, 7)
    )
    foreign = qualify_declared_child_type(
        values[7:10], view_id="second",
        evidence_ref="L054:322-331", as_of=as_of,
    )
    skipped = qualify_declared_child_type(
        values[7:10], view_id="first",
        evidence_ref="L054:256-274", as_of=as_of,
    )
    with pytest.raises(ValueError, match="reuse or skip source members"):
        certify_declared_child_partition(
            values, (first[0], skipped), view_id="first", as_of=as_of
        )

    with pytest.raises(ValueError, match="exactly three"):
        build_declared_parent_view(
            values, view_id="first", initial_certificates=first[:2],
            as_of=as_of, price_quantum=Decimal("0.01"),
        )
    with pytest.raises(ValueError, match="another qualification view"):
        build_declared_parent_view(
            values, view_id="first",
            initial_certificates=(first[0], first[1], foreign),
            as_of=as_of, price_quantum=Decimal("0.01"),
        )
    with pytest.raises(ValueError, match="requires a departure"):
        build_declared_parent_view(
            values, view_id="first", initial_certificates=first,
            return_certificate=foreign, as_of=as_of,
            price_quantum=Decimal("0.01"),
        )
    forged = replace(
        first[2], unit=replace(
            first[2].unit, high_tick=first[2].unit.high_tick + 1,
        ),
    )
    with pytest.raises(ValueError, match="geometry or time differs"):
        build_declared_parent_view(
            values, view_id="first",
            initial_certificates=(first[0], first[1], forged),
            as_of=as_of, price_quantum=Decimal("0.01"),
        )


def test_declared_source_coverage_does_not_require_consolidation_orientation_to_alternate():
    # L038:81-94 permits consolidation + consolidation at a fixed grade.
    # These declared groups each have an overlapping three-member core; the
    # second also includes its upward departure. Coverage proves connection,
    # not completion, a selected general view, or a parent center.
    vertices = (100, 120, 110, 130, 120, 140, 125, 150)
    values = tuple(
        unit(index, "up" if end > start else "down", start, end)
        for index, (start, end) in enumerate(zip(vertices, vertices[1:]))
    )
    as_of = values[-1].available_at
    certificates = tuple(
        qualify_declared_child_type(
            values[start:end], view_id="same-orientation",
            evidence_ref="L038:81-94", as_of=as_of,
        )
        for start, end in ((0, 3), (3, 7))
    )
    assert [item.unit.direction for item in certificates] == ["up", "up"]
    coverage = certify_declared_child_partition(
        values, certificates, view_id="same-orientation", as_of=as_of,
    )
    assert coverage.coverage_status == "source_covered_by_completed_types"
    assert coverage.qualified_source_unit_ids == tuple(
        item.unit_id for item in values
    )
    assert not coverage.strict_completed_only
    with pytest.raises(ValueError, match="exactly three"):
        build_declared_parent_view(
            values, view_id="same-orientation",
            initial_certificates=certificates, as_of=as_of,
            price_quantum=Decimal("0.01"),
        )


def test_recorded_upgrade_chain_distinguishes_local_split_from_missing_connector():
    frame = pd.read_parquet(
        Path(__file__).parents[2]
        / "fixtures/segment_local/SZ.001309_1m.parquet"
    )
    frame.attrs.update(
        structure_price_quantum="0.01", price_basis_revision="recorded-raw",
    )
    cd = build_strict_chart_cd(
        market="a", code="SZ.001309", frequency="1m",
        frame=frame, last_bar_closed=True,
    ).cd
    assert cd is not None
    level = cd.get_strict_evidence().structure.levels[0]
    upgrade_groups = tuple(
        trend for trend in level.trend_types
        if trend.kind.value == "consolidation" and len(trend.centers) > 1
    )
    assert [len(trend.centers) for trend in upgrade_groups] == [3, 3]
    first = review_declared_upgrade_split(
        upgrade_groups[0], view_id="sz001309-upgrade-a",
        as_of=cd._strict_as_of(),
    )
    assert first["status"] == "local_connected_completed_source_partition"
    assert first["schema"] == "chanlun-declared-upgrade-split-review-v2"
    assert first["center_relations"] == ["upgrade", "upgrade"]
    assert first["current_endpoint_contract_compatible"] is False
    assert len(first["connector_unit_ids"]) == 2
    assert len(first["candidate_child_types"]) == 3
    assert [
        item["directional_extrema_compatible"]
        for item in first["candidate_child_types"]
    ] == [True, False, False]
    assert first["source_partition"]["coverage_status"] == (
        "source_covered_by_declared_candidates"
    )
    assert not first["source_partition"]["strict_completed_only"]
    assert [
        unit_id for candidate in first["candidate_child_types"]
        for unit_id in candidate["source_unit_ids"]
    ] == first["source_unit_ids"]
    assert all(
        candidate["known_at"] <= first["as_of"]
        for candidate in first["candidate_child_types"]
    )
    assert first["candidate_identity_scope"] == "declared_repartition_only"
    assert not first["full_path_partition_certified"]
    assert not first["higher_center_formal"]
    assert not first["tradable"]

    source_by_id = {unit.unit_id: unit for unit in upgrade_groups[0].constituent_units}
    evidence_ref = lambda item: f"declared_upgrade_split:{item['candidate_id']}"
    pinned = tuple(
        qualify_declared_child_type(
            tuple(source_by_id[item] for item in candidate["source_unit_ids"]),
            view_id=first["view_id"], evidence_ref=evidence_ref(candidate),
            as_of=cd._strict_as_of(),
            confirmation_witness_at=datetime.fromtimestamp(
                candidate["confirmed_at"], timezone.utc,
            ),
            availability_witness_at=datetime.fromtimestamp(
                candidate["known_at"], timezone.utc,
            ),
        )
        for candidate in first["candidate_child_types"]
    )
    coverage = certify_declared_child_partition(
        upgrade_groups[0].constituent_units, pinned,
        view_id=first["view_id"], as_of=cd._strict_as_of(),
    )
    assert coverage.coverage_status == "source_covered_by_completed_types"
    assert not coverage.strict_completed_only
    pinned_unit, = combine_same_level_trends(
        tuple(item.unit for item in pinned), frozenset(),
    ).units
    canonical_unit = trend_type_to_unit(upgrade_groups[0])
    assert pinned_unit.available_at == canonical_unit.available_at
    assert (
        pinned_unit.direction, pinned_unit.start_tick, pinned_unit.end_tick,
        pinned_unit.low_tick, pinned_unit.high_tick,
        pinned_unit.market_start, pinned_unit.market_end,
    ) == (
        canonical_unit.direction, canonical_unit.start_tick,
        canonical_unit.end_tick, canonical_unit.low_tick,
        canonical_unit.high_tick, canonical_unit.market_start,
        canonical_unit.market_end,
    )
    unpinned = tuple(
        qualify_declared_child_type(
            tuple(source_by_id[item] for item in candidate["source_unit_ids"]),
            view_id=first["view_id"], evidence_ref=evidence_ref(candidate),
            as_of=cd._strict_as_of(),
        )
        for candidate in first["candidate_child_types"]
    )
    unpinned_unit, = combine_same_level_trends(
        tuple(item.unit for item in unpinned), frozenset(),
    ).units
    assert unpinned_unit.available_at < pinned_unit.available_at
    path = materialize_declared_upgrade_child_path(
        upgrade_groups[0], view_id=first["view_id"], as_of=cd._strict_as_of(),
    )
    assert path.certificates == pinned
    assert path.combined_units == (pinned_unit,)
    assert path.geometry_equivalent
    assert path.clock_not_earlier_than_protected
    payload = declared_upgrade_child_path_payload(path)
    assert payload["causal_combination"]["combined_known_at"] == int(
        canonical_unit.available_at.timestamp()
    )
    assert payload["causal_combination"]["geometry_equivalent"]
    assert not payload["causal_combination"]["full_path_partition_certified"]
    assert not payload["causal_combination"]["tradable"]
    parent_replay = replay_declared_upgrade_parent_geometry(
        path, cd.get_strict_evidence().structure.levels[1],
        as_of=cd._strict_as_of(),
    )
    assert parent_replay["status"] == (
        "parent_center_and_third_class_geometry_preserved"
    )
    assert parent_replay["center_geometry_and_clock_equal"]
    assert parent_replay["third_class_geometry_equal"]
    assert parent_replay["third_class_geometries"] == [None]
    assert parent_replay["canonical_center_ids"] != parent_replay["declared_center_ids"]
    assert not parent_replay["formal_parent_center_identity_reused"]
    assert not parent_replay["formal_third_point_reissued"]
    assert not parent_replay["tradable"]
    backdated_unit = replace(
        path.combined_units[0],
        available_at=path.protected_source_unit.available_at - timedelta(minutes=1),
    )
    backdated = replay_declared_upgrade_parent_geometry(
        replace(path, combined_units=(backdated_unit,)),
        cd.get_strict_evidence().structure.levels[1],
        as_of=cd._strict_as_of(),
    )
    assert backdated["status"] == "pending_declared_child_path_or_clock"
    with pytest.raises(ValueError, match="both witness clocks"):
        qualify_declared_child_type(
            upgrade_groups[0].constituent_units[:5],
            view_id=first["view_id"], evidence_ref="partial-clock",
            as_of=cd._strict_as_of(),
            confirmation_witness_at=pinned[0].confirmation_witness_at,
        )
    with pytest.raises(ValueError, match="witness clocks are not causal"):
        qualify_declared_child_type(
            upgrade_groups[0].constituent_units[:5],
            view_id=first["view_id"], evidence_ref="backdated-clock",
            as_of=cd._strict_as_of(),
            confirmation_witness_at=pinned[0].confirmation_witness_at,
            availability_witness_at=(
                pinned[0].confirmation_witness_at - timedelta(minutes=1)
            ),
        )
    tampered = replace(
        pinned[0],
        availability_witness_at=(
            pinned[0].availability_witness_at + timedelta(minutes=1)
        ),
    )
    with pytest.raises(ValueError, match="geometry or time differs"):
        certify_declared_child_partition(
            upgrade_groups[0].constituent_units,
            (tampered, *pinned[1:]),
            view_id=first["view_id"], as_of=cd._strict_as_of(),
        )

    second = review_declared_upgrade_split(
        upgrade_groups[1], view_id="sz001309-upgrade-b",
        as_of=cd._strict_as_of(),
    )
    assert second["status"] == "local_connected_completed_source_partition"
    assert second["current_endpoint_contract_compatible"] is False
    assert len(second["candidate_child_types"]) == 3
    assert not second["full_path_partition_certified"]
    second_path = materialize_declared_upgrade_child_path(
        upgrade_groups[1], view_id=second["view_id"], as_of=cd._strict_as_of(),
    )
    assert len(second_path.combined_units) == 1
    assert declared_upgrade_child_path_payload(second_path)["causal_combination"] is not None
    second_replay = replay_declared_upgrade_parent_geometry(
        second_path, cd.get_strict_evidence().structure.levels[1],
        as_of=cd._strict_as_of(),
    )
    assert second_replay["status"] == "parent_center_and_third_class_geometry_preserved"


def test_declared_upgrade_clock_path_also_preserves_recursive_grade_source():
    base = _two_completed_centers()
    values = tuple(
        replace(
            source, structural_level=1, source_kind=SourceKind.TREND_TYPE,
            child_ids=(f"recorded-child-{index}",),
        )
        for index, source in enumerate(base)
    )
    centers = calculate_centers(values, 1, SourceKind.TREND_TYPE)
    assembly = assemble_trend_types(centers.centers, values, 1)
    trend, = assembly.current_trends
    assert trend.kind.value == "consolidation" and len(trend.centers) == 2
    witness_at = trend.available_at + timedelta(minutes=5)
    locked = replace(
        trend, state=TrendState.LOCKED,
        confirmed_at=witness_at, available_at=witness_at,
    )
    path = materialize_declared_upgrade_child_path(
        locked, view_id="recursive-grade-upgrade", as_of=witness_at,
    )
    assert path.review["status"] == "local_connected_completed_source_partition"
    assert len(path.certificates) == 2
    assert all(
        certificate.unit.structural_level == 2
        and certificate.unit.source_kind is SourceKind.TREND_TYPE
        for certificate in path.certificates
    )
    combined, = path.combined_units
    assert combined.structural_level == 2
    assert path.geometry_equivalent
    assert path.clock_not_earlier_than_protected
    assert combined.available_at == witness_at
    payload = declared_upgrade_child_path_payload(path)
    assert not payload["full_path_partition_certified"]
    assert not payload["higher_center_formal"]


def test_protected_center_matches_same_raw_members_across_view_namespaces():
    values = _l054_segments()
    as_of = values[-1].available_at

    def build(view_id, protected=(), completed=False):
        certificates = tuple(
            qualify_declared_child_type(
                values[start : start + 3], view_id=view_id,
                evidence_ref="L054:256-274", as_of=as_of,
            )
            for start in (1, 4, 7)
        )
        departure = (
            qualify_declared_child_type(
                values[10:13], view_id=view_id,
                evidence_ref="L054:277-295", as_of=as_of,
            ) if completed else None
        )
        returned = (
            qualify_declared_child_type(
                values[13:14], view_id=view_id,
                evidence_ref="L054:283-295", as_of=as_of,
            ) if completed else None
        )
        return build_declared_parent_view(
            values, view_id=view_id, initial_certificates=certificates,
            as_of=as_of, price_quantum=Decimal("0.01"),
            departure_certificate=departure, return_certificate=returned,
            protected_parent_centers=protected,
        )

    established = build("l054:existing")
    revisited = build("l054:revisited", (established.parent_center,), completed=True)

    assert revisited.parent_center.center_id != established.parent_center.center_id
    assert revisited.protected_center_relation.status == "same_existing_center"
    assert revisited.protected_center_relation.matching_center_id == (
        established.parent_center.center_id
    )
    assert revisited.protected_center_relation.overlapping_source_unit_ids == ()
    assert revisited.third_class_points == ()


def test_parent_accepts_real_locked_trend_type_certificate_with_exact_lineage():
    _structure, assembly = structure_from_values(
        strength=divergent_strength("up")
    )
    trend = next(value for value in assembly.current_trends if value.locked)
    last = trend.constituent_units[-1]
    assert trend.direction == last.direction == "up"
    index = int(last.unit_id.rsplit("-", 1)[-1]) + 1
    end = last.end_tick
    prices = (end, end - 10, end - 2, end - 12,
              end + 5, end - 5, end + 7)
    extra = tuple(
        unit(index + offset, "up" if finish > start else "down", start, finish)
        for offset, (start, finish) in enumerate(zip(prices, prices[1:]))
    )
    source = (*trend.constituent_units, *extra)
    as_of = extra[-1].available_at
    certified = qualify_locked_trend_type(
        trend, view_id="strict-plus-source", as_of=as_of
    )
    author_children = tuple(
        qualify_declared_child_type(
            extra[start : start + 3], view_id="strict-plus-source",
            evidence_ref="synthetic-fixture", as_of=as_of,
        )
        for start in (0, 3)
    )
    result = build_declared_parent_view(
        source, view_id="strict-plus-source",
        initial_certificates=(certified, *author_children),
        as_of=as_of, price_quantum=Decimal("0.01"),
    )

    assert certified.qualification_kind == "strict_locked_trend_type"
    assert certified.unit.unit_id == trend.trend_id
    assert certified.source_unit_ids == tuple(
        value.unit_id for value in trend.constituent_units
    )
    assert result.parent_center.structural_level == 1
    assert result.parent_center.initial_units[0] == certified.unit
    payload = declared_parent_view_payload(result)
    assert payload["child_certificates"][0]["qualification_kind"] == (
        "strict_locked_trend_type"
    )
    assert payload["schema"] == "chanlun-declared-parent-view-v2"
    assert payload["qualification_scope"] == "mixed_declared_view_only"
    forged = replace(
        certified,
        unit=replace(certified.unit, high_tick=certified.unit.high_tick + 1),
    )
    with pytest.raises(ValueError, match="geometry or time differs"):
        build_declared_parent_view(
            source, view_id="strict-plus-source",
            initial_certificates=(forged, *author_children),
            as_of=as_of, price_quantum=Decimal("0.01"),
        )


def test_protected_center_gate_keeps_disjoint_later_members_independent():
    original = _l054_segments()
    vertices = (50, 20, 35, 15, 30, 10, 25, 5, 20, 0)
    later_units = tuple(
        unit(14 + offset, "up" if finish > start else "down", start, finish)
        for offset, (start, finish) in enumerate(zip(vertices, vertices[1:]))
    )
    source = (*original, *later_units)
    as_of = source[-1].available_at

    def certs(view_id, groups):
        return tuple(
            qualify_declared_child_type(
                source[start:end], view_id=view_id,
                evidence_ref="synthetic-fixture", as_of=as_of,
            )
            for start, end in groups
        )

    older = build_declared_parent_view(
        source, view_id="old", initial_certificates=certs(
            "old", ((1, 4), (4, 7), (7, 10))
        ), as_of=as_of, price_quantum=Decimal("0.01"),
    )
    later = build_declared_parent_view(
        source, view_id="later", initial_certificates=certs(
            "later", ((14, 17), (17, 20), (20, 23))
        ), as_of=as_of, price_quantum=Decimal("0.01"),
        protected_parent_centers=(older.parent_center,),
    )

    assert later.protected_center_relation.status == "disjoint_new_center_candidate"
    assert later.parent_path_audit.parent_path_status == "source_partition_pending"
    assert later.protected_center_relation.overlapping_source_unit_ids == ()
    assert (later.parent_center.zd_tick, later.parent_center.zg_tick) == (15, 25)


def test_general_view_rejects_reused_or_skipped_members():
    values = _l054_segments()
    invalid = (
        ((1, 2, 3), (3, 4, 5)),
        ((1, 2, 3), (5, 6, 7)),
    )
    for groups in invalid:
        try:
            materialize_associative_view(
                values,
                view_id="bad-view",
                component_member_ids=_ids(values, groups),
                as_of=values[-1].available_at,
            )
        except ValueError:
            pass
        else:
            raise AssertionError("an associative view may not reuse or skip members")
