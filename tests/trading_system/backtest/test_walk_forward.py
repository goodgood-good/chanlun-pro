from __future__ import annotations

from dataclasses import FrozenInstanceError
from datetime import date
from decimal import Decimal

import pytest

from chanlun.decision_support.trading_system.backtest.walk_forward import (
    PRE_REGISTERED_PARAMETER_GRID,
    ValidationResult,
    build_walk_forward_windows,
    evaluate_locked_test,
    select_on_validation,
)


def test_exact_36_6_6_boundaries_are_chronological() -> None:
    windows = build_walk_forward_windows(
        start=date(2018, 1, 1),
        end=date(2026, 6, 30),
        train_months=36,
        validation_months=6,
        test_months=6,
        step_months=6,
        embargo_days=5,
    )
    first = windows[0]

    assert first.train_start == date(2018, 1, 1)
    assert first.train_end == date(2020, 12, 31)
    assert first.validation_start == date(2021, 1, 6)
    assert first.validation_end == date(2021, 7, 5)
    assert first.test_start == date(2021, 7, 11)
    assert first.test_end == date(2022, 1, 10)
    assert first.train_end < first.validation_start
    assert first.validation_end < first.test_start
    assert windows[1].test_start > first.test_end


def test_embargo_has_exact_number_of_clear_calendar_days() -> None:
    window = build_walk_forward_windows(
        start=date(2018, 1, 1),
        end=date(2026, 6, 30),
        embargo_days=5,
    )[0]

    assert (window.validation_start - window.train_end).days - 1 == 5
    assert (window.test_start - window.validation_end).days - 1 == 5


def test_short_dataset_returns_explicit_empty_reason() -> None:
    schedule = build_walk_forward_windows(
        start=date(2024, 1, 1),
        end=date(2026, 1, 1),
    )

    assert len(schedule) == 0
    assert schedule.reason == "insufficient_calendar_span_for_walk_forward"


def test_parameter_grid_is_frozen_and_contains_only_registered_axes() -> None:
    assert len(PRE_REGISTERED_PARAMETER_GRID) == 8
    assert {row.base_trade_risk for row in PRE_REGISTERED_PARAMETER_GRID} == {
        Decimal("0.0035"),
        Decimal("0.005"),
    }
    assert {row.max_portfolio_heat for row in PRE_REGISTERED_PARAMETER_GRID} == {
        Decimal("0.015"),
        Decimal("0.02"),
    }
    assert {
        row.first_buy_risk_multiplier for row in PRE_REGISTERED_PARAMETER_GRID
    } == {Decimal("0.25"), Decimal("0.50")}
    with pytest.raises(FrozenInstanceError):
        PRE_REGISTERED_PARAMETER_GRID[0].base_trade_risk = Decimal("0.01")  # type: ignore[misc]


def validation_results() -> tuple[ValidationResult, ...]:
    rows = [
        ValidationResult(
            parameters=parameters,
            valid=False,
            max_drawdown=Decimal("0.01"),
            calmar=Decimal("5"),
            net_return=Decimal("1"),
            failure_codes=("sample_inadequate",),
        )
        for parameters in PRE_REGISTERED_PARAMETER_GRID
    ]
    candidates = PRE_REGISTERED_PARAMETER_GRID[:4]
    rows[0] = ValidationResult(
        candidates[0],
        True,
        Decimal("0.08"),
        Decimal("1.0"),
        Decimal("0.30"),
        (),
    )
    rows[1] = ValidationResult(
        candidates[1],
        True,
        Decimal("0.07"),
        Decimal("0.8"),
        Decimal("0.40"),
        (),
    )
    rows[2] = ValidationResult(
        candidates[2],
        True,
        Decimal("0.07"),
        Decimal("1.2"),
        Decimal("0.35"),
        (),
    )
    rows[3] = ValidationResult(
        candidates[3],
        True,
        Decimal("0.07"),
        Decimal("1.2"),
        Decimal("0.45"),
        (),
    )
    return tuple(rows)


def test_validation_selection_is_deterministic_lexicographic() -> None:
    selected = select_on_validation(
        PRE_REGISTERED_PARAMETER_GRID,
        validation_results(),
    )

    assert selected.parameters == PRE_REGISTERED_PARAMETER_GRID[3]
    assert selected.validation_result.net_return == Decimal("0.45")


def test_test_metrics_cannot_override_selected_parameters() -> None:
    selected = select_on_validation(
        PRE_REGISTERED_PARAMETER_GRID,
        validation_results(),
    )
    with pytest.raises(TypeError):
        evaluate_locked_test(
            selected,
            lambda parameters: {"parameters": parameters},
            parameter_override={"base_trade_risk": "0.01"},  # type: ignore[call-arg]
        )


def test_locked_test_receives_exact_selected_parameters() -> None:
    selected = select_on_validation(
        PRE_REGISTERED_PARAMETER_GRID,
        validation_results(),
    )

    evaluated = evaluate_locked_test(
        selected,
        lambda parameters: (parameters, "test-result"),
    )

    assert evaluated.parameters is selected.parameters
    assert evaluated.result == (selected.parameters, "test-result")
