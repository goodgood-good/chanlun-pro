from dataclasses import replace
from datetime import datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest

from chanlun.core.strict_structure.identity import build_strict_evidence_revision
from chanlun.core.strict_structure.models import (
    CenterLevelResult,
    StrictEvidenceResult,
    StrictStructureResult,
)
from chanlun.decision_support.trading_system.structure_adapter import (
    extract_confirmed_points,
)
from tests.core.strict_structure.signal_helpers import confirmed_point
from tests.core.strict_structure.helpers import (
    completed_down_center,
    completed_up_center,
    engine_for,
    structure_for,
)


CN = ZoneInfo("Asia/Shanghai")
AS_OF = datetime(2026, 7, 20, 15, 0, tzinfo=CN)
SIX_POINT_TYPES = ("1buy", "2buy", "3buy", "1sell", "2sell", "3sell")


def _aware_point(point_type: str):
    anchor_at = AS_OF - timedelta(minutes=30)
    confirmed_at = AS_OF - timedelta(minutes=20)
    available_at = AS_OF - timedelta(minutes=10)
    raw = confirmed_point(point_type=point_type)
    divergence = raw.divergence
    if divergence is not None:
        divergence = replace(divergence, available_at=available_at)
    return replace(
        raw,
        anchor_at=anchor_at,
        confirmed_at=confirmed_at,
        available_at=available_at,
        divergence=divergence,
    )


def _evidence(
    points=(),
    *,
    symbol: str = "SZ.000001",
    source_frequency: str = "1m",
    structure=None,
) -> StrictEvidenceResult:
    structure = structure or StrictStructureResult(
        schema_version="chanlun-structure/v3",
        price_basis_revision="test-raw-v1",
        levels=(),
    )
    observations = CenterLevelResult(
        structural_level=0,
        price_basis_revision="test-raw-v1",
        centers=(),
        previews=(),
        events=(),
        locked_unit_count=0,
        replay_from=0,
    )
    revision = build_strict_evidence_revision(
        symbol=symbol,
        source_frequency=source_frequency,
        price_basis_revision="test-raw-v1",
        strict_config_revision="strict-config-v1",
        structure=structure,
        confirmed_points=points,
    )
    return StrictEvidenceResult(
        symbol=symbol,
        source_frequency=source_frequency,
        source_closed_at=AS_OF,
        price_basis_revision="test-raw-v1",
        structure_price_quantum=Decimal("0.01"),
        strict_config_revision="strict-config-v1",
        structure_revision=revision,
        structure=structure,
        stroke_center_observations=observations,
        confirmed_points=points,
        approaching_points=(),
    )


def test_adapter_reads_only_strict_points() -> None:
    raw = _aware_point("1buy")
    points = extract_confirmed_points(
        _evidence((raw,)),
        code="SZ.000001",
        source_frequency="1m",
        as_of=AS_OF,
    )

    assert len(points) == 1
    assert points[0].tower == "formal"
    assert points[0].available_at == raw.available_at
    assert points[0].price_basis_revision == raw.price_basis_revision
    assert points[0].structure_anchor_price == float(raw.structure_anchor_price)


def test_adapter_rejects_point_available_after_as_of() -> None:
    raw = _aware_point("1buy")
    evidence = _evidence((raw,))
    object.__setattr__(raw, "available_at", AS_OF + timedelta(minutes=1))

    with pytest.raises(ValueError, match="available after as_of"):
        extract_confirmed_points(
            evidence,
            code="SZ.000001",
            source_frequency="1m",
            as_of=AS_OF,
        )


def test_all_six_types_survive_adapter_without_category_collapse() -> None:
    up = completed_up_center(0)
    down = completed_down_center(10)
    structure = structure_for(up, down)
    third_points = engine_for(up, down).third_class_points()
    raw_points = tuple(
        _aware_point(point_type)
        for point_type in ("1buy", "2buy", "1sell", "2sell")
    ) + third_points

    points = extract_confirmed_points(
        _evidence(raw_points, structure=structure),
        code="SZ.000001",
        source_frequency="1m",
        as_of=AS_OF,
    )

    assert {point.point_type for point in points} == set(SIX_POINT_TYPES)
    assert len(points) == 6
    assert next(
        point for point in points if point.point_type == "3buy"
    ).center_ordinal == 1


def test_adapter_rejects_mismatched_or_future_snapshot_context() -> None:
    evidence = _evidence((_aware_point("1buy"),))

    with pytest.raises(ValueError, match="strict evidence context mismatch"):
        extract_confirmed_points(
            evidence,
            code="SH.600000",
            source_frequency="1m",
            as_of=AS_OF,
        )
    with pytest.raises(ValueError, match="snapshot is after as_of"):
        extract_confirmed_points(
            evidence,
            code="SZ.000001",
            source_frequency="1m",
            as_of=AS_OF - timedelta(minutes=1),
        )
