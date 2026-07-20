from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Literal

from chanlun.decision_support.trading_system.backtest.models import BacktestDataset


EvidenceGrade = Literal["certified", "research_only", "invalid"]


@dataclass(frozen=True, slots=True)
class DataEvidence:
    grade: EvidenceGrade
    failures: tuple[str, ...]
    warnings: tuple[str, ...]
    coverage: tuple[tuple[str, Decimal], ...]


def calculate_coverage(
    dataset: BacktestDataset,
) -> tuple[tuple[str, Decimal], ...]:
    if not dataset.bars:
        status_coverage = Decimal("0")
    else:
        status_keys = {(row.code, row.session) for row in dataset.statuses}
        covered = sum(
            (bar.code, bar.closed_at.date()) in status_keys for bar in dataset.bars
        )
        status_coverage = Decimal(covered) / Decimal(len(dataset.bars))
    return (
        ("bar_status_coverage", status_coverage),
        (
            "historical_membership",
            Decimal("1")
            if dataset.membership_as_of_each_session
            else Decimal("0"),
        ),
        (
            "point_in_time_adjustment",
            Decimal("1") if dataset.point_in_time_adjustment else Decimal("0"),
        ),
        (
            "historical_security_status",
            Decimal("1")
            if dataset.security_status_as_of_each_session
            else Decimal("0"),
        ),
    )


def audit_dataset(dataset: BacktestDataset) -> DataEvidence:
    failures: list[str] = []
    if not dataset.bars or not dataset.statuses:
        failures.append("market_data_missing")
    status_keys = {(row.code, row.session) for row in dataset.statuses}
    if any(
        (bar.code, bar.closed_at.date()) not in status_keys
        for bar in dataset.bars
    ):
        failures.append("security_status_coverage_missing")
    if not dataset.membership_as_of_each_session:
        failures.append("historical_sector_membership_missing")
    if not dataset.point_in_time_adjustment:
        failures.append("point_in_time_adjustment_missing")
    if not dataset.security_status_as_of_each_session:
        failures.append("historical_security_status_missing")
    if any(bar.adjustment_known_at > bar.closed_at for bar in dataset.bars):
        failures.append("future_adjustment_factor")
    if any(
        not dataset.status_at(bar.code, bar.closed_at.date()).listed
        for bar in dataset.bars
        if (bar.code, bar.closed_at.date()) in status_keys
    ):
        failures.append("unlisted_bar_present")
    invalid_failures = {
        "market_data_missing",
        "security_status_coverage_missing",
        "future_adjustment_factor",
        "unlisted_bar_present",
    }
    grade: EvidenceGrade = (
        "invalid"
        if any(failure in invalid_failures for failure in failures)
        else "research_only"
        if failures
        else "certified"
    )
    return DataEvidence(
        grade=grade,
        failures=tuple(dict.fromkeys(failures)),
        warnings=(),
        coverage=calculate_coverage(dataset),
    )
