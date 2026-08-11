from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from chanlun.core.strict_structure.identity import build_strict_evidence_revision
from chanlun.core.strict_structure.divergence import merge_formal_divergence_ledger
from chanlun.core.strict_structure.models import (
    CenterLevelResult,
    SourceKind,
    StrictEvidenceResult,
    StrictLevelResult,
    StrictPointStatus,
    StrictStructureResult,
)
from tests.core.strict_structure.signal_helpers import confirmed_point
from tests.core.strict_structure.helpers import (
    completed_up_center,
    engine_for,
    ongoing_center,
    structure_for,
    unit,
)


NOW = datetime(2026, 7, 20, 8, 0, tzinfo=timezone.utc)


def empty_structure(*, with_level=False):
    levels = ()
    if with_level:
        anchor = replace(
            unit(0, "up", 100, 100),
            unit_id="anchor-unit",
            market_start=NOW - timedelta(minutes=1),
            market_end=NOW,
            confirmed_at=NOW + timedelta(minutes=1),
            available_at=NOW + timedelta(minutes=1),
        )
        center_result = CenterLevelResult(
            structural_level=0,
            price_basis_revision="test-raw",
            centers=(),
            previews=(),
            events=(),
            locked_unit_count=1,
            replay_from=0,
        )
        levels = (
            StrictLevelResult(
                structural_level=0,
                units=(anchor,),
                center_result=center_result,
                trend_types=(),
                completed_trends=(),
            ),
        )
    return StrictStructureResult(
        schema="chanlun-structure",
        price_basis_revision="test-raw",
        levels=levels,
    )


def observation_result(*, replay_from=0):
    return CenterLevelResult(
        structural_level=0,
        price_basis_revision="test-raw",
        centers=(),
        previews=(),
        events=(),
        locked_unit_count=0,
        replay_from=replay_from,
    )


def aware_confirmed_point(point_type="3buy"):
    point = confirmed_point(point_type=point_type)
    divergence = point.divergence
    if divergence is not None:
        divergence = replace(
            divergence,
            anchor_at=NOW,
            anchor_tick=point.anchor_tick,
            confirmed_at=NOW + timedelta(minutes=1),
            available_at=NOW + timedelta(minutes=2),
        )
    return replace(
        point,
        anchor_at=NOW,
        confirmed_at=NOW + timedelta(minutes=1),
        available_at=NOW + timedelta(minutes=2),
        divergence=divergence,
    )


def approaching_point(sequence=1):
    point = aware_confirmed_point()
    return replace(
        point,
        point_id=f"approaching-{sequence}",
        status=StrictPointStatus.APPROACHING,
        confirmed_at=None,
        missing_conditions=("terminal_unit_locked",),
    )


def evidence_bundle(
    *,
    structure=None,
    stroke_observations=None,
    confirmed_points=(),
    approaching_points=(),
    divergences=(),
):
    formal = structure or empty_structure(
        with_level=bool(confirmed_points or approaching_points)
    )
    divergences = merge_formal_divergence_ledger(
        formal,
        confirmed_points,
        divergences,
    )
    revision = build_strict_evidence_revision(
        symbol="SZ.000001",
        source_frequency="1m",
        price_basis_revision="test-raw",
        strict_config_revision="strict-config",
        structure=formal,
        confirmed_points=confirmed_points,
        divergences=divergences,
    )
    return StrictEvidenceResult(
        symbol="SZ.000001",
        source_frequency="1m",
        source_closed_at=NOW + timedelta(hours=1),
        price_basis_revision="test-raw",
        structure_price_quantum=Decimal("0.01"),
        strict_config_revision="strict-config",
        structure_revision=revision,
        structure=formal,
        stroke_center_observations=stroke_observations or observation_result(),
        confirmed_points=confirmed_points,
        approaching_points=approaching_points,
        divergences=divergences,
    )


def test_evidence_revision_excludes_observation_only_changes():
    before = evidence_bundle(stroke_observations=observation_result(replay_from=1))
    after = evidence_bundle(stroke_observations=observation_result(replay_from=9))
    assert build_strict_evidence_revision(**before.formal_inputs) == (
        build_strict_evidence_revision(**after.formal_inputs)
    )


def test_evidence_revision_excludes_approaching_only_changes():
    before = evidence_bundle(approaching_points=(approaching_point(1),))
    after = evidence_bundle(approaching_points=(approaching_point(2),))
    assert build_strict_evidence_revision(**before.formal_inputs) == (
        build_strict_evidence_revision(**after.formal_inputs)
    )


def test_evidence_revision_and_atomic_bundle_include_independent_divergences():
    divergence = aware_confirmed_point("1buy").divergence
    assert divergence is not None
    base = evidence_bundle(structure=empty_structure(with_level=True))
    changed = evidence_bundle(
        structure=empty_structure(with_level=True),
        divergences=(divergence,),
    )
    assert changed.divergences == (divergence,)
    assert build_strict_evidence_revision(**base.formal_inputs) != (
        build_strict_evidence_revision(**changed.formal_inputs)
    )


def test_evidence_revision_changes_for_formal_point_or_structure_change():
    base = evidence_bundle()
    changed_point = evidence_bundle(
        confirmed_points=(aware_confirmed_point("1buy"),)
    )
    changed_structure = evidence_bundle(structure=empty_structure(with_level=True))
    assert len(
        {
            build_strict_evidence_revision(**base.formal_inputs),
            build_strict_evidence_revision(**changed_point.formal_inputs),
            build_strict_evidence_revision(**changed_structure.formal_inputs),
        }
    ) == 3


def test_atomic_bundle_rejects_formal_point_tampering_with_stale_revision():
    point = aware_confirmed_point("1buy")
    base = evidence_bundle(confirmed_points=(point,))
    tampered = replace(
        point,
        evidence_codes=(*point.evidence_codes, "tampered-proof-code"),
    )

    with pytest.raises(ValueError, match="structure_revision does not match"):
        replace(base, confirmed_points=(tampered,))


def test_atomic_bundle_requires_embedded_divergence_in_top_level_ledger():
    point = aware_confirmed_point("1buy")
    base = evidence_bundle(confirmed_points=(point,))
    revision_without_ledger = build_strict_evidence_revision(
        symbol=base.symbol,
        source_frequency=base.source_frequency,
        price_basis_revision=base.price_basis_revision,
        strict_config_revision=base.strict_config_revision,
        structure=base.structure,
        confirmed_points=base.confirmed_points,
        divergences=(),
    )

    with pytest.raises(ValueError, match="embedded divergence is missing"):
        replace(
            base,
            divergences=(),
            structure_revision=revision_without_ledger,
        )


def test_atomic_bundle_rejects_future_structure_without_any_points():
    future_unit = replace(
        unit(0, "up", 100, 110),
        market_start=NOW + timedelta(hours=2),
        market_end=NOW + timedelta(hours=2, minutes=5),
        confirmed_at=NOW + timedelta(hours=2, minutes=10),
        available_at=NOW + timedelta(hours=2, minutes=10),
    )
    structure = StrictStructureResult(
        schema="chanlun-structure",
        price_basis_revision="test-raw",
        levels=(
            StrictLevelResult(
                structural_level=0,
                units=(future_unit,),
                center_result=CenterLevelResult(
                    structural_level=0,
                    price_basis_revision="test-raw",
                    centers=(),
                    previews=(),
                    events=(),
                    locked_unit_count=1,
                    replay_from=0,
                ),
                trend_types=(),
                completed_trends=(),
            ),
        ),
    )

    with pytest.raises(ValueError, match="future unit evidence"):
        evidence_bundle(structure=structure)


def test_atomic_bundle_rejects_missing_recursive_unit_lineage():
    lower = unit(0, "up", 100, 110)
    higher = replace(
        lower,
        unit_id="l1-carrier",
        structural_level=1,
        source_kind=SourceKind.TREND_TYPE,
        child_ids=("missing-l0-child",),
    )

    def level(number, units):
        return StrictLevelResult(
            structural_level=number,
            units=units,
            center_result=CenterLevelResult(
                structural_level=number,
                price_basis_revision="test-raw",
                centers=(),
                previews=(),
                events=(),
                locked_unit_count=len(units),
                replay_from=0,
            ),
            trend_types=(),
            completed_trends=(),
        )

    structure = StrictStructureResult(
        schema="chanlun-structure",
        price_basis_revision="test-raw",
        levels=(level(0, (lower,)), level(1, (higher,))),
    )

    with pytest.raises(ValueError, match="exactly replay prior locked trends"):
        evidence_bundle(structure=structure)


def test_evidence_revision_is_order_independent_for_confirmed_point_ledger():
    one = aware_confirmed_point("1buy")
    two = aware_confirmed_point("1sell")
    forward = build_strict_evidence_revision(
        **evidence_bundle(confirmed_points=(one, two)).formal_inputs
    )
    reverse = build_strict_evidence_revision(
        **evidence_bundle(confirmed_points=(two, one)).formal_inputs
    )
    assert forward == reverse


def test_evidence_revision_rejects_duplicate_ids_and_naive_datetimes():
    point = aware_confirmed_point("1buy")
    duplicate_inputs = dict(evidence_bundle().formal_inputs)
    duplicate_inputs["confirmed_points"] = (point, point)
    with pytest.raises(ValueError, match="duplicate confirmed point id"):
        build_strict_evidence_revision(**duplicate_inputs)
    naive_inputs = dict(evidence_bundle().formal_inputs)
    aware = confirmed_point(point_type="3buy")
    naive_inputs["confirmed_points"] = (
        replace(
            aware,
            anchor_at=aware.anchor_at.replace(tzinfo=None),
            confirmed_at=aware.confirmed_at.replace(tzinfo=None),
            available_at=aware.available_at.replace(tzinfo=None),
        ),
    )
    with pytest.raises(ValueError, match="timezone-aware"):
        build_strict_evidence_revision(**naive_inputs)


def test_evidence_rejects_completed_center_without_matching_third_point():
    completed = completed_up_center()
    with pytest.raises(
        ValueError,
        match="completed centers and third-class points must match",
    ):
        evidence_bundle(
            structure=structure_for(completed),
            confirmed_points=(),
        )


def test_center_identity_cannot_be_detached_from_its_seed_evidence():
    with pytest.raises(ValueError, match="immutable center seed"):
        replace(completed_up_center(), center_id="forged-center-id")


def test_confirmed_and_approaching_ledgers_cannot_share_one_point_identity():
    confirmed = aware_confirmed_point("1buy")
    approaching = replace(
        confirmed,
        status=StrictPointStatus.APPROACHING,
        confirmed_at=None,
        missing_conditions=("terminal_unit_locked",),
    )
    with pytest.raises(ValueError, match="must be disjoint"):
        evidence_bundle(
            confirmed_points=(confirmed,),
            approaching_points=(approaching,),
        )


def test_evidence_rejects_third_point_for_ongoing_center():
    ongoing = ongoing_center()
    completed = completed_up_center()
    assert completed.center_id == ongoing.center_id
    points = engine_for(completed).third_class_points()
    assert len(points) == 1
    with pytest.raises(
        ValueError,
        match="strict point anchor is missing from its level",
    ):
        evidence_bundle(
            structure=structure_for(ongoing),
            confirmed_points=(points[0],),
        )


def test_evidence_accepts_exactly_one_matching_point_per_completed_center():
    completed = completed_up_center()
    points = engine_for(completed).third_class_points()
    value = evidence_bundle(
        structure=structure_for(completed),
        confirmed_points=points,
    )
    assert value.confirmed_points == points
