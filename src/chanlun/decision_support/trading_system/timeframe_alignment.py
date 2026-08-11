from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime
from decimal import Decimal
from typing import Iterable, Literal, Mapping

import pandas as pd

from chanlun.core.strict_structure.models import TrendType
from chanlun.decision_support.fingerprints import normalize_datetime
from chanlun.decision_support.trading_system.backtest.fixed_year import (
    CausalCenterCompletionFact,
)
from chanlun.decision_support.trading_system.models import StructuralPoint
from chanlun.decision_support.trading_system.structure_adapter import (
    has_explicit_small_to_large_second_proof,
)
from chanlun.decision_support.trading_system.parameters import snapshot_sha256


ALIGNMENT_CONTRACT_ID = "L0_COMPLETION_EVIDENCE_L1_L2_CAUSAL"
AlignmentStatus = Literal["PASS", "REJECT"]


@dataclass(frozen=True, slots=True)
class CompletedL1TrendFact:
    """Price-domain copy of one frozen completed 5m level-zero trend."""

    trend_id: str
    source_frequency: str
    recursive_level: int
    price_basis_revision: str
    direction: Literal["up", "down"]
    market_start: datetime
    market_end: datetime
    confirmed_at: datetime
    available_at: datetime
    start_price: Decimal
    end_price: Decimal
    low_price: Decimal
    high_price: Decimal
    terminal_start: datetime
    terminal_end: datetime
    evidence_unit_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        if not self.trend_id:
            raise ValueError("completed L1 trend id is required")
        if self.source_frequency != "5m" or self.recursive_level != 0:
            raise ValueError("L1 trend fact must be independent 5m level zero")
        if self.direction not in ("up", "down"):
            raise ValueError("L1 trend direction must be up or down")
        if not self.price_basis_revision:
            raise ValueError("L1 trend price basis is required")
        for field in (
            "market_start",
            "market_end",
            "confirmed_at",
            "available_at",
            "terminal_start",
            "terminal_end",
        ):
            object.__setattr__(
                self,
                field,
                normalize_datetime(getattr(self, field), field),
            )
        if not (
            self.market_start
            <= self.terminal_start
            <= self.terminal_end
            <= self.market_end
            <= self.confirmed_at
            <= self.available_at
        ):
            raise ValueError("completed L1 trend times are not causal")
        prices = (
            self.start_price,
            self.end_price,
            self.low_price,
            self.high_price,
        )
        if any(price <= 0 for price in prices):
            raise ValueError("completed L1 trend prices must be positive")
        if not self.low_price <= min(self.start_price, self.end_price):
            raise ValueError("completed L1 trend low is inconsistent")
        if not self.high_price >= max(self.start_price, self.end_price):
            raise ValueError("completed L1 trend high is inconsistent")
        if self.direction == "up" and self.end_price < self.start_price:
            raise ValueError("up L1 trend cannot end below its start")
        if self.direction == "down" and self.end_price > self.start_price:
            raise ValueError("down L1 trend cannot end above its start")
        if not self.evidence_unit_ids or len(self.evidence_unit_ids) != len(
            set(self.evidence_unit_ids)
        ):
            raise ValueError("completed L1 trend evidence must be non-empty and unique")

    def document(self) -> dict[str, object]:
        return asdict(self)


def completed_l1_trend_fact(
    trend: TrendType,
    *,
    price_quantum: Decimal,
) -> CompletedL1TrendFact:
    """Copy one immutable core trend without recalculating structure."""

    quantum = Decimal(price_quantum)
    if quantum <= 0:
        raise ValueError("price quantum must be positive")
    if trend.structural_level != 0 or not trend.complete:
        raise ValueError("only completed independent-chart level-zero trends apply")
    if trend.confirmed_at is None:
        raise ValueError("completed L1 trend must carry confirmation time")
    terminal = trend.terminal_unit
    price = lambda tick: quantum * Decimal(tick)
    return CompletedL1TrendFact(
        trend_id=trend.trend_id,
        source_frequency="5m",
        recursive_level=0,
        price_basis_revision=trend.price_basis_revision,
        direction=trend.direction,
        market_start=trend.market_start,
        market_end=trend.market_end,
        confirmed_at=trend.confirmed_at,
        available_at=trend.available_at,
        start_price=price(trend.start_tick),
        end_price=price(trend.end_tick),
        low_price=price(trend.low_tick),
        high_price=price(trend.high_tick),
        terminal_start=terminal.market_start,
        terminal_end=terminal.market_end,
        evidence_unit_ids=tuple(unit.unit_id for unit in trend.constituent_units),
    )


@dataclass(frozen=True, slots=True)
class AlignmentContract:
    contract_id: str = ALIGNMENT_CONTRACT_ID
    l0_departure_window: str = "CENTER_COMPLETION_LEAVE_UNIT_MARKET_INTERVAL"
    l0_return_window: str = "CENTER_COMPLETION_RETURN_UNIT_MARKET_INTERVAL"
    l1_evidence_kind: str = "LOCKED_COMPLETED_5M_LEVEL_ZERO_TREND"
    l1_departure_selection: str = (
        "FIRST_COMPLETED_UP_TREND_TERMINAL_OVERLAPS_LEAVE_UNIT"
    )
    l1_return_selection: str = (
        "FIRST_SUBSEQUENT_COMPLETED_DOWN_TREND_TERMINAL_OVERLAPS_RETURN_UNIT"
    )
    l2_locator_window: str = "L1_RETURN_TERMINAL_UNIT"
    decision_time: str = "MAX_EVIDENCE_AVAILABLE_AT"
    confirmation_price_boundary: str = "RAW_HIGH_OF_FIRST_AVAILABILITY_BAR"
    stale_point_reuse_allowed: bool = False

    def __post_init__(self) -> None:
        if self.contract_id != ALIGNMENT_CONTRACT_ID:
            raise ValueError("alignment contract identity is frozen")
        if self.stale_point_reuse_allowed:
            raise ValueError("alignment cannot reuse stale locator points")

    def document(self) -> dict[str, object]:
        return asdict(self)

    @property
    def parameter_set_id(self) -> str:
        return snapshot_sha256(self.document())


@dataclass(frozen=True, slots=True)
class ConfirmationBarFact:
    point_id: str
    source_frequency: str
    available_at: datetime
    raw_open: Decimal
    raw_high: Decimal
    raw_low: Decimal
    raw_close: Decimal

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "available_at",
            normalize_datetime(self.available_at, "available_at"),
        )
        if not self.point_id or self.source_frequency != "1m":
            raise ValueError("strict strategy confirmation bar identity is invalid")
        prices = (self.raw_open, self.raw_high, self.raw_low, self.raw_close)
        if any(value <= 0 for value in prices):
            raise ValueError("strict strategy confirmation bar prices must be positive")
        if self.raw_low > min(self.raw_open, self.raw_close):
            raise ValueError("strict strategy confirmation bar low is inconsistent")
        if self.raw_high < max(self.raw_open, self.raw_close):
            raise ValueError("strict strategy confirmation bar high is inconsistent")

    def document(self) -> dict[str, object]:
        return asdict(self)


def confirmation_bar_fact(
    point: StructuralPoint,
    frame: pd.DataFrame,
) -> ConfirmationBarFact:
    """Copy the raw bar that first made a frozen point observable.

    Using ``structure_anchor_price`` here is incorrect for a first buy because
    that value is the structural low, not the confirmation/availability bar's
    executable upper boundary.
    """

    if point.source_frequency != "1m" or not point.confirmed:
        raise ValueError("strict strategy confirmation evidence requires a confirmed 1m point")
    if "date" not in frame.columns:
        raise ValueError("strict strategy confirmation frame requires date")
    matches = frame.loc[pd.to_datetime(frame["date"]) == point.available_at]
    if len(matches) != 1:
        raise ValueError("strict strategy point availability must map to exactly one raw bar")
    row = matches.iloc[0]
    required = ("raw_open", "raw_high", "raw_low", "raw_close")
    if any(field not in frame.columns for field in required):
        raise ValueError("strict strategy confirmation requires original raw OHLC")
    return ConfirmationBarFact(
        point_id=point.point_id,
        source_frequency=point.source_frequency,
        available_at=point.available_at,
        raw_open=Decimal(str(row["raw_open"])),
        raw_high=Decimal(str(row["raw_high"])),
        raw_low=Decimal(str(row["raw_low"])),
        raw_close=Decimal(str(row["raw_close"])),
    )


@dataclass(frozen=True, slots=True)
class AlignedEntryChain:
    l0_point_id: str
    l0_center_id: str
    l1_departure_evidence_id: str
    l1_return_evidence_id: str
    l1_evidence_kind: str
    l2_locator_point_id: str
    decision_at: datetime
    return_low: Decimal
    l0_zg: Decimal
    l2_confirmation_bar_high: Decimal
    structural_invalidation_price: Decimal

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "decision_at", normalize_datetime(self.decision_at, "decision_at")
        )
        if self.return_low < self.l0_zg:
            raise ValueError("strict strategy first return must hold L0 ZG")
        if self.structural_invalidation_price != self.l0_zg:
            raise ValueError("strict strategy third-buy invalidation must be L0 ZG")
        if self.l2_confirmation_bar_high <= 0:
            raise ValueError("strict strategy entry price boundary must be positive")
        if self.l1_evidence_kind not in {
            "COMPLETED_TREND",
            "COMPLETED_CONSTITUENT_UNIT",
        }:
            raise ValueError("strict strategy L1 evidence kind is unsupported")

    def document(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class AlignmentDecision:
    l0_point_id: str
    window_start: datetime
    window_end: datetime
    status: AlignmentStatus
    reason_codes: tuple[str, ...]
    chain: AlignedEntryChain | None = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "window_start",
            normalize_datetime(self.window_start, "window_start"),
        )
        object.__setattr__(
            self, "window_end", normalize_datetime(self.window_end, "window_end")
        )
        object.__setattr__(self, "reason_codes", tuple(self.reason_codes))
        if self.window_start > self.window_end:
            raise ValueError("strict strategy alignment window is reversed")
        if self.status == "PASS" and (self.chain is None or self.reason_codes):
            raise ValueError("strict strategy passing alignment requires a clean chain")
        if self.status == "REJECT" and (self.chain is not None or not self.reason_codes):
            raise ValueError("strict strategy rejection requires reasons")

    def document(self) -> dict[str, object]:
        return asdict(self)


def _reject(
    point: StructuralPoint,
    *,
    start: datetime,
    end: datetime,
    reason: str,
) -> AlignmentDecision:
    return AlignmentDecision(
        l0_point_id=point.point_id,
        window_start=start,
        window_end=end,
        status="REJECT",
        reason_codes=(reason,),
    )


def _intervals_overlap(
    left_start: datetime,
    left_end: datetime,
    right_start: datetime,
    right_end: datetime,
) -> bool:
    """Return inclusive market-time overlap for independent chart partitions."""

    return left_start <= right_end and right_start <= left_end


def align_independent_entry_chains(
    *,
    l0_points: Iterable[StructuralPoint],
    l0_center_completions: Iterable[CausalCenterCompletionFact],
    l1_trends: Iterable[CompletedL1TrendFact],
    l2_points: Iterable[StructuralPoint],
    confirmation_bars: Mapping[str, ConfirmationBarFact],
    l0_price_quantum: Decimal,
    allowed_l2_second_buy_ids: Iterable[str] = (),
) -> tuple[AlignmentDecision, ...]:
    quantum = Decimal(l0_price_quantum)
    if quantum <= 0:
        raise ValueError("strict strategy L0 price quantum must be positive")
    centers = {
        (fact.center_id, fact.structural_level): fact
        for fact in l0_center_completions
        if fact.source_frequency == "30m"
    }
    trends = tuple(l1_trends)
    locators = tuple(l2_points)
    allowed_second = frozenset(allowed_l2_second_buy_ids)
    locator_by_id = {point.point_id: point for point in locators}
    proven_second = frozenset(
        point.point_id
        for point in locators
        if has_explicit_small_to_large_second_proof(
            point,
            points_by_id=locator_by_id,
        )
    )
    decisions: list[AlignmentDecision] = []

    for l0 in sorted(l0_points, key=lambda item: (item.available_at, item.point_id)):
        if (
            not l0.confirmed
            or l0.source_frequency != "30m"
            or l0.recursive_level != 0
            or l0.point_type != "3buy"
            or l0.center_ordinal != 1
            or l0.center_id is None
            or l0.center_zg is None
        ):
            raise ValueError("strict strategy alignment requires a frozen L0 first-center 3buy")
        center = centers.get((l0.center_id, 0))
        if center is None:
            decisions.append(
                _reject(
                    l0,
                    start=l0.anchor_at,
                    end=l0.available_at,
                    reason="L0_CENTER_COMPLETION_EVIDENCE_MISSING",
                )
            )
            continue
        start = center.leave_market_start
        end = center.return_market_end
        if center.price_basis_revision != l0.price_basis_revision:
            decisions.append(
                _reject(l0, start=start, end=end, reason="L0_CENTER_PRICE_BASIS_MISMATCH")
            )
            continue
        if center.available_at > l0.available_at:
            decisions.append(
                _reject(l0, start=start, end=end, reason="L0_CENTER_EVIDENCE_NOT_YET_VISIBLE")
            )
            continue
        zg = quantum * center.zg_tick
        matching_departures = tuple(
            sorted(
                (
                    trend
                    for trend in trends
                    if trend.price_basis_revision == l0.price_basis_revision
                    and trend.direction == "up"
                    and _intervals_overlap(
                        trend.terminal_start,
                        trend.terminal_end,
                        center.leave_market_start,
                        center.leave_market_end,
                    )
                    and trend.available_at <= l0.available_at
                    and trend.end_price > zg
                ),
                key=lambda item: (
                    item.market_end,
                    item.market_start,
                    item.available_at,
                    item.trend_id,
                ),
            )
        )
        departure = matching_departures[0] if matching_departures else None
        if departure is None:
            decisions.append(
                _reject(
                    l0,
                    start=start,
                    end=end,
                    reason="NO_COMPLETED_L1_UP_DEPARTURE_ALIGNED_WITH_L0_LEAVE_UNIT",
                )
            )
            continue
        subsequent_returns = tuple(
            sorted(
                (
                    trend
                    for trend in trends
                    if trend.price_basis_revision == l0.price_basis_revision
                    and trend.direction == "down"
                    and trend.market_start >= departure.market_end
                    and trend.available_at >= departure.available_at
                    and trend.available_at <= l0.available_at
                ),
                key=lambda item: (
                    item.market_start,
                    item.market_end,
                    item.available_at,
                    item.trend_id,
                ),
            )
        )
        first_return = subsequent_returns[0] if subsequent_returns else None
        if first_return is None:
            decisions.append(
                _reject(
                    l0,
                    start=start,
                    end=end,
                    reason="NO_SUBSEQUENT_COMPLETED_L1_DOWN_RETURN",
                )
            )
            continue
        if not _intervals_overlap(
            first_return.terminal_start,
            first_return.terminal_end,
            center.return_market_start,
            center.return_market_end,
        ):
            decisions.append(
                _reject(
                    l0,
                    start=start,
                    end=end,
                    reason=(
                        "FIRST_COMPLETED_L1_DOWN_RETURN_NOT_ALIGNED_WITH_"
                        "L0_RETURN_UNIT"
                    ),
                )
            )
            continue
        if first_return.low_price < zg:
            decisions.append(
                _reject(l0, start=start, end=end, reason="FIRST_L1_RETURN_LOW_BELOW_L0_ZG")
            )
            continue
        locator = next(
            (
                point
                for point in sorted(
                    locators, key=lambda item: (item.available_at, item.point_id)
                )
                if point.price_basis_revision == l0.price_basis_revision
                and point.side == "buy"
                and point.point_type in {"1buy", "2buy"}
                and (
                    point.point_type == "1buy"
                    or (
                        point.point_id in allowed_second
                        and point.point_id in proven_second
                    )
                )
                and first_return.terminal_start <= point.anchor_at <= first_return.terminal_end
                and point.available_at <= l0.available_at
            ),
            None,
        )
        if locator is None:
            decisions.append(
                _reject(
                    l0,
                    start=start,
                    end=end,
                    reason="NO_L2_LOCATOR_AT_FIRST_L1_RETURN_TERMINAL",
                )
            )
            continue
        bar = confirmation_bars.get(locator.point_id)
        if bar is None or bar.available_at != locator.available_at:
            decisions.append(
                _reject(l0, start=start, end=end, reason="L2_CONFIRMATION_BAR_EVIDENCE_MISSING")
            )
            continue
        chain = AlignedEntryChain(
            l0_point_id=l0.point_id,
            l0_center_id=l0.center_id,
            l1_departure_evidence_id=departure.trend_id,
            l1_return_evidence_id=first_return.trend_id,
            l1_evidence_kind="COMPLETED_TREND",
            l2_locator_point_id=locator.point_id,
            decision_at=max(
                l0.available_at,
                center.available_at,
                departure.available_at,
                first_return.available_at,
                locator.available_at,
                bar.available_at,
            ),
            return_low=first_return.low_price,
            l0_zg=zg,
            l2_confirmation_bar_high=bar.raw_high,
            structural_invalidation_price=zg,
        )
        decisions.append(
            AlignmentDecision(
                l0_point_id=l0.point_id,
                window_start=start,
                window_end=end,
                status="PASS",
                reason_codes=(),
                chain=chain,
            )
        )
    return tuple(decisions)


def alignment_contract() -> AlignmentContract:
    return AlignmentContract()


__all__ = [
    "ConfirmationBarFact",
    "CompletedL1TrendFact",
    "AlignedEntryChain",
    "AlignmentContract",
    "AlignmentDecision",
    "align_independent_entry_chains",
    "completed_l1_trend_fact",
    "confirmation_bar_fact",
    "alignment_contract",
]
