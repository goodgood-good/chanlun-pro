from __future__ import annotations

from dataclasses import replace
from datetime import timedelta

import pytest

from chanlun.decision_support.trading_system.timeframe_override import (
    independent_timeframe_override,
)
from chanlun.decision_support.trading_system.structure_adapter import (
    extract_confirmed_points,
)
from tests.trading_system.strict_helpers import (
    DEFAULT_CLOSED_AT,
    strict_evidence_result,
    strict_point,
)


OBSERVED = DEFAULT_CLOSED_AT


def point(frequency: str):
    points = extract_confirmed_points(
        strict_evidence_result(
            source_frequency=frequency,
            confirmed_points=(strict_point("3buy"),),
        ),
        code="SZ.000001",
        source_frequency=frequency,
        as_of=OBSERVED,
    )
    return points[0]


def test_independent_timeframe_mapping_is_30m_5m_1m_and_hashed() -> None:
    override = independent_timeframe_override()

    assert override.frequency_for("L0") == "30m"
    assert override.frequency_for("L1") == "5m"
    assert override.frequency_for("L2") == "1m"
    assert override.direct_recursive_relation_required is False
    assert override.parameter_set_id.startswith("sha256:")
    assert override.live_status == "LIVE_DISABLED"


def test_each_level_accepts_only_its_independent_chart_level_zero() -> None:
    override = independent_timeframe_override()

    assert override.validate_point(
        level="L0", point=point("30m"), observed_at=OBSERVED
    ).source_frequency == "30m"
    with pytest.raises(ValueError, match="frequency"):
        override.validate_point(
            level="L0", point=point("1m"), observed_at=OBSERVED
        )
    with pytest.raises(ValueError, match="level-zero"):
        override.validate_point(
            level="L1",
            point=replace(point("5m"), recursive_level=1),
            observed_at=OBSERVED,
        )


def test_future_point_is_rejected() -> None:
    override = independent_timeframe_override()
    future = replace(
        point("1m"),
        available_at=OBSERVED + timedelta(minutes=1),
        confirmed_at=OBSERVED + timedelta(minutes=1),
    )

    with pytest.raises(ValueError, match="Future|future"):
        override.validate_point(level="L2", point=future, observed_at=OBSERVED)
