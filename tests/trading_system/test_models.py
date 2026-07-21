from dataclasses import replace
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from chanlun.decision_support.trading_system.models import (
    StructuralPoint,
    build_point_id,
)


CN = ZoneInfo("Asia/Shanghai")
AT = datetime(2026, 7, 20, 10, 31, tzinfo=CN)


def _point(*, level: int, price_basis_revision: str) -> StructuralPoint:
    available_at = AT + timedelta(minutes=2)
    point_id = build_point_id(
        code="SZ.000001",
        price_basis_revision=price_basis_revision,
        point_type="1buy",
        source_frequency="1m",
        tower="formal",
        recursive_level=level,
        anchor_at=AT,
        center_id="center-a",
        parent_point_id=None,
    )
    return StructuralPoint(
        point_id=point_id,
        code="SZ.000001",
        point_type="1buy",
        side="buy",
        status="confirmed",
        variant="standard",
        source_frequency="1m",
        price_basis_revision=price_basis_revision,
        tower="formal",
        recursive_level=level,
        anchor_at=AT,
        confirmed_at=AT + timedelta(minutes=1),
        available_at=available_at,
        structure_anchor_price=10.20,
        structure_invalidation_price=9.80,
        center_id="center-a",
        center_zd=9.40,
        center_zg=9.80,
        center_ordinal=1,
        divergence_kind="trend",
        parent_point_id=None,
        evidence_codes=("trend_divergence",),
    )


def test_point_identity_distinguishes_price_basis_and_recursive_level() -> None:
    raw_l0 = _point(level=0, price_basis_revision="raw-v1")
    raw_l1 = _point(level=1, price_basis_revision="raw-v1")
    adjusted_l0 = _point(level=0, price_basis_revision="forward-v1")

    assert len({raw_l0.point_id, raw_l1.point_id, adjusted_l0.point_id}) == 3
    assert raw_l0.structure_key == ("formal", 0, "center-a")
    assert raw_l0.confirmed is True


def test_confirmed_point_requires_basis_and_causal_availability() -> None:
    point = _point(level=0, price_basis_revision="raw-v1")

    with pytest.raises(ValueError, match="price_basis_revision is required"):
        replace(point, price_basis_revision="")
    with pytest.raises(ValueError, match="available_at cannot precede confirmed_at"):
        replace(point, available_at=AT)
    with pytest.raises(ValueError, match="invalid structure identity"):
        replace(point, tower="bi")
