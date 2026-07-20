from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal


@dataclass(frozen=True, slots=True)
class RiskLimits:
    base_trade_risk: Decimal = Decimal("0.005")
    max_symbol_fraction: Decimal = Decimal("0.10")
    max_sector_fraction: Decimal = Decimal("0.20")
    max_portfolio_heat: Decimal = Decimal("0.02")
    first_drawdown: Decimal = Decimal("0.05")
    second_drawdown: Decimal = Decimal("0.075")
    stop_drawdown: Decimal = Decimal("0.10")
    lot_size: int = 100

    def __post_init__(self) -> None:
        fractions = (
            self.base_trade_risk,
            self.max_symbol_fraction,
            self.max_sector_fraction,
            self.max_portfolio_heat,
        )
        if any(value <= 0 for value in fractions):
            raise ValueError("risk fractions must be positive")
        if not (
            Decimal("0")
            < self.first_drawdown
            < self.second_drawdown
            < self.stop_drawdown
        ):
            raise ValueError("drawdown thresholds must be strictly increasing")
        if self.lot_size <= 0:
            raise ValueError("lot_size must be positive")


@dataclass(frozen=True, slots=True)
class PortfolioSnapshot:
    equity: Decimal
    available_cash: Decimal
    drawdown: Decimal
    open_risk_cash: Decimal
    sector_market_values: tuple[tuple[str, Decimal], ...] = ()

    def __post_init__(self) -> None:
        if self.equity <= 0:
            raise ValueError("equity must be positive")
        if self.available_cash < 0 or self.open_risk_cash < 0:
            raise ValueError("cash and open risk cannot be negative")
        if self.drawdown < 0:
            raise ValueError("drawdown cannot be negative")
        sectors = tuple(sector_id for sector_id, _value in self.sector_market_values)
        if len(sectors) != len(set(sectors)):
            raise ValueError("sector market values must be unique")
        if any(value < 0 for _sector_id, value in self.sector_market_values):
            raise ValueError("sector market value cannot be negative")

    def sector_market_value(self, sector_id: str) -> Decimal:
        return dict(self.sector_market_values).get(sector_id, Decimal("0"))


@dataclass(frozen=True, slots=True)
class RiskCandidate:
    signal_id: str
    sector_id: str
    entry_price: Decimal
    stop_price: Decimal
    risk_multiplier: Decimal

    def __post_init__(self) -> None:
        if self.entry_price <= 0 or self.stop_price <= 0:
            raise ValueError("entry and stop prices must be positive")
        if self.risk_multiplier < 0:
            raise ValueError("risk_multiplier cannot be negative")


@dataclass(frozen=True, slots=True)
class RiskSizedOrder:
    signal_id: str
    shares: int
    planned_risk_cash: Decimal
    drawdown_factor: Decimal
    reason_codes: tuple[str, ...]

    @classmethod
    def blocked(
        cls,
        signal_id: str,
        factor: Decimal,
        reason: str,
    ) -> "RiskSizedOrder":
        return cls(signal_id, 0, Decimal("0"), factor, (reason,))

    @classmethod
    def from_shares(
        cls,
        candidate: RiskCandidate,
        shares: int,
        risk_budget: Decimal,
        factor: Decimal,
        reason_codes: tuple[str, ...],
    ) -> "RiskSizedOrder":
        if shares <= 0:
            return cls.blocked(candidate.signal_id, factor, "zero_shares")
        planned = (candidate.entry_price - candidate.stop_price) * shares
        if planned > risk_budget:
            return cls.blocked(
                candidate.signal_id,
                factor,
                "risk_budget_exceeded",
            )
        return cls(
            candidate.signal_id,
            shares,
            planned,
            factor,
            reason_codes,
        )


def _drawdown_factor(drawdown: Decimal, limits: RiskLimits) -> Decimal:
    if drawdown >= limits.stop_drawdown:
        return Decimal("0")
    if drawdown >= limits.second_drawdown:
        return Decimal("0.25")
    if drawdown >= limits.first_drawdown:
        return Decimal("0.50")
    return Decimal("1")


def size_entry(
    *,
    portfolio: PortfolioSnapshot,
    candidate: RiskCandidate,
    limits: RiskLimits,
) -> RiskSizedOrder:
    factor = _drawdown_factor(portfolio.drawdown, limits)
    if factor == 0:
        return RiskSizedOrder.blocked(
            candidate.signal_id,
            factor,
            "drawdown_stop_gate",
        )
    risk_per_share = candidate.entry_price - candidate.stop_price
    if risk_per_share <= 0:
        return RiskSizedOrder.blocked(
            candidate.signal_id,
            factor,
            "invalid_structural_stop",
        )
    if candidate.risk_multiplier == 0:
        return RiskSizedOrder.blocked(
            candidate.signal_id,
            factor,
            "risk_multiplier_zero",
        )
    risk_cash = (
        portfolio.equity
        * limits.base_trade_risk
        * candidate.risk_multiplier
        * factor
    )
    sector_cash = (
        portfolio.equity * limits.max_sector_fraction
        - portfolio.sector_market_value(candidate.sector_id)
    )
    heat_cash = (
        portfolio.equity * limits.max_portfolio_heat
        - portfolio.open_risk_cash
    )
    capacities = (
        ("risk_budget_cap", int(risk_cash / risk_per_share)),
        (
            "symbol_cap",
            int(
                portfolio.equity
                * limits.max_symbol_fraction
                / candidate.entry_price
            ),
        ),
        (
            "sector_cap",
            int(max(Decimal("0"), sector_cash) / candidate.entry_price),
        ),
        (
            "portfolio_heat_cap",
            int(max(Decimal("0"), heat_cash) / risk_per_share),
        ),
        ("cash_cap", int(portfolio.available_cash / candidate.entry_price)),
    )
    raw = min(shares for _name, shares in capacities)
    binding = tuple(name for name, shares in capacities if shares == raw)
    shares = raw // limits.lot_size * limits.lot_size
    return RiskSizedOrder.from_shares(
        candidate,
        shares,
        risk_cash,
        factor,
        binding,
    )


__all__ = [
    "PortfolioSnapshot",
    "RiskCandidate",
    "RiskLimits",
    "RiskSizedOrder",
    "size_entry",
]
