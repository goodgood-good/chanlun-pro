"""Causal level-two parent certificate over three completed level-one types.

D:\\缠论\\chanlun_lesson_corpus\\L018_不被面首的雏男是不完美的。
(2006-12-26150558).md author lines 31-55; L054_一个具体走势的分析
(2007-05-24013731).md author lines 145-157. Synthetic values are not
original chart evidence.
"""

from dataclasses import replace
from decimal import Decimal

import pytest
import pandas as pd

from chanlun.cl_utils.strict_chart_runtime import build_strict_chart_cd
from chanlun.core.strict_structure.center_machine import calculate_centers
from chanlun.core.strict_structure.general_decomposition import (
    audit_declared_parent_path,
    build_declared_parent_view,
    certify_declared_child_partition,
    declared_parent_view_payload,
    declared_partition_coverage_payload,
    declared_parent_path_audit_payload,
    qualify_locked_trend_type,
)
from chanlun.core.strict_structure.models import SourceKind, TrendState
from chanlun.core.strict_structure.trend_assembler import assemble_trend_types
from tests.core.strict_structure.helpers import unit


def _higher_level_types(structural_level=1):
    source = SourceKind.TREND_TYPE

    def child(index, direction, start, end):
        return unit(
            index, direction, start, end,
            structural_level=structural_level, source_kind=source,
        )

    first = (
        child(0, "up", 80, 120),
        child(1, "down", 120, 100),
        child(2, "up", 100, 115),
        child(3, "down", 115, 105),
        child(4, "up", 105, 130),
        child(5, "down", 130, 116),
    )
    second = (
        first[5],
        child(6, "up", 116, 125),
        child(7, "down", 125, 110),
        child(8, "up", 110, 120),
        child(9, "down", 120, 100),
        child(10, "up", 100, 115),
    )
    third = (
        second[5],
        child(11, "down", 115, 105),
        child(12, "up", 105, 120),
        child(13, "down", 120, 110),
        child(14, "up", 110, 135),
        child(15, "down", 135, 116),
    )

    def lock_type(values):
        center_result = calculate_centers(values, structural_level, source)
        assembly = assemble_trend_types(
            center_result.centers, values, structural_level
        )
        assert len(center_result.centers) == len(assembly.current_trends) == 1
        assert center_result.centers[0].state.value == "completed"
        assert assembly.current_trends[0].state is TrendState.COMPLETE
        return replace(assembly.current_trends[0], state=TrendState.LOCKED)

    return (lock_type(first), lock_type(second), lock_type(third)), third[-1].available_at


@pytest.mark.parametrize("source_level", [1, 2])
def test_three_locked_higher_types_form_the_next_level_parent_candidate(source_level):
    trends, as_of = _higher_level_types(source_level)
    source_units = tuple(
        unit for trend in trends for unit in trend.constituent_units
    )
    certificates = tuple(
        qualify_locked_trend_type(trend, view_id="level-two", as_of=as_of)
        for trend in trends
    )
    view = build_declared_parent_view(
        source_units, view_id="level-two",
        initial_certificates=certificates,
        as_of=as_of, price_quantum=Decimal("0.01"),
    )

    assert [trend.direction for trend in trends] == ["up", "down", "up"]
    assert all(
        left.constituent_units[-1].end_tick == right.constituent_units[0].start_tick
        for left, right in zip(trends, trends[1:])
    )
    assert view.parent_center.structural_level == source_level + 1
    assert view.parent_center.initial_units == tuple(
        certificate.unit for certificate in certificates
    )
    assert (view.parent_center.zd_tick, view.parent_center.zg_tick) == (100, 130)
    assert view.protected_center_relation.status == "no_protected_centers_supplied"
    assert view.parent_path_audit.parent_path_status == "structurally_consistent"
    assert view.third_class_points == ()
    assert view.partition_coverage.coverage_status == "source_covered_by_completed_types"
    assert view.partition_coverage.strict_completed_only
    assert view.partition_coverage.qualified_source_unit_ids == tuple(
        unit.unit_id for unit in source_units
    )
    assert view.partition_coverage.unqualified_prefix_unit_ids == ()
    assert view.partition_coverage.unqualified_tail_unit_ids == ()
    payload = declared_parent_view_payload(view)
    assert payload["source_structural_level"] == source_level
    assert payload["parent_center"]["structural_level"] == source_level + 1
    assert payload["parent_center"]["initial_child_type_ids"] == [
        trend.trend_id for trend in trends
    ]
    assert payload["source_partition"]["coverage_status"] == (
        "source_covered_by_completed_types"
    )
    assert not payload["source_partition"]["general_view_legality_proven"]
    assert payload["parent_path_audit"]["parent_path_status"] == (
        "structurally_consistent"
    )
    assert not payload["canonical_same_level_output"] and not payload["tradable"]
    protected = build_declared_parent_view(
        source_units, view_id="level-two",
        initial_certificates=certificates,
        as_of=as_of, price_quantum=Decimal("0.01"),
        protected_parent_centers=(view.parent_center,),
    )
    assert protected.protected_center_relation.status == "same_existing_center"
    assert protected.protected_center_relation.matching_center_id == (
        view.parent_center.center_id
    )


def test_level_two_parent_rejects_future_certificate_and_missing_member():
    trends, as_of = _higher_level_types()
    source_units = tuple(unit for trend in trends for unit in trend.constituent_units)
    before_third_is_known = trends[1].available_at
    with pytest.raises(ValueError, match="locked and causally available"):
        qualify_locked_trend_type(
            trends[2], view_id="too-early", as_of=before_third_is_known
        )
    certificates = tuple(
        qualify_locked_trend_type(trend, view_id="level-two", as_of=as_of)
        for trend in trends
    )
    with pytest.raises(ValueError, match="missing source members"):
        build_declared_parent_view(
            source_units[:-1], view_id="level-two",
            initial_certificates=certificates,
            as_of=as_of, price_quantum=Decimal("0.01"),
        )

    first = source_units[0]
    naive_first = replace(
        first,
        market_start=first.market_start.replace(tzinfo=None),
        market_end=first.market_end.replace(tzinfo=None),
        confirmed_at=first.confirmed_at.replace(tzinfo=None),
        available_at=first.available_at.replace(tzinfo=None),
    )
    with pytest.raises(ValueError, match="timezone-aware completed units"):
        build_declared_parent_view(
            (naive_first, *source_units[1:]), view_id="level-two",
            initial_certificates=certificates,
            as_of=as_of, price_quantum=Decimal("0.01"),
        )


def test_recorded_parent_upgrade_waits_for_second_formal_center():
    frame = pd.read_parquet("tests/fixtures/SH.600519_5m.parquet").head(10000)
    frame.attrs.update(
        structure_price_quantum="0.01", price_basis_revision="recorded-raw"
    )
    runtime = build_strict_chart_cd(
        market="a", code="SH.600519", frequency="5m", frame=frame
    )
    levels = runtime.cd.get_strict_evidence().structure.levels
    assert len(levels) >= 2
    parent_units = levels[1].units
    parent, = levels[1].center_result.centers
    assert parent.state.value == "ongoing"
    assert all(unit.locked for unit in parent_units[:-1])
    assert not parent_units[-1].locked
    assert parent.source_kind is SourceKind.TREND_TYPE
    assert parent.initial_units == tuple(parent.body_units[:3])
    assert len(parent_units) == 7


def test_recorded_locked_types_cover_a_prefix_with_explicit_pending_tail():
    frame = pd.read_parquet("tests/fixtures/SH.600519_5m.parquet").head(10000)
    frame.attrs.update(
        structure_price_quantum="0.01", price_basis_revision="recorded-raw"
    )
    runtime = build_strict_chart_cd(
        market="a", code="SH.600519", frequency="5m", frame=frame
    )
    level = runtime.cd.get_strict_evidence().structure.levels[0]
    source = tuple(unit for unit in level.units if unit.locked)
    as_of = runtime.cd._strict_as_of()
    certificates = tuple(
        qualify_locked_trend_type(
            trend, view_id="recorded-prefix", as_of=as_of
        )
        for trend in level.trend_types if trend.locked
    )
    coverage = certify_declared_child_partition(
        source, certificates, view_id="recorded-prefix", as_of=as_of
    )

    assert len(source) == 90 and len(certificates) == 6
    assert coverage.coverage_status == "prefix_and_tail_unqualified"
    assert coverage.strict_completed_only
    assert coverage.unqualified_prefix_unit_ids == (source[0].unit_id,)
    assert coverage.qualified_source_unit_ids == tuple(
        unit.unit_id for unit in source[1:79]
    )
    assert coverage.unqualified_tail_unit_ids == tuple(
        unit.unit_id for unit in source[79:]
    )
    canonical_parent = runtime.cd.get_strict_evidence().structure.levels[1].center_result.centers
    path_audit = audit_declared_parent_path(
        source, certificates, view_id="recorded-prefix", as_of=as_of,
        protected_parent_centers=canonical_parent,
    )
    assert path_audit.parent_path_status == "source_partition_pending"
    assert path_audit.parent_center_ids == tuple(
        center.center_id for center in canonical_parent
    )
    assert [relation.status for relation in path_audit.protected_relations] == [
        "same_existing_center"
    ]
    assert path_audit.connector_child_type_ids == ()
    audit_payload = declared_parent_path_audit_payload(path_audit)
    assert audit_payload["source_partition"]["coverage_status"] == "prefix_and_tail_unqualified"
    assert not audit_payload["general_view_legality_proven"]
    shifted = audit_declared_parent_path(
        source, certificates[1:4], view_id="recorded-prefix", as_of=as_of,
        protected_parent_centers=canonical_parent,
    )
    assert shifted.parent_path_status == "protected_repartition_required"
    shifted_payload = declared_parent_path_audit_payload(shifted)
    assert shifted_payload["protected_relations"][0]["overlapping_center_ids"]
    assert shifted_payload["protected_relations"][0][
        "overlapping_source_unit_ids"
    ]
    payload = declared_partition_coverage_payload(coverage)
    assert not payload["general_view_legality_proven"] and not payload["tradable"]

    one_minute = pd.read_parquet("tests/fixtures/SZ.002299_1m.parquet").head(3000)
    one_minute.attrs.update(
        structure_price_quantum="0.01", price_basis_revision="recorded-raw"
    )
    one_runtime = build_strict_chart_cd(
        market="a", code="SZ.002299", frequency="1m", frame=one_minute
    )
    one_level = one_runtime.cd.get_strict_evidence().structure.levels[0]
    one_source = tuple(unit for unit in one_level.units if unit.locked)
    empty = certify_declared_child_partition(
        one_source, (), view_id="no-complete-type",
        as_of=one_runtime.cd._strict_as_of(),
    )
    assert len(one_source) == 37
    assert empty.coverage_status == "no_completed_types"
    assert not empty.strict_completed_only
    assert empty.unqualified_tail_unit_ids == tuple(
        unit.unit_id for unit in one_source
    )
