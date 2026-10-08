"""U297 trade-backed gap fill and bounded strength observation.

L077:37-112 distinguishes a bar-visible gap from the whole interval trading
again, then conditions strong/flat/weak labels on what happens afterward.
Only an explicit tick-price transaction window can advance the fill proof;
OHLC ranges alone remain a nontrading pending observation.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from chanlun.core.strict_structure.general_decomposition import GapBarEvidence
from chanlun.core.strict_structure.identity import stable_structure_id


def _aware(value: datetime) -> bool:
    return isinstance(value, datetime) and value.tzinfo is not None and value.utcoffset() is not None


@dataclass(frozen=True, slots=True)
class GapTradePrint:
    price_tick: int
    market_at: datetime
    available_at: datetime

    def __post_init__(self):
        if (
            type(self.price_tick) is not int
            or not _aware(self.market_at)
            or not _aware(self.available_at)
            or self.available_at < self.market_at
        ):
            raise ValueError("gap trade print needs a tick and causal clocks")


@dataclass(frozen=True, slots=True)
class GapTradeWindowEvidence:
    source_ref: str
    source_sha256: str
    price_basis_revision: str
    complete_from: datetime
    complete_through: datetime
    available_at: datetime
    prints: tuple[GapTradePrint, ...]
    complete_source_window_certified: bool

    def __post_init__(self):
        object.__setattr__(self, "prints", tuple(self.prints))
        if (
            not self.source_ref
            or len(self.source_sha256) != 64
            or not all(char in "0123456789abcdef" for char in self.source_sha256)
            or not self.price_basis_revision
            or not all(_aware(value) for value in (
                self.complete_from, self.complete_through, self.available_at,
            ))
            or not self.complete_from <= self.complete_through <= self.available_at
            or type(self.complete_source_window_certified) is not bool
            or any(not isinstance(item, GapTradePrint)
                   or not self.complete_from <= item.market_at <= self.complete_through
                   or item.available_at > self.available_at
                   for item in self.prints)
            or tuple(sorted(self.prints, key=lambda item: (
                item.market_at, item.available_at, item.price_tick,
            ))) != self.prints
        ):
            raise ValueError("gap trade window needs ordered, basis-pinned source evidence")


@dataclass(frozen=True, slots=True)
class GapPostFillEvidence:
    new_extreme_confirmed: bool | None
    reversal_confirmed: bool | None
    original_trend_grade: int | None
    adjustment_grade: int | None
    available_at: datetime
    source_ref: str

    def __post_init__(self):
        if (
            any(value is not None and type(value) is not bool
                for value in (self.new_extreme_confirmed, self.reversal_confirmed))
            or any(value is not None and (type(value) is not int or value < 0)
                   for value in (self.original_trend_grade, self.adjustment_grade))
            or not _aware(self.available_at)
            or not self.source_ref
        ):
            raise ValueError("gap aftermath needs explicit structural evidence and clock")


def observe_u297_gap_strength(
    before: GapBarEvidence,
    after: GapBarEvidence,
    *,
    physical_frequency: str,
    as_of: datetime,
    transaction_window: GapTradeWindowEvidence | None = None,
    aftermath: GapPostFillEvidence | None = None,
) -> dict:
    """Classify only the submitted source interval, never predict a future fill."""

    if (
        not isinstance(before, GapBarEvidence)
        or not isinstance(after, GapBarEvidence)
        or not physical_frequency
        or not _aware(as_of)
        or before.price_basis_revision != after.price_basis_revision
        or not _aware(before.closed_at) or not _aware(after.closed_at)
        or not before.closed_at < after.closed_at <= as_of
        or type(before.low_tick) is not int
        or type(before.high_tick) is not int
        or type(after.low_tick) is not int
        or type(after.high_tick) is not int
        or before.low_tick > before.high_tick
        or after.low_tick > after.high_tick
        or not (before.high_tick < after.low_tick
                or after.high_tick < before.low_tick)
    ):
        raise ValueError("U297 requires two ordered, nonoverlapping closed bars")
    direction = "up" if before.high_tick < after.low_tick else "down"
    lower = before.high_tick if direction == "up" else after.high_tick
    upper = after.low_tick if direction == "up" else before.low_tick
    gap_id = stable_structure_id(
        "u297-gap-physical-bars-v1", physical_frequency,
        before.price_basis_revision, before.bar_id, after.bar_id,
        lower, upper,
    )
    base = {
        "rule_id": "U297",
        "rule_source_refs": ["L077:37-61", "L077:64-112"],
        "gap_id": gap_id,
        "direction": direction,
        "physical_frequency": physical_frequency,
        "structural_trend_grade": None,
        "price_basis_revision": before.price_basis_revision,
        "source_bar_ids": [before.bar_id, after.bar_id],
        "gap_lower_tick": lower,
        "gap_upper_tick": upper,
        "required_closed_tick_count": upper - lower + 1,
        "boundary_tick_policy": "closed_tick_interval_sufficient_proof",
        "gap_observed_at": int(after.closed_at.timestamp()),
        "as_of": int(as_of.timestamp()),
        "trade_window_source_ref": None,
        "full_transaction_fill_certified": False,
        "gap_strength_classification_certified": False,
        "author_higher_grade_adjustment_certified": False,
        "formal_point_from_observation": False,
        "tradable": False,
    }
    if transaction_window is None:
        return {**base, "status": "pending_transaction_coverage",
                "known_at": int(after.closed_at.timestamp()),
                "filled_at": None, "missing_conditions": ["trade_price_coverage"]}
    if (
        transaction_window.price_basis_revision != before.price_basis_revision
        or transaction_window.complete_from > before.closed_at
        or transaction_window.complete_through < after.closed_at
        or transaction_window.available_at > as_of
    ):
        raise ValueError("U297 trade window does not cover the gap source clock and basis")
    seen = set()
    filled_at = None
    fill_known_at = None
    for print_ in transaction_window.prints:
        if print_.market_at < after.closed_at or print_.available_at > as_of:
            continue
        if lower <= print_.price_tick <= upper:
            seen.add(print_.price_tick)
            if len(seen) == upper - lower + 1:
                filled_at = print_.market_at
                fill_known_at = print_.available_at
                break
    trade_base = {
        **base,
        "trade_window_source_ref": transaction_window.source_ref,
        "trade_window_sha256": transaction_window.source_sha256,
        "observed_gap_ticks": len(seen),
        "trade_window_complete_through": int(transaction_window.complete_through.timestamp()),
        "known_at": int(transaction_window.available_at.timestamp()),
        "filled_at": None if filled_at is None else int(filled_at.timestamp()),
    }
    if filled_at is None:
        if (transaction_window.complete_source_window_certified
                and transaction_window.complete_through >= as_of):
            return {**trade_base, "status": "unfilled_as_of_complete_trade_window",
                    "gap_strength_state": "strong_as_of_only",
                    "missing_conditions": ["future_outcome_open"]}
        return {**trade_base, "status": "pending_complete_trade_window",
                "missing_conditions": ["complete_trade_window_through_as_of"]}
    if not transaction_window.complete_source_window_certified:
        return {
            **trade_base,
            "status": "conditional_tick_coverage_unverified_source_window",
            "missing_conditions": ["complete_trade_window_source_certificate"],
        }
    trade_base["full_transaction_fill_certified"] = True
    trade_base["fill_known_at"] = int(max(
        fill_known_at, transaction_window.available_at,
    ).timestamp())
    if aftermath is None:
        return {**trade_base, "status": "filled_strength_pending",
                "missing_conditions": ["post_fill_price_and_trend_structure"]}
    if aftermath.available_at > as_of or aftermath.available_at < filled_at:
        raise ValueError("U297 aftermath is future or predates full fill")
    trade_base["structural_trend_grade"] = aftermath.original_trend_grade
    trade_base["aftermath_source_ref"] = aftermath.source_ref
    trade_base["aftermath_known_at"] = int(aftermath.available_at.timestamp())
    if aftermath.new_extreme_confirmed is True and aftermath.reversal_confirmed is not True:
        return {**trade_base, "status": "flat_after_fill_new_extreme",
                "gap_strength_classification_certified": True,
                "missing_conditions": []}
    if aftermath.new_extreme_confirmed is False and aftermath.reversal_confirmed is True:
        higher = (
            aftermath.original_trend_grade is not None
            and aftermath.adjustment_grade is not None
            and aftermath.adjustment_grade > aftermath.original_trend_grade
        )
        return {**trade_base,
                "status": "weak_after_fill_reversal" if higher
                          else "weak_price_path_grade_pending",
                "gap_strength_classification_certified": higher,
                "author_higher_grade_adjustment_certified": higher,
                "adjustment_grade": aftermath.adjustment_grade,
                "missing_conditions": [] if higher else ["higher_adjustment_grade"]}
    return {**trade_base, "status": "filled_strength_pending",
            "missing_conditions": ["ordered_post_fill_extreme_or_reversal"]}


__all__ = (
    "GapTradePrint", "GapTradeWindowEvidence", "GapPostFillEvidence",
    "observe_u297_gap_strength",
)
