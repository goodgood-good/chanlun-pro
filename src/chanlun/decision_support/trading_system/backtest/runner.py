from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, replace
from datetime import date, datetime, time
from decimal import Decimal
from typing import Callable, Iterable, Literal
from zoneinfo import ZoneInfo

import pandas as pd

from chanlun.decision_support.trading_system.backtest.data_source import (
    CausalStructureReplay,
    NativeSectorBar,
)
from chanlun.decision_support.trading_system.backtest.execution import (
    ExecutionPolicy,
)
from chanlun.decision_support.trading_system.backtest.models import (
    BacktestDataset,
    MinuteBar,
)
from chanlun.decision_support.trading_system.backtest.metrics import (
    calculate_metrics,
)
from chanlun.decision_support.trading_system.backtest.portfolio import (
    BacktestRun,
    EquityPoint,
    run_event_backtest,
)
from chanlun.decision_support.trading_system.backtest.report import (
    REQUIRED_ABLATION_IDS,
    AblationResult,
    BacktestEvaluationResult,
    BenchmarkResult,
    WalkForwardWindowResult,
)
from chanlun.decision_support.trading_system.backtest.walk_forward import (
    PRE_REGISTERED_PARAMETER_GRID,
    FrozenParameters,
    ValidationResult,
    build_walk_forward_windows,
    evaluate_locked_test,
    select_on_validation,
)
from chanlun.decision_support.trading_system.engine import TradingEngine
from chanlun.decision_support.trading_system.models import TradingPolicy
from chanlun.decision_support.trading_system.portfolio_risk import RiskLimits


_FRAME_COLUMNS = ("date", "open", "high", "low", "close", "volume")
CN = ZoneInfo("Asia/Shanghai")


PeriodRunner = Callable[
    [BacktestDataset, FrozenParameters, Decimal],
    BacktestRun,
]


@dataclass(frozen=True, slots=True)
class WalkForwardResearch:
    evaluation: BacktestEvaluationResult
    selected_parameters: tuple[tuple[str, FrozenParameters], ...]
    limitations: tuple[str, ...]
    ablations: tuple[AblationResult, ...] = ()
    benchmarks: tuple[BenchmarkResult, ...] = ()


def _frame(rows: list[dict[str, object]]) -> pd.DataFrame:
    return pd.DataFrame(rows, columns=_FRAME_COLUMNS)


def _session_minute(bar: MinuteBar) -> tuple[str, int] | None:
    observed = bar.opened_at.timetz().replace(tzinfo=None)
    if time(9, 30) <= observed < time(11, 30):
        return "morning", (observed.hour * 60 + observed.minute) - (9 * 60 + 30)
    if time(13, 0) <= observed < time(15, 0):
        return "afternoon", (observed.hour * 60 + observed.minute) - 13 * 60
    return None


def _stock_rows(bars: tuple[MinuteBar, ...]) -> list[dict[str, object]]:
    return [
        {
            "date": pd.Timestamp(bar.closed_at),
            "open": float(bar.analysis_open),
            "high": float(bar.analysis_high),
            "low": float(bar.analysis_low),
            "close": float(bar.analysis_close),
            "volume": float(bar.volume),
        }
        for bar in bars
    ]


def _aggregate_rows(
    bars: tuple[MinuteBar, ...],
    minutes: int,
) -> list[dict[str, object]]:
    grouped: dict[tuple[object, str, int], list[tuple[int, MinuteBar]]] = defaultdict(
        list
    )
    for bar in bars:
        position = _session_minute(bar)
        if position is None:
            continue
        segment, minute_index = position
        grouped[(bar.opened_at.date(), segment, minute_index // minutes)].append(
            (minute_index, bar)
        )
    output: list[dict[str, object]] = []
    for key in sorted(grouped):
        rows = sorted(grouped[key], key=lambda item: item[0])
        expected_start = key[2] * minutes
        if [index for index, _bar in rows] != list(
            range(expected_start, expected_start + minutes)
        ):
            continue
        ordered = tuple(bar for _index, bar in rows)
        output.append(
            {
                "date": pd.Timestamp(ordered[-1].closed_at),
                "open": float(ordered[0].analysis_open),
                "high": max(float(bar.analysis_high) for bar in ordered),
                "low": min(float(bar.analysis_low) for bar in ordered),
                "close": float(ordered[-1].analysis_close),
                "volume": sum(float(bar.volume) for bar in ordered),
            }
        )
    return output


def _sector_rows(bars: Iterable[NativeSectorBar]) -> list[dict[str, object]]:
    return [
        {
            "date": pd.Timestamp(bar.closed_at),
            "open": float(bar.opened),
            "high": float(bar.high),
            "low": float(bar.low),
            "close": float(bar.closed),
            "volume": float(bar.volume),
        }
        for bar in sorted(bars, key=lambda item: item.closed_at)
    ]


def build_replay_frames(
    dataset: BacktestDataset,
    native_sector_bars: tuple[NativeSectorBar, ...],
) -> dict[tuple[str, str], pd.DataFrame]:
    by_code: dict[str, list[MinuteBar]] = defaultdict(list)
    for bar in dataset.bars:
        by_code[bar.code].append(bar)
    output: dict[tuple[str, str], pd.DataFrame] = {}
    for code in sorted(by_code):
        bars = tuple(sorted(by_code[code], key=lambda item: item.closed_at))
        output[(code, "1m")] = _frame(_stock_rows(bars))
        output[(code, "5m")] = _frame(_aggregate_rows(bars, 5))
        output[(code, "30m")] = _frame(_aggregate_rows(bars, 30))

    sector_groups: dict[tuple[str, str], list[NativeSectorBar]] = defaultdict(list)
    for bar in native_sector_bars:
        sector_groups[(bar.index_code, bar.frequency)].append(bar)
    for key in sorted(sector_groups):
        output[key] = _frame(_sector_rows(sector_groups[key]))
    return output


def build_causal_period_runner(
    dataset: BacktestDataset,
    native_sector_bars: tuple[NativeSectorBar, ...],
    *,
    ablation_id: str = "plus_portfolio_risk",
) -> PeriodRunner:
    if ablation_id not in REQUIRED_ABLATION_IDS:
        raise ValueError("unknown required ablation id")
    stage = REQUIRED_ABLATION_IDS.index(ablation_id)
    frames = build_replay_frames(dataset, native_sector_bars)
    sector_index_codes: dict[str, str] = {}
    for bar in native_sector_bars:
        previous = sector_index_codes.setdefault(bar.sector_id, bar.index_code)
        if previous != bar.index_code:
            raise ValueError("sector maps to multiple native index codes")
    sector_names = {
        row.sector_id: row.sector_id for row in dataset.memberships
    }

    def run_period(
        period: BacktestDataset,
        parameters: FrozenParameters,
        initial_cash: Decimal,
    ) -> BacktestRun:
        replay = CausalStructureReplay(
            frames=frames,
            sector_names=sector_names,
            sector_index_codes=sector_index_codes,
        )
        policy = TradingPolicy(
            require_confirmed_one_minute=stage >= 3,
            require_sector_eligibility=stage >= 1,
            require_thirty_minute_context=stage >= 2,
            first_center_three_buy_only=(
                parameters.first_center_three_buy_only if stage >= 4 else False
            ),
            first_buy_risk_multiplier=parameters.first_buy_risk_multiplier,
        )
        if stage >= 5:
            risk_limits = replace(
                RiskLimits(),
                base_trade_risk=parameters.base_trade_risk,
                max_portfolio_heat=parameters.max_portfolio_heat,
            )
        else:
            risk_limits = replace(
                RiskLimits(),
                base_trade_risk=parameters.base_trade_risk,
                max_symbol_fraction=Decimal("1"),
                max_sector_fraction=Decimal("1"),
                max_portfolio_heat=Decimal("1"),
                first_drawdown=Decimal("0.90"),
                second_drawdown=Decimal("0.95"),
                stop_drawdown=Decimal("0.99"),
            )
        return run_event_backtest(
            period,
            engine=TradingEngine(policy),
            structure_replay=replay,
            risk_limits=risk_limits,
            execution_policy=ExecutionPolicy(),
            initial_cash=initial_cash,
            terminal_liquidation=True,
        )

    return run_period


def slice_dataset(
    dataset: BacktestDataset,
    *,
    start: date,
    end: date,
) -> BacktestDataset:
    if start > end:
        raise ValueError("start cannot follow end")
    return BacktestDataset(
        bars=tuple(
            bar
            for bar in dataset.bars
            if start <= bar.opened_at.date() <= end
        ),
        statuses=tuple(
            row for row in dataset.statuses if start <= row.session <= end
        ),
        memberships=tuple(
            row for row in dataset.memberships if start <= row.session <= end
        ),
        corporate_actions=tuple(
            row
            for row in dataset.corporate_actions
            if start <= row.effective_at.date() <= end
        ),
        membership_as_of_each_session=dataset.membership_as_of_each_session,
        point_in_time_adjustment=dataset.point_in_time_adjustment,
        source_hashes=dataset.source_hashes,
        security_status_as_of_each_session=(
            dataset.security_status_as_of_each_session
        ),
    )


def empty_evaluation(
    *,
    initial_cash: Decimal,
    observed_at: datetime,
    bootstrap_repetitions: int,
) -> BacktestEvaluationResult:
    if initial_cash <= 0:
        raise ValueError("initial_cash must be positive")
    baseline = EquityPoint(
        closed_at=observed_at,
        cash=initial_cash,
        market_value=Decimal("0"),
        equity=initial_cash,
        open_risk_cash=Decimal("0"),
    )
    return BacktestEvaluationResult(
        aggregate_run=BacktestRun(
            fills=(),
            trades=(),
            equity_curve=(baseline,),
            open_positions=(),
            pending_exits=(),
        ),
        bootstrap_repetitions=bootstrap_repetitions,
    )


def _validation_result(
    parameters: FrozenParameters,
    run: BacktestRun,
) -> ValidationResult:
    metrics = calculate_metrics(run)
    failures: list[str] = []
    if metrics.net_return <= 0:
        failures.append("validation_net_return_not_positive")
    if metrics.max_drawdown > Decimal("0.10"):
        failures.append("validation_drawdown_over_10pct")
    if metrics.calmar is None:
        failures.append("validation_calmar_unavailable")
    elif metrics.calmar < Decimal("1"):
        failures.append("validation_calmar_below_one")
    return ValidationResult(
        parameters=parameters,
        valid=not failures,
        max_drawdown=metrics.max_drawdown,
        calmar=metrics.calmar,
        net_return=metrics.net_return,
        failure_codes=tuple(failures),
    )


def _parameter_document(
    parameters: FrozenParameters,
) -> tuple[tuple[str, str | bool], ...]:
    return (
        ("base_trade_risk", str(parameters.base_trade_risk)),
        (
            "first_center_three_buy_only",
            parameters.first_center_three_buy_only,
        ),
        ("max_portfolio_heat", str(parameters.max_portfolio_heat)),
        (
            "first_buy_risk_multiplier",
            str(parameters.first_buy_risk_multiplier),
        ),
    )


def _combine_test_runs(runs: tuple[BacktestRun, ...]) -> BacktestRun:
    if not runs:
        raise ValueError("at least one test run is required")
    return BacktestRun(
        fills=tuple(fill for run in runs for fill in run.fills),
        trades=tuple(trade for run in runs for trade in run.trades),
        equity_curve=tuple(point for run in runs for point in run.equity_curve),
        open_positions=runs[-1].open_positions,
        pending_exits=runs[-1].pending_exits,
    )


def _unavailable_ablation_rows(reason: str) -> tuple[AblationResult, ...]:
    return tuple(
        AblationResult(
            ablation_id=ablation_id,
            label=ablation_id,
            trade_count=0,
            sample_reduction=Decimal("0"),
            net_return=Decimal("0"),
            max_drawdown=Decimal("0"),
            calmar=None,
            quality_change=Decimal("0"),
            completed=False,
            data_grade="invalid",
            failure_codes=(reason,),
        )
        for ablation_id in REQUIRED_ABLATION_IDS
    )


def run_required_ablations(
    dataset: BacktestDataset,
    *,
    start: date,
    end: date,
    initial_cash: Decimal,
    selected_parameters: tuple[tuple[str, FrozenParameters], ...],
    period_runner_factory: Callable[[str], PeriodRunner],
    data_grade: Literal["certified", "research_only", "invalid"],
) -> tuple[AblationResult, ...]:
    schedule = build_walk_forward_windows(start=start, end=end)
    if not schedule:
        return _unavailable_ablation_rows(
            schedule.reason or "walk_forward_schedule_empty"
        )
    selected = dict(selected_parameters)
    expected_ids = tuple(window.window_id for window in schedule)
    if tuple(selected) != expected_ids:
        return _unavailable_ablation_rows("locked_parameters_missing")

    metrics_by_id = {}
    for ablation_id in REQUIRED_ABLATION_IDS:
        period_runner = period_runner_factory(ablation_id)
        capital = initial_cash
        runs: list[BacktestRun] = []
        for window in schedule:
            test_data = slice_dataset(
                dataset,
                start=window.test_start,
                end=window.test_end,
            )
            if not test_data.bars:
                return _unavailable_ablation_rows(
                    f"test_market_data_missing:{window.window_id}"
                )
            run = period_runner(
                test_data,
                selected[window.window_id],
                capital,
            )
            runs.append(run)
            capital = run.equity_curve[-1].equity
        metrics_by_id[ablation_id] = calculate_metrics(
            _combine_test_runs(tuple(runs))
        )

    output: list[AblationResult] = []
    previous_trade_count: int | None = None
    previous_net_return: Decimal | None = None
    for ablation_id in REQUIRED_ABLATION_IDS:
        metrics = metrics_by_id[ablation_id]
        trade_count = sum(
            summary.trade_count
            for _point_type, summary in metrics.per_point_type
        )
        if previous_trade_count in (None, 0):
            sample_reduction = Decimal("0")
        else:
            sample_reduction = max(
                Decimal("0"),
                min(
                    Decimal("1"),
                    Decimal(previous_trade_count - trade_count)
                    / Decimal(previous_trade_count),
                ),
            )
        quality_change = (
            Decimal("0")
            if previous_net_return is None
            else metrics.net_return - previous_net_return
        )
        output.append(
            AblationResult(
                ablation_id=ablation_id,
                label=ablation_id,
                trade_count=trade_count,
                sample_reduction=sample_reduction,
                net_return=metrics.net_return,
                max_drawdown=metrics.max_drawdown,
                calmar=metrics.calmar,
                quality_change=quality_change,
                completed=True,
                data_grade=data_grade,
            )
        )
        previous_trade_count = trade_count
        previous_net_return = metrics.net_return
    return tuple(output)


def run_walk_forward_evaluation(
    dataset: BacktestDataset,
    *,
    start: date,
    end: date,
    initial_cash: Decimal,
    bootstrap_repetitions: int,
    period_runner: PeriodRunner | None = None,
) -> WalkForwardResearch:
    schedule = build_walk_forward_windows(start=start, end=end)
    if not schedule:
        observed_at = datetime.combine(start, time.min, tzinfo=CN)
        return WalkForwardResearch(
            evaluation=empty_evaluation(
                initial_cash=initial_cash,
                observed_at=observed_at,
                bootstrap_repetitions=bootstrap_repetitions,
            ),
            selected_parameters=(),
            limitations=(schedule.reason or "walk_forward_schedule_empty",),
        )
    if period_runner is None:
        raise ValueError("period_runner is required for non-empty walk-forward")

    test_runs: list[BacktestRun] = []
    window_results: list[WalkForwardWindowResult] = []
    selected_rows: list[tuple[str, FrozenParameters]] = []
    all_validation_results: list[ValidationResult] = []
    capital = initial_cash
    for window in schedule:
        validation_data = slice_dataset(
            dataset,
            start=window.validation_start,
            end=window.validation_end,
        )
        if not validation_data.bars:
            raise ValueError(f"validation market data missing: {window.window_id}")
        validation_results = tuple(
            _validation_result(
                parameters,
                period_runner(validation_data, parameters, initial_cash),
            )
            for parameters in PRE_REGISTERED_PARAMETER_GRID
        )
        all_validation_results.extend(validation_results)
        selected = select_on_validation(
            PRE_REGISTERED_PARAMETER_GRID,
            validation_results,
        )
        test_data = slice_dataset(
            dataset,
            start=window.test_start,
            end=window.test_end,
        )
        if not test_data.bars:
            raise ValueError(f"test market data missing: {window.window_id}")
        locked = evaluate_locked_test(
            selected,
            lambda parameters: period_runner(test_data, parameters, capital),
        )
        test_run = locked.result
        metrics = calculate_metrics(test_run)
        test_runs.append(test_run)
        capital = test_run.equity_curve[-1].equity
        selected_rows.append((window.window_id, locked.parameters))
        window_results.append(
            WalkForwardWindowResult(
                window_id=window.window_id,
                train_start=window.train_start,
                train_end=window.train_end,
                validation_start=window.validation_start,
                validation_end=window.validation_end,
                test_start=window.test_start,
                test_end=window.test_end,
                selected_parameters=_parameter_document(locked.parameters),
                test_metrics=metrics,
                closed_trade_count=len(test_run.trades),
            )
        )
    validation_net_returns = tuple(
        row.net_return for row in all_validation_results
    )
    validation_drawdowns = tuple(
        row.max_drawdown for row in all_validation_results
    )
    evaluation = BacktestEvaluationResult(
        aggregate_run=_combine_test_runs(tuple(test_runs)),
        walk_forward_windows=tuple(window_results),
        parameter_robustness=(
            (
                "validation_net_return_range",
                max(validation_net_returns) - min(validation_net_returns),
            ),
            (
                "validation_drawdown_range",
                max(validation_drawdowns) - min(validation_drawdowns),
            ),
        ),
        bootstrap_repetitions=bootstrap_repetitions,
    )
    return WalkForwardResearch(
        evaluation=evaluation,
        selected_parameters=tuple(selected_rows),
        limitations=("walk_forward_windows_evaluated_independently",),
    )


__all__ = [
    "WalkForwardResearch",
    "build_causal_period_runner",
    "build_replay_frames",
    "empty_evaluation",
    "run_required_ablations",
    "run_walk_forward_evaluation",
    "slice_dataset",
]
