from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta
from decimal import Decimal
from typing import cast

from chanlun.decision_support.trading_system.backtest.metrics import (
    calculate_metrics,
    clustered_bootstrap,
    sample_adequacy,
)
from chanlun.decision_support.trading_system.backtest.execution import FillDecision
from chanlun.decision_support.trading_system.backtest.portfolio import (
    BacktestRun,
    BacktestTrade,
    EquityPoint,
)
from chanlun.decision_support.trading_system.models import PointType
from tests.trading_system.backtest.helpers import CN


START = datetime(2024, 1, 2, 15, 0, tzinfo=CN)


def equity_run(
    *values: str,
    spacing: timedelta = timedelta(days=1),
    trades: tuple[BacktestTrade, ...] = (),
) -> BacktestRun:
    points = tuple(
        EquityPoint(
            closed_at=START + index * spacing,
            cash=Decimal(value),
            market_value=Decimal("0"),
            equity=Decimal(value),
            open_risk_cash=Decimal("0"),
        )
        for index, value in enumerate(values)
    )
    return BacktestRun(
        fills=(),
        trades=trades,
        equity_curve=points,
        open_positions=(),
        pending_exits=(),
    )


def trade(
    index: int,
    *,
    point_type: str = "2buy",
    code: str = "SZ.000001",
    sector_id: str = "TDX.880301",
    net_pnl: str = "10",
    net_return: str = "0.01",
    total_cost: str = "1",
) -> BacktestTrade:
    entry_at = START + timedelta(days=index)
    return BacktestTrade(
        code=code,
        sector_id=sector_id,
        point_type=cast(PointType, point_type),
        entry_at=entry_at,
        exit_at=entry_at + timedelta(hours=1),
        shares=100,
        entry_price=Decimal("10"),
        exit_price=Decimal("10.10"),
        exit_trigger_price=Decimal("10.05"),
        exit_reason="signal_exit_full",
        net_pnl=Decimal(net_pnl),
        net_return=Decimal(net_return),
        total_cost=Decimal(total_cost),
    )


def test_drawdown_uses_running_peak_and_reports_duration() -> None:
    metrics = calculate_metrics(equity_run("100", "110", "99", "105"))

    assert metrics.max_drawdown == Decimal("0.10")
    assert metrics.max_drawdown_duration_bars == 2


def test_less_than_one_year_suppresses_annualized_headlines() -> None:
    metrics = calculate_metrics(
        equity_run("100", "105", spacing=timedelta(days=24))
    )

    assert metrics.annualized_return is None
    assert metrics.sharpe is None
    assert metrics.sortino is None
    assert "insufficient_calendar_span" in metrics.warnings


def test_calmar_is_none_without_annualization_or_drawdown() -> None:
    short = calculate_metrics(equity_run("100", "90"))
    no_drawdown = calculate_metrics(
        equity_run("100", "110", spacing=timedelta(days=365))
    )

    assert short.calmar is None
    assert no_drawdown.annualized_return == Decimal("0.1")
    assert no_drawdown.calmar is None


def test_calendar_periods_are_sorted_chronologically_not_lexically() -> None:
    points = tuple(
        EquityPoint(
            closed_at=at,
            cash=Decimal(value),
            market_value=Decimal("0"),
            equity=Decimal(value),
            open_risk_cash=Decimal("0"),
        )
        for at, value in (
            (datetime(2024, 1, 8, 15, 0, tzinfo=CN), "100"),
            (datetime(2024, 3, 4, 15, 0, tzinfo=CN), "200"),
            (datetime(2024, 3, 11, 15, 0, tzinfo=CN), "300"),
        )
    )
    run = replace(equity_run("100"), equity_curve=points)

    metrics = calculate_metrics(run)

    assert metrics.worst_week == Decimal("0")


def test_cost_metrics_use_gross_profit_before_costs() -> None:
    trades = (
        trade(0, net_pnl="8", net_return="0.08", total_cost="2"),
        trade(1, net_pnl="-6", net_return="-0.06", total_cost="2"),
    )

    metrics = calculate_metrics(equity_run("100", "102", trades=trades))

    assert metrics.total_cost == Decimal("4")
    assert metrics.cost_to_gross_profit == Decimal("0.4")
    assert metrics.profit_factor == Decimal("1.333333333333333333333333333")


def test_account_cost_and_turnover_include_terminal_open_entry_fill() -> None:
    closed = trade(0, net_pnl="8", net_return="0.08", total_cost="4")
    fills = (
        FillDecision(
            order_id="entry:closed",
            filled=True,
            reason="filled",
            filled_at=START,
            execution_price=Decimal("10"),
            shares=100,
            fees=Decimal("2"),
        ),
        FillDecision(
            order_id="exit:closed",
            filled=True,
            reason="filled",
            filled_at=START + timedelta(hours=1),
            execution_price=Decimal("10.10"),
            shares=100,
            fees=Decimal("2"),
        ),
        FillDecision(
            order_id="entry:terminal-open",
            filled=True,
            reason="filled",
            filled_at=START + timedelta(days=1),
            execution_price=Decimal("5"),
            shares=100,
            fees=Decimal("3"),
        ),
    )
    run = replace(
        equity_run("1000", "1005", trades=(closed,)),
        fills=fills,
    )

    metrics = calculate_metrics(run)

    assert metrics.total_cost == Decimal("7")
    assert metrics.turnover == Decimal("2.51")
    assert metrics.cost_to_gross_profit == Decimal("0.3333333333333333333333333333")


def test_point_type_summaries_remain_independent() -> None:
    trades = (
        trade(0, point_type="1buy"),
        trade(1, point_type="2buy"),
        trade(2, point_type="2buy", net_pnl="-5", net_return="-0.01"),
        trade(3, point_type="3buy"),
    )

    metrics = calculate_metrics(equity_run("100", "104", trades=trades))
    grouped = dict(metrics.per_point_type)

    assert grouped["1buy"].trade_count == 1
    assert grouped["2buy"].trade_count == 2
    assert grouped["3buy"].trade_count == 1


def test_sample_gate_requires_200_and_50_per_enabled_buy_class() -> None:
    enough = tuple(
        trade(
            index,
            point_type=(
                "1buy" if index < 50 else "2buy" if index < 100 else "3buy"
            ),
        )
        for index in range(200)
    )
    adequate = sample_adequacy(equity_run("100", "110", trades=enough))
    missing_one = sample_adequacy(
        replace(
            equity_run("100", "110", trades=enough),
            trades=enough[:-1],
        )
    )
    missing_class = sample_adequacy(
        equity_run(
            "100",
            "110",
            trades=tuple(
                trade(
                    index,
                    point_type=(
                        "1buy"
                        if index < 49
                        else "2buy"
                        if index < 100
                        else "3buy"
                    ),
                )
                for index in range(200)
            ),
        )
    )

    assert adequate.adequate is True
    assert adequate.failures == ()
    assert missing_one.adequate is False
    assert "closed_trades_below_200" in missing_one.failures
    assert "1buy_closed_trades_below_50" in missing_class.failures


def test_clustered_bootstrap_is_seed_deterministic() -> None:
    trades = tuple(
        trade(
            index,
            code="SZ.000001" if index % 2 else "SH.600000",
            sector_id="TDX.880301" if index % 3 else "TDX.880305",
            net_pnl="10" if index % 4 else "-8",
            net_return="0.01" if index % 4 else "-0.008",
        )
        for index in range(24)
    )

    first = clustered_bootstrap(trades, repetitions=100, seed=20260720)
    second = clustered_bootstrap(trades, repetitions=100, seed=20260720)

    assert first == second
    assert first.repetitions == 100
    assert first.cluster_count > 1
    assert first.net_return.lower <= first.net_return.upper


def test_trade_concentration_over_twenty_percent_is_disclosed() -> None:
    trades = tuple(
        trade(
            index,
            code="SZ.000001" if index < 8 else f"SH.60000{index}",
            sector_id="TDX.880301" if index < 8 else "TDX.880305",
        )
        for index in range(10)
    )

    metrics = calculate_metrics(equity_run("100", "105", trades=trades))

    assert metrics.max_symbol_trade_concentration == Decimal("0.8")
    assert metrics.max_sector_trade_concentration == Decimal("0.8")
    assert "symbol_trade_concentration_over_20pct" in metrics.warnings
    assert "sector_trade_concentration_over_20pct" in metrics.warnings
