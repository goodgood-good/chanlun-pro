from dataclasses import replace
from datetime import timedelta

import pytest

from chanlun.core.strict_structure.models import (
    SourceKind,
    StrictPointStatus,
    StrictPointVariant,
)
from tests.core.strict_structure.signal_helpers import confirmed_point


ONE_MINUTE = timedelta(minutes=1)


def test_confirmed_point_requires_available_at_not_before_confirmation():
    point = confirmed_point()
    with pytest.raises(ValueError, match="available_at must not precede confirmed_at"):
        replace(point, available_at=point.confirmed_at - ONE_MINUTE)


def test_approaching_point_cannot_claim_confirmation():
    point = confirmed_point()
    with pytest.raises(ValueError, match="approaching point cannot carry confirmed_at"):
        replace(
            point,
            status=StrictPointStatus.APPROACHING,
            missing_conditions=("terminal_unit_locked",),
        )


def test_stroke_observation_cannot_become_strict_point():
    with pytest.raises(ValueError, match="stroke observation is not tradable"):
        replace(confirmed_point(), source_kind=SourceKind.STROKE_OBSERVATION)


def test_boundary_touch_variant_is_reserved_for_third_class():
    with pytest.raises(ValueError, match="boundary touch requires third class"):
        replace(
            confirmed_point(point_type="1buy"),
            variant=StrictPointVariant.BOUNDARY_TOUCH,
        )


def test_weak_divergence_variant_requires_second_class_consolidation_divergence():
    with pytest.raises(ValueError, match="weak divergence requires second class"):
        replace(
            confirmed_point(point_type="1buy"),
            variant=StrictPointVariant.WEAK_DIVERGENCE,
        )
    with pytest.raises(
        ValueError,
        match="weak divergence requires confirmed consolidation divergence",
    ):
        replace(
            confirmed_point(point_type="2buy"),
            variant=StrictPointVariant.WEAK_DIVERGENCE,
            divergence=None,
        )


def test_point_class_requires_compatible_variant_parent_and_divergence():
    with pytest.raises(ValueError, match="first class requires standard trend divergence"):
        replace(
            confirmed_point(point_type="1buy"),
            variant=StrictPointVariant.STRICT,
        )
    with pytest.raises(ValueError, match="second class requires parent point"):
        replace(confirmed_point(point_type="2buy"), parent_point_id=None)
    with pytest.raises(ValueError, match="third class requires center ordinal"):
        replace(confirmed_point(point_type="3buy"), center_ordinal=None)
    with pytest.raises(ValueError, match="center ordinal is reserved for third class"):
        replace(confirmed_point(point_type="1buy"), center_ordinal=1)


def test_point_availability_must_cover_divergence_evidence():
    point = confirmed_point(point_type="1buy")
    late = replace(
        point.divergence,
        available_at=point.available_at + ONE_MINUTE,
    )
    with pytest.raises(ValueError, match="point availability must cover divergence"):
        replace(point, divergence=late)


def test_point_identity_is_namespaced_by_price_basis_revision():
    raw = confirmed_point(price_basis_revision="raw-v1")
    rebased = confirmed_point(price_basis_revision="corp-action-2026-07-20")
    assert raw.point_id != rebased.point_id
