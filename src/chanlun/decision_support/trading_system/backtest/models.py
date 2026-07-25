from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Literal

from chanlun.decision_support.fingerprints import normalize_datetime


def _validate_ohlc(
    *,
    label: str,
    opened: Decimal,
    high: Decimal,
    low: Decimal,
    closed: Decimal,
) -> None:
    if any(value <= 0 for value in (opened, high, low, closed)):
        raise ValueError(f"{label} prices must be positive")
    if low > min(opened, closed) or high < max(opened, closed) or low > high:
        raise ValueError(f"{label} OHLC range is inconsistent")


@dataclass(frozen=True, slots=True)
class MinuteBar:
    code: str
    opened_at: datetime
    closed_at: datetime
    raw_open: Decimal
    raw_high: Decimal
    raw_low: Decimal
    raw_close: Decimal
    analysis_open: Decimal
    analysis_high: Decimal
    analysis_low: Decimal
    analysis_close: Decimal
    previous_raw_close: Decimal
    volume: Decimal
    turnover: Decimal
    adjustment_known_at: datetime

    def __post_init__(self) -> None:
        opened_at = normalize_datetime(self.opened_at, "opened_at")
        closed_at = normalize_datetime(self.closed_at, "closed_at")
        known_at = normalize_datetime(
            self.adjustment_known_at,
            "adjustment_known_at",
        )
        if opened_at >= closed_at:
            raise ValueError("opened_at must precede closed_at")
        if known_at > closed_at:
            raise ValueError("adjustment_known_at cannot follow closed_at")
        object.__setattr__(self, "opened_at", opened_at)
        object.__setattr__(self, "closed_at", closed_at)
        object.__setattr__(self, "adjustment_known_at", known_at)
        _validate_ohlc(
            label="raw",
            opened=self.raw_open,
            high=self.raw_high,
            low=self.raw_low,
            closed=self.raw_close,
        )
        _validate_ohlc(
            label="analysis",
            opened=self.analysis_open,
            high=self.analysis_high,
            low=self.analysis_low,
            closed=self.analysis_close,
        )
        if self.previous_raw_close <= 0:
            raise ValueError("previous_raw_close must be positive")
        if self.volume < 0 or self.turnover < 0:
            raise ValueError("volume and turnover cannot be negative")


@dataclass(frozen=True, slots=True)
class SecurityStatus:
    session: date
    code: str
    listed: bool
    st: bool
    suspended: bool
    limit_pct: Decimal
    lot_size: int
    t_plus_days: int

    def __post_init__(self) -> None:
        if self.limit_pct <= 0:
            raise ValueError("limit_pct must be positive")
        if self.lot_size <= 0 or self.t_plus_days < 0:
            raise ValueError("invalid settlement constraints")


@dataclass(frozen=True, slots=True)
class SectorMembershipAt:
    session: date
    sector_id: str
    code: str
    known_at: datetime

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "known_at",
            normalize_datetime(self.known_at, "known_at"),
        )


@dataclass(frozen=True, slots=True)
class CorporateActionAt:
    code: str
    effective_at: datetime
    known_at: datetime
    action_type: Literal["cash_dividend", "split", "rights"]
    cash_per_share: Decimal = Decimal("0")
    share_multiplier: Decimal = Decimal("1")
    subscription_cost_per_share: Decimal = Decimal("0")
    raw_price_divisor: Decimal = Decimal("1")

    def __post_init__(self) -> None:
        effective_at = normalize_datetime(self.effective_at, "effective_at")
        known_at = normalize_datetime(self.known_at, "known_at")
        if known_at > effective_at:
            raise ValueError("corporate action cannot be known after effective_at")
        if (
            self.cash_per_share < 0
            or self.share_multiplier <= 0
            or self.subscription_cost_per_share < 0
            or self.raw_price_divisor <= 0
        ):
            raise ValueError("invalid corporate action economics")
        object.__setattr__(self, "effective_at", effective_at)
        object.__setattr__(self, "known_at", known_at)


@dataclass(frozen=True, slots=True)
class BacktestDataset:
    bars: tuple[MinuteBar, ...]
    statuses: tuple[SecurityStatus, ...]
    memberships: tuple[SectorMembershipAt, ...]
    corporate_actions: tuple[CorporateActionAt, ...]
    membership_as_of_each_session: bool
    point_in_time_adjustment: bool
    source_hashes: tuple[tuple[str, str], ...]
    security_status_as_of_each_session: bool = True

    def __post_init__(self) -> None:
        bar_keys = tuple((bar.code, bar.opened_at) for bar in self.bars)
        if len(bar_keys) != len(set(bar_keys)):
            raise ValueError("duplicate minute bar key")
        status_keys = tuple((row.code, row.session) for row in self.statuses)
        if len(status_keys) != len(set(status_keys)):
            raise ValueError("duplicate security status key")
        membership_keys = tuple(
            (row.session, row.sector_id, row.code) for row in self.memberships
        )
        if len(membership_keys) != len(set(membership_keys)):
            raise ValueError("duplicate sector membership key")
        source_names = tuple(name for name, _digest in self.source_hashes)
        if len(source_names) != len(set(source_names)):
            raise ValueError("duplicate source hash name")
        object.__setattr__(self, "source_hashes", tuple(sorted(self.source_hashes)))

    def status_at(self, code: str, session: date) -> SecurityStatus:
        matches = tuple(
            row
            for row in self.statuses
            if row.code == code and row.session == session
        )
        if len(matches) != 1:
            raise KeyError(f"security status is not unique: {code} {session}")
        return matches[0]

    def actions_at(self, closed_at: datetime) -> tuple[CorporateActionAt, ...]:
        normalized = normalize_datetime(closed_at, "closed_at")
        return tuple(
            action
            for action in self.corporate_actions
            if action.effective_at == normalized and action.known_at <= normalized
        )

    @property
    def last_closed_at(self) -> datetime:
        if not self.bars:
            raise ValueError("dataset has no bars")
        return max(bar.closed_at for bar in self.bars)
