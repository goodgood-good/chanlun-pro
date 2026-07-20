from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from math import sqrt
from typing import Callable, Hashable, Iterable

import numpy as np

from chanlun.decision_support.trading_system.backtest.portfolio import (
    BacktestRun,
    BacktestTrade,
    EquityPoint,
)
from chanlun.decision_support.trading_system.models import PointType


_ZERO = Decimal("0")
_ONE = Decimal("1")


@dataclass(frozen=True, slots=True)
class TradeGroupMetrics:
    trade_count: int
    net_pnl: Decimal
    net_return: Decimal
    win_rate: Decimal | None
    expectancy: Decimal | None


@dataclass(frozen=True, slots=True)
class PerformanceMetrics:
    net_return: Decimal
    max_drawdown: Decimal
    max_drawdown_duration_bars: int
    calmar: Decimal | None
    ulcer_index: Decimal
    worst_trade: Decimal | None
    worst_day: Decimal | None
    worst_week: Decimal | None
    worst_month: Decimal | None
    value_at_risk_95: Decimal | None
    expected_shortfall_95: Decimal | None
    win_rate: Decimal | None
    payoff_ratio: Decimal | None
    profit_factor: Decimal | None
    expectancy: Decimal | None
    exposure_ratio: Decimal
    turnover: Decimal
    total_cost: Decimal
    cost_to_gross_profit: Decimal | None
    per_point_type: tuple[tuple[PointType, TradeGroupMetrics], ...]
    per_sector: tuple[tuple[str, TradeGroupMetrics], ...]
    per_year: tuple[tuple[int, TradeGroupMetrics], ...]
    annualized_return: Decimal | None
    sharpe: Decimal | None
    sortino: Decimal | None
    max_symbol_trade_concentration: Decimal
    max_sector_trade_concentration: Decimal
    warnings: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class SampleAdequacy:
    adequate: bool
    closed_trade_count: int
    point_counts: tuple[tuple[PointType, int], ...]
    enabled_buy_classes: tuple[PointType, ...]
    failures: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ConfidenceInterval:
    lower: Decimal
    median: Decimal
    upper: Decimal


@dataclass(frozen=True, slots=True)
class BootstrapIntervals:
    expectancy: ConfidenceInterval
    net_return: ConfidenceInterval
    max_drawdown: ConfidenceInterval
    repetitions: int
    seed: int
    cluster_count: int


def _trade_summary(trades: Iterable[BacktestTrade]) -> TradeGroupMetrics:
    rows = tuple(trades)
    if not rows:
        return TradeGroupMetrics(0, _ZERO, _ZERO, None, None)
    wins = sum(trade.net_pnl > 0 for trade in rows)
    compounded = _ONE
    for trade in sorted(rows, key=lambda item: (item.exit_at, item.code)):
        compounded *= _ONE + trade.net_return
    return TradeGroupMetrics(
        trade_count=len(rows),
        net_pnl=sum((trade.net_pnl for trade in rows), _ZERO),
        net_return=compounded - _ONE,
        win_rate=Decimal(wins) / Decimal(len(rows)),
        expectancy=sum((trade.net_return for trade in rows), _ZERO)
        / Decimal(len(rows)),
    )


def _group_trade_metrics(
    trades: tuple[BacktestTrade, ...],
    key: Callable[[BacktestTrade], Hashable],
) -> tuple[tuple[Hashable, TradeGroupMetrics], ...]:
    grouped: dict[Hashable, list[BacktestTrade]] = defaultdict(list)
    for trade in trades:
        grouped[key(trade)].append(trade)
    return tuple(
        (name, _trade_summary(grouped[name]))
        for name in sorted(grouped, key=str)
    )


def _drawdown_series(
    points: tuple[EquityPoint, ...],
) -> tuple[tuple[Decimal, ...], int]:
    peak = points[0].equity
    drawdowns: list[Decimal] = []
    current_duration = 0
    maximum_duration = 0
    for point in points:
        peak = max(peak, point.equity)
        drawdown = (peak - point.equity) / peak if peak > 0 else _ZERO
        drawdowns.append(drawdown)
        if drawdown > 0:
            current_duration += 1
            maximum_duration = max(maximum_duration, current_duration)
        else:
            current_duration = 0
    return tuple(drawdowns), maximum_duration


def _period_returns(
    points: tuple[EquityPoint, ...],
    key: Callable[[date], Hashable],
) -> tuple[Decimal, ...]:
    grouped: dict[Hashable, list[EquityPoint]] = defaultdict(list)
    for point in points:
        grouped[key(point.closed_at.date())].append(point)
    ordered_keys = sorted(
        grouped,
        key=lambda period: min(
            point.closed_at for point in grouped[period]
        ),
    )
    previous = points[0].equity
    returns: list[Decimal] = []
    for period in ordered_keys:
        ending = sorted(grouped[period], key=attr_closed_at)[-1].equity
        returns.append(ending / previous - _ONE)
        previous = ending
    return tuple(returns)


def attr_closed_at(point: EquityPoint) -> datetime:
    return point.closed_at


def _tail_loss(returns: tuple[Decimal, ...]) -> tuple[Decimal | None, Decimal | None]:
    if not returns:
        return None, None
    values = np.asarray([float(value) for value in returns], dtype=float)
    threshold = float(np.quantile(values, 0.05))
    tail = values[values <= threshold]
    var = max(0.0, -threshold)
    expected_shortfall = max(0.0, -float(tail.mean()))
    return Decimal(str(var)), Decimal(str(expected_shortfall))


def _annualized_statistics(
    points: tuple[EquityPoint, ...],
    net_return: Decimal,
    daily_returns: tuple[Decimal, ...],
) -> tuple[Decimal | None, Decimal | None, Decimal | None, tuple[str, ...]]:
    span_seconds = Decimal(
        str((points[-1].closed_at - points[0].closed_at).total_seconds())
    )
    span_days = span_seconds / Decimal("86400")
    if span_days < Decimal("365"):
        return None, None, None, ("insufficient_calendar_span",)
    if net_return <= -_ONE:
        return None, None, None, ("annualization_domain_error",)
    if span_days == Decimal("365"):
        annualized = net_return
    else:
        annualized = Decimal(
            str(
                (_ONE + net_return).__float__()
                ** float(Decimal("365") / span_days)
                - 1.0
            )
        )
    if len(daily_returns) < 2:
        return annualized, None, None, ()
    values = np.asarray([float(value) for value in daily_returns], dtype=float)
    standard_deviation = float(values.std(ddof=1))
    sharpe = (
        None
        if standard_deviation == 0
        else Decimal(str(float(values.mean()) / standard_deviation * sqrt(252)))
    )
    downside = values[values < 0]
    downside_deviation = (
        0.0 if len(downside) == 0 else float(np.sqrt(np.mean(downside**2)))
    )
    sortino = (
        None
        if downside_deviation == 0
        else Decimal(
            str(float(values.mean()) / downside_deviation * sqrt(252))
        )
    )
    return annualized, sharpe, sortino, ()


def _maximum_concentration(
    trades: tuple[BacktestTrade, ...],
    key: Callable[[BacktestTrade], str],
) -> Decimal:
    if not trades:
        return _ZERO
    counts = Counter(key(trade) for trade in trades)
    return Decimal(max(counts.values())) / Decimal(len(trades))


def calculate_metrics(run: BacktestRun) -> PerformanceMetrics:
    points = tuple(sorted(run.equity_curve, key=attr_closed_at))
    if not points:
        raise ValueError("equity curve cannot be empty")
    if any(point.equity <= 0 for point in points):
        raise ValueError("equity curve must remain positive")
    trades = tuple(sorted(run.trades, key=lambda item: (item.exit_at, item.code)))
    net_return = points[-1].equity / points[0].equity - _ONE
    drawdowns, maximum_duration = _drawdown_series(points)
    max_drawdown = max(drawdowns, default=_ZERO)
    ulcer_index = Decimal(
        str(sqrt(sum(float(value * value) for value in drawdowns) / len(drawdowns)))
    )
    daily_returns = _period_returns(points, lambda session: session)
    weekly_returns = _period_returns(
        points,
        lambda session: (session.isocalendar().year, session.isocalendar().week),
    )
    monthly_returns = _period_returns(
        points,
        lambda session: (session.year, session.month),
    )
    annualized, sharpe, sortino, annualized_warnings = _annualized_statistics(
        points,
        net_return,
        daily_returns,
    )
    calmar = (
        None
        if annualized is None or max_drawdown == 0
        else annualized / max_drawdown
    )
    winners = tuple(trade for trade in trades if trade.net_pnl > 0)
    losers = tuple(trade for trade in trades if trade.net_pnl < 0)
    win_rate = (
        None
        if not trades
        else Decimal(len(winners)) / Decimal(len(trades))
    )
    average_win = (
        None
        if not winners
        else sum((trade.net_pnl for trade in winners), _ZERO)
        / Decimal(len(winners))
    )
    average_loss = (
        None
        if not losers
        else abs(sum((trade.net_pnl for trade in losers), _ZERO))
        / Decimal(len(losers))
    )
    payoff_ratio = (
        None
        if average_win is None or average_loss in (None, _ZERO)
        else average_win / average_loss
    )
    gross_profit = sum((trade.net_pnl for trade in winners), _ZERO)
    gross_loss = abs(sum((trade.net_pnl for trade in losers), _ZERO))
    profit_factor = None if gross_loss == 0 else gross_profit / gross_loss
    expectancy = (
        None
        if not trades
        else sum((trade.net_return for trade in trades), _ZERO)
        / Decimal(len(trades))
    )
    total_cost = sum((trade.total_cost for trade in trades), _ZERO)
    gross_profit_before_costs = sum(
        (
            max(_ZERO, trade.net_pnl + trade.total_cost)
            for trade in trades
        ),
        _ZERO,
    )
    cost_to_gross_profit = (
        None
        if gross_profit_before_costs == 0
        else total_cost / gross_profit_before_costs
    )
    turnover_cash = sum(
        (
            (trade.entry_price + trade.exit_price) * Decimal(trade.shares)
            for trade in trades
        ),
        _ZERO,
    )
    turnover = turnover_cash / points[0].equity
    exposure_ratio = Decimal(
        sum(point.market_value > 0 for point in points)
    ) / Decimal(len(points))
    value_at_risk, expected_shortfall = _tail_loss(daily_returns)
    symbol_concentration = _maximum_concentration(trades, lambda row: row.code)
    sector_concentration = _maximum_concentration(
        trades,
        lambda row: row.sector_id,
    )
    warnings = list(annualized_warnings)
    if symbol_concentration > Decimal("0.20"):
        warnings.append("symbol_trade_concentration_over_20pct")
    if sector_concentration > Decimal("0.20"):
        warnings.append("sector_trade_concentration_over_20pct")
    return PerformanceMetrics(
        net_return=net_return,
        max_drawdown=max_drawdown,
        max_drawdown_duration_bars=maximum_duration,
        calmar=calmar,
        ulcer_index=ulcer_index,
        worst_trade=min((trade.net_return for trade in trades), default=None),
        worst_day=min(daily_returns, default=None),
        worst_week=min(weekly_returns, default=None),
        worst_month=min(monthly_returns, default=None),
        value_at_risk_95=value_at_risk,
        expected_shortfall_95=expected_shortfall,
        win_rate=win_rate,
        payoff_ratio=payoff_ratio,
        profit_factor=profit_factor,
        expectancy=expectancy,
        exposure_ratio=exposure_ratio,
        turnover=turnover,
        total_cost=total_cost,
        cost_to_gross_profit=cost_to_gross_profit,
        per_point_type=tuple(
            (name, summary)
            for name, summary in _group_trade_metrics(
                trades,
                lambda item: item.point_type,
            )
        ),
        per_sector=tuple(
            (str(name), summary)
            for name, summary in _group_trade_metrics(
                trades,
                lambda item: item.sector_id,
            )
        ),
        per_year=tuple(
            (int(name), summary)
            for name, summary in _group_trade_metrics(
                trades,
                lambda item: item.exit_at.year,
            )
        ),
        annualized_return=annualized,
        sharpe=sharpe,
        sortino=sortino,
        max_symbol_trade_concentration=symbol_concentration,
        max_sector_trade_concentration=sector_concentration,
        warnings=tuple(dict.fromkeys(warnings)),
    )


def sample_adequacy(
    run: BacktestRun,
    *,
    enabled_buy_classes: tuple[PointType, ...] = (
        "1buy",
        "2buy",
        "3buy",
    ),
) -> SampleAdequacy:
    if not enabled_buy_classes:
        raise ValueError("at least one buy class must be enabled")
    if len(enabled_buy_classes) != len(set(enabled_buy_classes)):
        raise ValueError("enabled buy classes must be unique")
    counts = Counter(trade.point_type for trade in run.trades)
    failures: list[str] = []
    if len(run.trades) < 200:
        failures.append("closed_trades_below_200")
    for point_type in enabled_buy_classes:
        if counts[point_type] < 50:
            failures.append(f"{point_type}_closed_trades_below_50")
    point_counts = tuple(
        (point_type, counts[point_type]) for point_type in enabled_buy_classes
    )
    return SampleAdequacy(
        adequate=not failures,
        closed_trade_count=len(run.trades),
        point_counts=point_counts,
        enabled_buy_classes=enabled_buy_classes,
        failures=tuple(failures),
    )


def _clustered_trade_groups(
    trades: tuple[BacktestTrade, ...],
) -> tuple[tuple[BacktestTrade, ...], ...]:
    grouped: dict[tuple[date, str], list[BacktestTrade]] = defaultdict(list)
    for trade in trades:
        grouped[(trade.exit_at.date(), trade.sector_id)].append(trade)
    return tuple(
        tuple(sorted(grouped[key], key=lambda item: (item.exit_at, item.code)))
        for key in sorted(grouped)
    )


def _sample_statistics(
    trades: tuple[BacktestTrade, ...],
) -> tuple[float, float, float]:
    returns = [float(trade.net_return) for trade in trades]
    expectancy = float(np.mean(returns)) if returns else 0.0
    equity = 1.0
    peak = 1.0
    max_drawdown = 0.0
    for trade_return in returns:
        equity *= 1.0 + trade_return
        peak = max(peak, equity)
        max_drawdown = max(max_drawdown, (peak - equity) / peak)
    return expectancy, equity - 1.0, max_drawdown


def _confidence_interval(values: list[float]) -> ConfidenceInterval:
    lower, median, upper = np.quantile(
        np.asarray(values, dtype=float),
        (0.025, 0.5, 0.975),
    )
    return ConfidenceInterval(
        lower=Decimal(str(float(lower))),
        median=Decimal(str(float(median))),
        upper=Decimal(str(float(upper))),
    )


def clustered_bootstrap(
    trades: tuple[BacktestTrade, ...],
    *,
    repetitions: int = 2000,
    seed: int = 20260720,
) -> BootstrapIntervals:
    if repetitions <= 0:
        raise ValueError("repetitions must be positive")
    groups = _clustered_trade_groups(trades)
    if not groups:
        raise ValueError("bootstrap requires closed trades")
    rng = np.random.default_rng(seed)
    expectancy_samples: list[float] = []
    net_return_samples: list[float] = []
    drawdown_samples: list[float] = []
    for _index in range(repetitions):
        indexes = rng.integers(0, len(groups), size=len(groups))
        sample = tuple(
            trade
            for group_index in indexes
            for trade in groups[int(group_index)]
        )
        expectancy, net_return, max_drawdown = _sample_statistics(sample)
        expectancy_samples.append(expectancy)
        net_return_samples.append(net_return)
        drawdown_samples.append(max_drawdown)
    return BootstrapIntervals(
        expectancy=_confidence_interval(expectancy_samples),
        net_return=_confidence_interval(net_return_samples),
        max_drawdown=_confidence_interval(drawdown_samples),
        repetitions=repetitions,
        seed=seed,
        cluster_count=len(groups),
    )


__all__ = [
    "BootstrapIntervals",
    "ConfidenceInterval",
    "PerformanceMetrics",
    "SampleAdequacy",
    "TradeGroupMetrics",
    "calculate_metrics",
    "clustered_bootstrap",
    "sample_adequacy",
]
