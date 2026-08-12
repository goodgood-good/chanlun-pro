from __future__ import annotations

from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from itertools import product
from typing import Generic, TypeVar, overload

import pandas as pd


@dataclass(frozen=True, slots=True)
class WalkForwardWindow:
    ordinal: int
    train_start: date
    train_end: date
    validation_start: date
    validation_end: date
    test_start: date
    test_end: date
    embargo_days: int

    def __post_init__(self) -> None:
        if self.ordinal <= 0 or self.embargo_days < 0:
            raise ValueError("invalid window identity")
        if not (
            self.train_start
            <= self.train_end
            < self.validation_start
            <= self.validation_end
            < self.test_start
            <= self.test_end
        ):
            raise ValueError("walk-forward roles must be chronological and disjoint")
        train_gap = (self.validation_start - self.train_end).days - 1
        validation_gap = (self.test_start - self.validation_end).days - 1
        if train_gap != self.embargo_days or validation_gap != self.embargo_days:
            raise ValueError("window gaps do not match embargo_days")

    @property
    def window_id(self) -> str:
        return f"wf-{self.ordinal:03d}"


@dataclass(frozen=True, slots=True)
class WalkForwardSchedule(Sequence[WalkForwardWindow]):
    windows: tuple[WalkForwardWindow, ...]
    reason: str | None = None

    def __post_init__(self) -> None:
        if self.windows and self.reason is not None:
            raise ValueError("non-empty schedule cannot carry an empty reason")
        if not self.windows and not self.reason:
            raise ValueError("empty schedule requires an explicit reason")
        if any(
            later.test_start <= earlier.test_end
            for earlier, later in zip(self.windows, self.windows[1:])
        ):
            raise ValueError("out-of-sample test windows cannot overlap")

    def __len__(self) -> int:
        return len(self.windows)

    @overload
    def __getitem__(self, index: int) -> WalkForwardWindow: ...

    @overload
    def __getitem__(self, index: slice) -> tuple[WalkForwardWindow, ...]: ...

    def __getitem__(
        self,
        index: int | slice,
    ) -> WalkForwardWindow | tuple[WalkForwardWindow, ...]:
        return self.windows[index]

    def __iter__(self) -> Iterator[WalkForwardWindow]:
        return iter(self.windows)


def _calendar_end(started_at: pd.Timestamp, months: int) -> pd.Timestamp:
    return started_at + pd.DateOffset(months=months) - pd.Timedelta(days=1)


def build_walk_forward_windows(
    *,
    start: date,
    end: date,
    train_months: int = 36,
    validation_months: int = 6,
    test_months: int = 6,
    step_months: int = 6,
    embargo_days: int = 5,
) -> WalkForwardSchedule:
    if start > end:
        raise ValueError("start cannot follow end")
    if any(
        value <= 0
        for value in (
            train_months,
            validation_months,
            test_months,
            step_months,
        )
    ):
        raise ValueError("window month counts must be positive")
    if embargo_days < 0:
        raise ValueError("embargo_days cannot be negative")
    if step_months < test_months:
        raise ValueError("step_months cannot create overlapping test windows")

    anchor = pd.Timestamp(start)
    final = pd.Timestamp(end)
    windows: list[WalkForwardWindow] = []
    ordinal = 1
    while anchor <= final:
        train_end = _calendar_end(anchor, train_months)
        validation_start = train_end + pd.Timedelta(days=embargo_days + 1)
        validation_end = _calendar_end(validation_start, validation_months)
        test_start = validation_end + pd.Timedelta(days=embargo_days + 1)
        test_end = _calendar_end(test_start, test_months)
        if test_end > final:
            break
        windows.append(
            WalkForwardWindow(
                ordinal=ordinal,
                train_start=anchor.date(),
                train_end=train_end.date(),
                validation_start=validation_start.date(),
                validation_end=validation_end.date(),
                test_start=test_start.date(),
                test_end=test_end.date(),
                embargo_days=embargo_days,
            )
        )
        ordinal += 1
        anchor += pd.DateOffset(months=step_months)
    if not windows:
        return WalkForwardSchedule(
            windows=(),
            reason="insufficient_calendar_span_for_walk_forward",
        )
    return WalkForwardSchedule(windows=tuple(windows))


@dataclass(frozen=True, slots=True)
class FrozenParameters:
    base_trade_risk: Decimal
    max_portfolio_heat: Decimal
    first_buy_risk_multiplier: Decimal

    def __post_init__(self) -> None:
        if self.base_trade_risk <= 0 or self.max_portfolio_heat <= 0:
            raise ValueError("risk parameters must be positive")
        if self.first_buy_risk_multiplier < 0:
            raise ValueError("first-buy multiplier cannot be negative")


PRE_REGISTERED_PARAMETER_GRID = tuple(
    FrozenParameters(
        base_trade_risk=base_trade_risk,
        max_portfolio_heat=max_portfolio_heat,
        first_buy_risk_multiplier=first_buy_risk_multiplier,
    )
    for (
        base_trade_risk,
        max_portfolio_heat,
        first_buy_risk_multiplier,
    ) in product(
        (Decimal("0.0035"), Decimal("0.005")),
        (Decimal("0.015"), Decimal("0.02")),
        (Decimal("0.25"), Decimal("0.50")),
    )
)


@dataclass(frozen=True, slots=True)
class ValidationResult:
    parameters: FrozenParameters
    valid: bool
    max_drawdown: Decimal
    calmar: Decimal | None
    net_return: Decimal
    failure_codes: tuple[str, ...]

    def __post_init__(self) -> None:
        if self.max_drawdown < 0:
            raise ValueError("max_drawdown cannot be negative")
        if self.valid and self.failure_codes:
            raise ValueError("valid result cannot carry failure codes")
        if len(self.failure_codes) != len(set(self.failure_codes)):
            raise ValueError("failure codes must be unique")


@dataclass(frozen=True, slots=True)
class SelectedParameters:
    parameters: FrozenParameters
    validation_result: ValidationResult

    def __post_init__(self) -> None:
        if self.parameters != self.validation_result.parameters:
            raise ValueError("selected parameters and evidence disagree")


def _parameter_key(
    parameters: FrozenParameters,
) -> tuple[Decimal, Decimal, Decimal]:
    return (
        parameters.base_trade_risk,
        parameters.max_portfolio_heat,
        parameters.first_buy_risk_multiplier,
    )


def _selection_key(
    result: ValidationResult,
) -> tuple[int, Decimal, Decimal, Decimal, tuple[Decimal, Decimal, Decimal]]:
    calmar_rank = (
        Decimal("Infinity") if result.calmar is None else -result.calmar
    )
    return (
        0 if result.valid else 1,
        result.max_drawdown,
        calmar_rank,
        -result.net_return,
        _parameter_key(result.parameters),
    )


def select_on_validation(
    parameter_grid: tuple[FrozenParameters, ...],
    results: tuple[ValidationResult, ...],
) -> SelectedParameters:
    if parameter_grid != PRE_REGISTERED_PARAMETER_GRID:
        raise ValueError("parameter grid differs from pre-registered grid")
    if len(results) != len(parameter_grid):
        raise ValueError("validation must evaluate every registered parameter set")
    result_parameters = tuple(result.parameters for result in results)
    if len(result_parameters) != len(set(result_parameters)):
        raise ValueError("duplicate validation parameter result")
    if set(result_parameters) != set(parameter_grid):
        raise ValueError("validation results do not cover the registered grid")
    selected = min(results, key=_selection_key)
    return SelectedParameters(selected.parameters, selected)


TestResult = TypeVar("TestResult")


@dataclass(frozen=True, slots=True)
class LockedTestEvaluation(Generic[TestResult]):
    parameters: FrozenParameters
    result: TestResult


def evaluate_locked_test(
    selected: SelectedParameters,
    test_data: Callable[[FrozenParameters], TestResult],
) -> LockedTestEvaluation[TestResult]:
    return LockedTestEvaluation(
        parameters=selected.parameters,
        result=test_data(selected.parameters),
    )


__all__ = [
    "FrozenParameters",
    "LockedTestEvaluation",
    "PRE_REGISTERED_PARAMETER_GRID",
    "SelectedParameters",
    "ValidationResult",
    "WalkForwardSchedule",
    "WalkForwardWindow",
    "build_walk_forward_windows",
    "evaluate_locked_test",
    "select_on_validation",
]
