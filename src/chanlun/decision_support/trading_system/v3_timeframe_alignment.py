from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime
from decimal import Decimal
from typing import Iterable, Literal

from chanlun.core.strict_structure.models import TrendType
from chanlun.decision_support.fingerprints import normalize_datetime
from chanlun.decision_support.trading_system.models import StructuralPoint
from chanlun.decision_support.trading_system.v3_parameters import snapshot_sha256
from chanlun.decision_support.trading_system.v3_timeframe_override import (
    ENTRY_ALIGNMENT_CONTRACT_ID,
    independent_timeframe_override,
)


AlignmentStatus = Literal["PASS", "REJECT"]


@dataclass(frozen=True, slots=True)
class IndependentAlignmentContract:
    """Frozen causal pairing rule for the user-authorized chart override."""

    contract_id: str = ENTRY_ALIGNMENT_CONTRACT_ID
    l0_window_start: str = "POINT_ANCHOR_AT"
    l0_window_end: str = "POINT_AVAILABLE_AT"
    window_boundaries: str = "INCLUSIVE"
    l1_departure_selection: str = "FIRST_MATCHING_COMPLETE_UP_TREND"
    l1_departure_start: str = "INSIDE_L0_CENTER_INCLUSIVE"
    l1_departure_end: str = "STRICTLY_ABOVE_L0_ZG"
    l1_return_selection: str = "FIRST_SUBSEQUENT_COMPLETE_DOWN_TREND"
    l1_return_boundary: str = "LOW_GREATER_OR_EQUAL_L0_ZG"
    l2_locator_anchor: str = "INSIDE_L1_RETURN_TERMINAL_UNIT_INCLUSIVE"
    l2_first_buy_allowed: bool = True
    l2_second_buy_requires_explicit_small_to_large_evidence: bool = True
    stale_point_reuse_allowed: bool = False
    user_authorized_on: str = "2026-07-26"

    def __post_init__(self) -> None:
        if self.contract_id != ENTRY_ALIGNMENT_CONTRACT_ID:
            raise ValueError("independent alignment contract id is frozen")
        if self.window_boundaries != "INCLUSIVE":
            raise ValueError("independent alignment boundaries are inclusive")
        if self.stale_point_reuse_allowed:
            raise ValueError("stale structural points cannot be reused")
        if not self.l2_second_buy_requires_explicit_small_to_large_evidence:
            raise ValueError("independent alignment cannot infer an allowed second buy")

    def document(self) -> dict[str, object]:
        return asdict(self)

    @property
    def parameter_set_id(self) -> str:
        return snapshot_sha256(self.document())


@dataclass(frozen=True, slots=True)
class CompletedL1TrendFact:
    """Read-only price-domain copy of a frozen completed 5m level-zero trend."""

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
            object.__setattr__(self, field, normalize_datetime(getattr(self, field), field))
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
    """Copy one immutable core trend without recalculating any structure."""

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
class AlignedEntryChain:
    l0_point_id: str
    l1_departure_trend_id: str
    l1_return_trend_id: str
    l2_locator_point_id: str
    decision_at: datetime
    return_low: Decimal
    l0_zg: Decimal

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "decision_at", normalize_datetime(self.decision_at, "decision_at")
        )
        if self.return_low < self.l0_zg:
            raise ValueError("aligned first return must stay above or equal to L0 ZG")

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
        if self.window_end < self.window_start:
            raise ValueError("alignment window cannot be negative")
        if self.status == "PASS" and (self.chain is None or self.reason_codes):
            raise ValueError("passing alignment requires exactly one clean chain")
        if self.status == "REJECT" and (self.chain is not None or not self.reason_codes):
            raise ValueError("rejected alignment requires reasons and no chain")

    def document(self) -> dict[str, object]:
        return asdict(self)


def _validate_l0(point: StructuralPoint) -> None:
    independent_timeframe_override().validate_point(
        level="L0", point=point, observed_at=point.available_at
    )
    if (
        point.point_type != "3buy"
        or point.center_ordinal != 1
        or point.center_id is None
        or point.center_zd is None
        or point.center_zg is None
    ):
        raise ValueError("alignment requires an L0 first-center third buy")


def _validate_l2(point: StructuralPoint) -> None:
    independent_timeframe_override().validate_point(
        level="L2", point=point, observed_at=point.available_at
    )


def align_independent_entry_chains(
    *,
    l0_points: Iterable[StructuralPoint],
    l1_trends: Iterable[CompletedL1TrendFact],
    l2_points: Iterable[StructuralPoint],
    allowed_l2_second_buy_ids: Iterable[str] = (),
) -> tuple[AlignmentDecision, ...]:
    """Pair only causal, same-window independent-chart entry evidence.

    Selection is intentionally first-match and fail-closed.  A later trend or
    an old locator cannot repair the first completed return of a setup.
    """

    contract = IndependentAlignmentContract()
    if contract.contract_id != independent_timeframe_override().entry_alignment_contract_id:
        raise RuntimeError("override and alignment contract disagree")
    l0_values = tuple(l0_points)
    trend_values = tuple(l1_trends)
    l2_values = tuple(l2_points)
    second_buy_ids = frozenset(allowed_l2_second_buy_ids)
    for point in l0_values:
        _validate_l0(point)
    for point in l2_values:
        _validate_l2(point)
    if any(point_id not in {point.point_id for point in l2_values} for point_id in second_buy_ids):
        raise ValueError("allowed L2 second-buy evidence references an unknown point")

    decisions: list[AlignmentDecision] = []
    for l0 in sorted(l0_values, key=lambda item: (item.available_at, item.point_id)):
        start = l0.anchor_at
        end = l0.available_at
        zd = Decimal(str(l0.center_zd))
        zg = Decimal(str(l0.center_zg))
        matching_trends = tuple(
            sorted(
                (
                    trend
                    for trend in trend_values
                    if trend.price_basis_revision == l0.price_basis_revision
                    and start <= trend.market_start
                    and trend.market_end <= end
                    and start <= trend.available_at <= end
                ),
                key=lambda item: (
                    item.market_start,
                    item.market_end,
                    item.available_at,
                    item.trend_id,
                ),
            )
        )
        departure = next(
            (
                trend
                for trend in matching_trends
                if trend.direction == "up"
                and zd <= trend.start_price <= zg
                and trend.end_price > zg
            ),
            None,
        )
        if departure is None:
            decisions.append(
                AlignmentDecision(
                    l0_point_id=l0.point_id,
                    window_start=start,
                    window_end=end,
                    status="REJECT",
                    reason_codes=(
                        "NO_COMPLETED_L1_UP_DEPARTURE_IN_L0_CONFIRMATION_WINDOW",
                    ),
                )
            )
            continue

        first_return = next(
            (
                trend
                for trend in matching_trends
                if trend.direction == "down"
                and trend.market_start >= departure.market_end
                and trend.available_at >= departure.available_at
            ),
            None,
        )
        if first_return is None:
            decisions.append(
                AlignmentDecision(
                    l0_point_id=l0.point_id,
                    window_start=start,
                    window_end=end,
                    status="REJECT",
                    reason_codes=(
                        "NO_FIRST_COMPLETED_L1_DOWN_RETURN_IN_L0_CONFIRMATION_WINDOW",
                    ),
                )
            )
            continue
        if first_return.low_price < zg:
            decisions.append(
                AlignmentDecision(
                    l0_point_id=l0.point_id,
                    window_start=start,
                    window_end=end,
                    status="REJECT",
                    reason_codes=("FIRST_L1_RETURN_LOW_BELOW_L0_ZG",),
                )
            )
            continue

        locator = next(
            (
                point
                for point in sorted(
                    l2_values, key=lambda item: (item.available_at, item.point_id)
                )
                if point.price_basis_revision == l0.price_basis_revision
                and point.point_type in {"1buy", "2buy"}
                and (point.point_type == "1buy" or point.point_id in second_buy_ids)
                and first_return.terminal_start
                <= point.anchor_at
                <= first_return.terminal_end
                and start <= point.available_at <= end
            ),
            None,
        )
        if locator is None:
            decisions.append(
                AlignmentDecision(
                    l0_point_id=l0.point_id,
                    window_start=start,
                    window_end=end,
                    status="REJECT",
                    reason_codes=("NO_L2_LOCATOR_AT_FIRST_L1_RETURN_TERMINAL",),
                )
            )
            continue

        chain = AlignedEntryChain(
            l0_point_id=l0.point_id,
            l1_departure_trend_id=departure.trend_id,
            l1_return_trend_id=first_return.trend_id,
            l2_locator_point_id=locator.point_id,
            decision_at=max(
                l0.available_at,
                departure.available_at,
                first_return.available_at,
                locator.available_at,
            ),
            return_low=first_return.low_price,
            l0_zg=zg,
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


def independent_alignment_contract() -> IndependentAlignmentContract:
    return IndependentAlignmentContract()


__all__ = [
    "AlignedEntryChain",
    "AlignmentDecision",
    "CompletedL1TrendFact",
    "IndependentAlignmentContract",
    "align_independent_entry_chains",
    "completed_l1_trend_fact",
    "independent_alignment_contract",
]
