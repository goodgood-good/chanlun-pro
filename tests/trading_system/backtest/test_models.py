from datetime import timedelta
from decimal import Decimal

import pytest

from chanlun.decision_support.trading_system.backtest.models import (
    BacktestDataset,
)
from tests.trading_system.backtest.helpers import (
    BAR_AT,
    dataset,
    minute_bar,
    normal_status,
)


def test_market_bar_keeps_raw_and_analysis_prices_separate() -> None:
    bar = minute_bar(
        raw_open="10.00",
        raw_close="10.20",
        analysis_open="5.00",
        analysis_close="5.10",
        adjustment_known_at=BAR_AT,
    )

    assert bar.raw_open == Decimal("10.00")
    assert bar.analysis_open == Decimal("5.00")
    assert bar.adjustment_known_at <= bar.closed_at


def test_future_adjustment_is_rejected() -> None:
    with pytest.raises(ValueError, match="adjustment_known_at"):
        minute_bar(adjustment_known_at=BAR_AT + timedelta(minutes=1))


def test_source_hashes_are_sorted_deterministically() -> None:
    sample = dataset(
        source_hashes=(("statuses", "sha256:s"), ("bars", "sha256:b")),
    )

    assert sample.source_hashes == (
        ("bars", "sha256:b"),
        ("statuses", "sha256:s"),
    )


def test_duplicate_bar_or_status_key_is_rejected() -> None:
    bar = minute_bar()
    status = normal_status()

    with pytest.raises(ValueError, match="duplicate minute bar"):
        BacktestDataset(
            bars=(bar, bar),
            statuses=(status,),
            memberships=(),
            corporate_actions=(),
            membership_as_of_each_session=True,
            point_in_time_adjustment=True,
            source_hashes=(),
        )
    with pytest.raises(ValueError, match="duplicate security status"):
        BacktestDataset(
            bars=(bar,),
            statuses=(status, status),
            memberships=(),
            corporate_actions=(),
            membership_as_of_each_session=True,
            point_in_time_adjustment=True,
            source_hashes=(),
        )
