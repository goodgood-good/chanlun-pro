from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import math
from typing import Iterable

from .fingerprints import normalize_datetime
from .market_rules import a_share_board, a_share_limit_pct, is_st_name
from .models import MarketConstraints


@dataclass(frozen=True, slots=True)
class SecuritySnapshot:
    market: str
    code: str
    name: str
    listed_days: int | None
    suspended: bool | None
    delisting: bool | None
    avg_turnover_20d: float | None
    quote_time: datetime | None
    limit_up_locked: bool | None
    limit_down_locked: bool | None

    def __post_init__(self) -> None:
        for field_name in ("market", "code", "name"):
            value = getattr(self, field_name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{field_name} must be a non-empty string")
        if self.listed_days is not None and (
            isinstance(self.listed_days, bool)
            or not isinstance(self.listed_days, int)
            or self.listed_days < 0
        ):
            raise ValueError("listed_days must be a non-negative integer")
        for field_name in ("suspended", "delisting", "limit_up_locked", "limit_down_locked"):
            value = getattr(self, field_name)
            if value is not None and type(value) is not bool:
                raise ValueError(f"{field_name} must be boolean")
        turnover = self.avg_turnover_20d
        if turnover is not None:
            if (
                isinstance(turnover, bool)
                or not isinstance(turnover, (int, float))
                or not math.isfinite(float(turnover))
                or float(turnover) < 0
            ):
                raise ValueError("avg_turnover_20d must be a non-negative number")
            object.__setattr__(self, "avg_turnover_20d", float(turnover))
        if self.quote_time is not None:
            object.__setattr__(
                self,
                "quote_time",
                normalize_datetime(self.quote_time, "quote_time"),
            )


@dataclass(frozen=True, slots=True)
class EligibleSecurity:
    security: SecuritySnapshot
    board: str
    limit_pct: float
    entry_tradable: bool
    exit_tradable: bool
    quote_time: datetime

    @property
    def market(self) -> str:
        return self.security.market

    @property
    def code(self) -> str:
        return self.security.code

    @property
    def name(self) -> str:
        return self.security.name

    def market_constraints(self) -> MarketConstraints:
        return MarketConstraints(
            board=self.board,
            lot=100,
            t_plus=1,
            limit_pct=self.limit_pct,
            entry_tradable=self.entry_tradable,
            exit_tradable=self.exit_tradable,
            quote_time=self.quote_time,
            limit_up_locked=bool(self.security.limit_up_locked),
            limit_down_locked=bool(self.security.limit_down_locked),
        )


@dataclass(frozen=True, slots=True)
class UniverseExclusion:
    security: SecuritySnapshot
    reason: str

    @property
    def code(self) -> str:
        return self.security.code


@dataclass(frozen=True, slots=True)
class UniverseResult:
    included: tuple[EligibleSecurity, ...]
    excluded: tuple[UniverseExclusion, ...]


@dataclass(frozen=True, slots=True)
class UniversePolicy:
    min_listed_days: int = 60
    min_avg_turnover_20d: float = 100_000_000.0
    exclude_st: bool = True
    exclude_delisting: bool = True
    require_complete_metadata: bool = True

    def __post_init__(self) -> None:
        if (
            isinstance(self.min_listed_days, bool)
            or not isinstance(self.min_listed_days, int)
            or self.min_listed_days < 0
        ):
            raise ValueError("min_listed_days must be a non-negative integer")
        if (
            isinstance(self.min_avg_turnover_20d, bool)
            or not isinstance(self.min_avg_turnover_20d, (int, float))
            or not math.isfinite(float(self.min_avg_turnover_20d))
            or float(self.min_avg_turnover_20d) < 0
        ):
            raise ValueError("min_avg_turnover_20d must be non-negative")
        object.__setattr__(
            self,
            "min_avg_turnover_20d",
            float(self.min_avg_turnover_20d),
        )
        for field_name in (
            "exclude_st",
            "exclude_delisting",
            "require_complete_metadata",
        ):
            if type(getattr(self, field_name)) is not bool:
                raise ValueError(f"{field_name} must be boolean")

    @classmethod
    def a_share_short_term(cls) -> UniversePolicy:
        return cls()


def _missing_metadata(security: SecuritySnapshot) -> bool:
    return any(
        getattr(security, field_name) is None
        for field_name in (
            "listed_days",
            "suspended",
            "delisting",
            "avg_turnover_20d",
            "quote_time",
            "limit_up_locked",
            "limit_down_locked",
        )
    )


def _exclusion_reason(
    security: SecuritySnapshot,
    asof: datetime,
    policy: UniversePolicy,
) -> tuple[str | None, str | None]:
    if security.market.strip().casefold() != "a":
        return "unsupported_market", None
    board = a_share_board(security.code, security.name)
    if board is None:
        return "invalid_code", None
    if policy.require_complete_metadata and _missing_metadata(security):
        return "missing_metadata", board
    if policy.exclude_st and is_st_name(security.name):
        return "st", board
    if policy.exclude_delisting and (
        bool(security.delisting) or "退" in security.name
    ):
        return "delisting", board
    if (
        security.listed_days is not None
        and security.listed_days < policy.min_listed_days
    ):
        return "new_listing", board
    if security.suspended:
        return "suspended", board
    if (
        security.avg_turnover_20d is not None
        and security.avg_turnover_20d < policy.min_avg_turnover_20d
    ):
        return "low_liquidity", board
    if security.quote_time is not None and security.quote_time > asof:
        return "future_quote", board
    return None, board


def filter_universe(
    securities: Iterable[SecuritySnapshot],
    asof: datetime,
    policy: UniversePolicy,
) -> UniverseResult:
    asof = normalize_datetime(asof, "asof")
    if not isinstance(policy, UniversePolicy):
        raise TypeError("policy must be UniversePolicy")
    values = tuple(securities)
    if not all(isinstance(item, SecuritySnapshot) for item in values):
        raise TypeError("securities must contain SecuritySnapshot values")

    included: list[EligibleSecurity] = []
    excluded: list[UniverseExclusion] = []
    for security in sorted(values, key=lambda item: item.code):
        reason, board = _exclusion_reason(security, asof, policy)
        if reason is not None or board is None:
            excluded.append(
                UniverseExclusion(security, reason or "invalid_code")
            )
            continue
        limit_pct = a_share_limit_pct(board)
        if limit_pct is None:
            excluded.append(UniverseExclusion(security, "invalid_code"))
            continue
        quote_time = security.quote_time or asof
        included.append(
            EligibleSecurity(
                security=security,
                board=board,
                limit_pct=float(limit_pct),
                entry_tradable=(
                    not bool(security.suspended)
                    and not bool(security.limit_up_locked)
                ),
                exit_tradable=(
                    not bool(security.suspended)
                    and not bool(security.limit_down_locked)
                ),
                quote_time=quote_time,
            )
        )
    return UniverseResult(tuple(included), tuple(excluded))
