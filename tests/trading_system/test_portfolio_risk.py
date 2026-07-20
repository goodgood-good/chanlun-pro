from dataclasses import replace
from decimal import Decimal

import pytest

from chanlun.decision_support.trading_system.portfolio_risk import (
    PortfolioSnapshot,
    RiskCandidate,
    RiskLimits,
    size_entry,
)


def portfolio(
    *,
    equity: str = "100000",
    cash: str = "100000",
    drawdown: str = "0",
    open_risk: str = "0",
    sector_value: str = "0",
) -> PortfolioSnapshot:
    return PortfolioSnapshot(
        equity=Decimal(equity),
        available_cash=Decimal(cash),
        drawdown=Decimal(drawdown),
        open_risk_cash=Decimal(open_risk),
        sector_market_values=(("TDX.880301", Decimal(sector_value)),),
    )


def candidate(
    *,
    entry: str = "10.00",
    stop: str = "9.80",
    multiplier: str = "1.00",
) -> RiskCandidate:
    return RiskCandidate(
        signal_id="signal-a",
        sector_id="TDX.880301",
        entry_price=Decimal(entry),
        stop_price=Decimal(stop),
        risk_multiplier=Decimal(multiplier),
    )


def test_wider_stop_reduces_position_size() -> None:
    narrow = size_entry(
        portfolio=portfolio(),
        candidate=candidate(entry="10.00", stop="9.80", multiplier="1.00"),
        limits=RiskLimits(),
    )
    wide = size_entry(
        portfolio=portfolio(),
        candidate=candidate(entry="10.00", stop="9.00", multiplier="1.00"),
        limits=RiskLimits(),
    )

    assert narrow.shares > wide.shares > 0


@pytest.mark.parametrize(
    ("drawdown", "factor"),
    (
        ("0.049", "1.00"),
        ("0.050", "0.50"),
        ("0.075", "0.25"),
        ("0.100", "0.00"),
    ),
)
def test_drawdown_gate_is_monotonic(drawdown: str, factor: str) -> None:
    order = size_entry(
        portfolio=portfolio(drawdown=drawdown),
        candidate=candidate(),
        limits=RiskLimits(),
    )

    assert order.drawdown_factor == Decimal(factor)


@pytest.mark.parametrize(
    ("multiplier", "expected_shares"),
    (("0.50", 200), ("1.00", 500), ("0.75", 300)),
)
def test_point_risk_multipliers_are_independent(
    multiplier: str,
    expected_shares: int,
) -> None:
    limits = replace(
        RiskLimits(),
        max_symbol_fraction=Decimal("1"),
        max_sector_fraction=Decimal("1"),
        max_portfolio_heat=Decimal("1"),
    )

    order = size_entry(
        portfolio=portfolio(),
        candidate=candidate(stop="9.00", multiplier=multiplier),
        limits=limits,
    )

    assert order.shares == expected_shares


def test_single_symbol_cap_limits_market_value() -> None:
    order = size_entry(
        portfolio=portfolio(),
        candidate=candidate(stop="9.99"),
        limits=RiskLimits(),
    )

    assert order.shares * Decimal("10") <= Decimal("10000")
    assert "symbol_cap" in order.reason_codes


def test_sector_cap_uses_existing_sector_exposure() -> None:
    order = size_entry(
        portfolio=portfolio(sector_value="19000"),
        candidate=candidate(),
        limits=RiskLimits(),
    )

    assert order.shares == 100
    assert "sector_cap" in order.reason_codes


def test_portfolio_heat_cap_uses_open_risk() -> None:
    order = size_entry(
        portfolio=portfolio(open_risk="1900"),
        candidate=candidate(),
        limits=RiskLimits(),
    )

    assert order.shares == 500
    assert "portfolio_heat_cap" in order.reason_codes


def test_cash_cap_prevents_unfunded_position() -> None:
    order = size_entry(
        portfolio=portfolio(cash="1500"),
        candidate=candidate(),
        limits=RiskLimits(),
    )

    assert order.shares == 100
    assert "cash_cap" in order.reason_codes


def test_lot_rounding_never_rounds_up() -> None:
    limits = replace(
        RiskLimits(),
        max_symbol_fraction=Decimal("1"),
        max_sector_fraction=Decimal("1"),
        max_portfolio_heat=Decimal("1"),
    )

    order = size_entry(
        portfolio=portfolio(),
        candidate=candidate(stop="8.00"),
        limits=limits,
    )

    assert order.shares == 200
    assert order.shares % limits.lot_size == 0
