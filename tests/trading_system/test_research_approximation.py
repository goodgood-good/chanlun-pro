from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta
from decimal import Decimal

import pandas as pd
import pytest

from chanlun.decision_support.trading_system.research_approximation import (
    ResearchApproximationObservation,
    ResearchApproximationParameters,
    evaluate_sector_research_approximation,
)
from tests.trading_system.helpers import CN
from tools.build_qmt_research_approximation import _observation, _positive_decimal


NOW = datetime(2026, 7, 24, 15, 0, tzinfo=CN)
SHA = "sha256:" + "a" * 64


def _peer(index: int) -> ResearchApproximationObservation:
    value = Decimal(index + 1)
    return ResearchApproximationObservation(
        symbol=f"SH.60{index:04d}",
        sector_id="qmt-sw1:S27",
        observed_at=NOW,
        last_completed_daily_close=value,
        median_daily_amount_20=value,
        book_value_per_share=Decimal("1"),
        roe=value,
        revenue_yoy=value,
        parent_profit_yoy=value,
        daily_known_at=NOW,
        finance_known_at=NOW - timedelta(days=10),
        source_revision=SHA,
    )


def test_research_proxy_accepts_leader_and_returns_explicit_reason() -> None:
    rows = tuple(
        replace(_peer(index), last_completed_daily_close=Decimal("6"))
        if index == 9
        else _peer(index)
        for index in range(10)
    )

    result = evaluate_sector_research_approximation(rows, sector_triggered=True)
    leader = result[-1]

    assert leader.accepted is True
    assert leader.fundamental_role == "LEADER"
    assert leader.relative_value_status == "FAIR"
    assert leader.reason_codes == ("PASS_QMT_RESEARCH_APPROXIMATION",)
    assert leader.highest_status == "RESEARCH_ONLY"
    assert leader.live_status == "LIVE_DISABLED"


def test_equal_thresholds_are_inclusive() -> None:
    rows = list(_peer(index) for index in range(10))
    target = rows[7]
    # Target has exactly 80th-percentile liquidity, 50th-percentile ROE and
    # 30th-percentile PB.  Each frozen boundary is inclusive.
    roe_values = (Decimal("1"), Decimal("2"), Decimal("3"), Decimal("4"), Decimal("6"), Decimal("7"), Decimal("8"), Decimal("5"), Decimal("9"), Decimal("10"))
    pb_values = (Decimal("1"), Decimal("2"), Decimal("4"), Decimal("5"), Decimal("6"), Decimal("7"), Decimal("8"), Decimal("3"), Decimal("9"), Decimal("10"))
    rows = [
        replace(row, roe=roe_values[index], last_completed_daily_close=pb_values[index])
        for index, row in enumerate(rows)
    ]

    decisions = evaluate_sector_research_approximation(rows, sector_triggered=True)
    decision = next(value for value in decisions if value.symbol == target.symbol)

    assert decision.liquidity_percentile == Decimal("0.8")
    assert decision.roe_percentile == Decimal("0.5")
    assert decision.pb_percentile == Decimal("0.3")
    assert decision.fundamental_role == "LEADER"
    assert decision.relative_value_status == "UNDERVALUED"
    assert decision.accepted is True


def test_missing_data_is_not_deleted_to_raise_peer_coverage() -> None:
    rows = tuple(
        _peer(index)
        if index < 4
        else replace(
            _peer(index),
            book_value_per_share=None,
            finance_known_at=None,
        )
        for index in range(10)
    )

    decisions = evaluate_sector_research_approximation(rows, sector_triggered=True)

    assert all(not value.accepted for value in decisions)
    assert all(
        "REJECT_RESEARCH_PEER_COVERAGE_INSUFFICIENT" in value.reason_codes
        for value in decisions
    )
    missing = decisions[-1]
    assert "REJECT_POINT_IN_TIME_RESEARCH_FIELDS_MISSING" in missing.reason_codes


def test_sector_gate_precedes_the_research_proxy() -> None:
    decisions = evaluate_sector_research_approximation(
        tuple(_peer(index) for index in range(10)),
        sector_triggered=False,
    )

    assert all(not value.accepted for value in decisions)
    assert all("REJECT_SECTOR_NOT_TRIGGERED" in value.reason_codes for value in decisions)


def test_future_finance_fact_is_rejected_at_input_boundary() -> None:
    with pytest.raises(ValueError, match="finance_known_at cannot come from the future"):
        replace(_peer(0), finance_known_at=NOW + timedelta(seconds=1))


def test_approximation_parameter_identity_is_frozen() -> None:
    parameters = ResearchApproximationParameters()

    assert parameters.parameter_set_id.startswith("sha256:")
    assert parameters.status_ceiling == "RESEARCH_ONLY"
    assert parameters.live_status == "LIVE_DISABLED"


def test_daily_session_date_is_canonicalized_in_source_identity() -> None:
    daily = pd.DataFrame(
        {
            "session": tuple((NOW - timedelta(days=value)).date() for value in range(20)),
            "close": tuple(Decimal("10") for _value in range(20)),
            "amount": tuple(Decimal("10000000") for _value in range(20)),
        }
    ).sort_values("session")

    observation = _observation(
        code="SH.600000",
        sector_id="qmt-sw1:S27",
        observed_at=NOW,
        daily=daily,
        daily_sha256=SHA,
        finance=(),
        finance_sha256=SHA,
        parameters=ResearchApproximationParameters(),
    )

    assert observation.daily_known_at is not None
    assert observation.source_revision.startswith("sha256:")


def test_nonpositive_book_value_is_preserved_as_missing_not_fabricated() -> None:
    assert _positive_decimal("-0.5") is None
    assert _positive_decimal("0") is None
    assert _positive_decimal("1.25") == Decimal("1.25")
