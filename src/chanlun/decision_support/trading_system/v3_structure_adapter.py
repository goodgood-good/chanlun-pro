from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import TYPE_CHECKING

from chanlun.decision_support.fingerprints import normalize_datetime
from chanlun.decision_support.trading_system.models import StructuralPoint
from chanlun.decision_support.trading_system.v3_selection import TechnicalEntrySnapshot
from chanlun.decision_support.trading_system.v3_timeframe_alignment import (
    AlignedEntryChain,
    CompletedL1TrendFact,
    independent_alignment_contract,
)
from chanlun.decision_support.trading_system.v3_timeframe_override import (
    independent_timeframe_override,
)

if TYPE_CHECKING:
    from chanlun.decision_support.trading_system.v3_direct_recursive_structure import (
        DirectRecursiveEntryChain,
    )


def build_v3_technical_entry_snapshot(
    *,
    structure_snapshot_id: str,
    observed_at: datetime,
    l0_three_buy: StructuralPoint,
    l2_locator: StructuralPoint,
    l1_departure_completed: bool,
    l1_first_return_completed: bool,
    first_return_low: Decimal,
    direct_recursive_levels_unique: bool,
    all_components_completed: bool,
) -> TechnicalEntrySnapshot:
    """Translate frozen structure outputs into the v3 entry contract.

    This adapter does not calculate or repair a point, center, return, level,
    or boundary.  Every structural fact is supplied by the frozen snapshot and
    merely validated before being copied into the decision model.
    """

    observed = normalize_datetime(observed_at, "observed_at")
    if not l0_three_buy.confirmed or l0_three_buy.source_frequency != "30m":
        raise ValueError("v3 L0 input must be a confirmed frozen 30m point")
    if l0_three_buy.point_type != "3buy" or l0_three_buy.side != "buy":
        raise ValueError("v3 L0 entry input must be a frozen third buy")
    if l0_three_buy.center_id is None or l0_three_buy.center_zg is None:
        raise ValueError("v3 L0 third buy requires its frozen center boundary")
    if not l2_locator.confirmed or l2_locator.source_frequency != "1m":
        raise ValueError("v3 locator must be a confirmed frozen 1m point")
    if l2_locator.point_type not in {"1buy", "2buy"} or l2_locator.side != "buy":
        raise ValueError("v3 locator must be a frozen first or second buy")
    if l0_three_buy.price_basis_revision != l2_locator.price_basis_revision:
        raise ValueError("v3 structure adapter cannot cross price bases")
    if l0_three_buy.available_at > observed or l2_locator.available_at > observed:
        raise ValueError("v3 structure adapter rejects future points")
    confirmation_time = max(l0_three_buy.available_at, l2_locator.available_at)
    return TechnicalEntrySnapshot(
        structure_snapshot_id=structure_snapshot_id,
        observed_at=observed,
        price_basis_revision=l0_three_buy.price_basis_revision,
        pen_definition_mode="ORIGINAL_OLD_PEN",
        l0_source_frequency="30m",
        l1_source_frequency="5m",
        l2_source_frequency="1m",
        direct_recursive_levels_unique=direct_recursive_levels_unique,
        all_components_completed=all_components_completed,
        l0_center_id=l0_three_buy.center_id,
        l0_center_ordinal=l0_three_buy.center_ordinal,
        l0_center_completed=True,
        l0_point_type=l0_three_buy.point_type,
        l0_point_id=l0_three_buy.point_id,
        l0_point_confirmation_time=confirmation_time,
        l1_departure_completed=l1_departure_completed,
        l1_first_return_completed=l1_first_return_completed,
        first_return_low=first_return_low,
        l0_zg=Decimal(str(l0_three_buy.center_zg)),
        l2_locator=(
            "L2_FIRST_BUY"
            if l2_locator.point_type == "1buy"
            else "L2_SECOND_BUY_AFTER_SMALL_TO_LARGE_REVERSAL"
        ),
        l2_point_id=l2_locator.point_id,
        l2_confirmation_bar_high=Decimal(str(l2_locator.structure_anchor_price)),
    )


def build_v3_independent_technical_entry_snapshot(
    *,
    structure_snapshot_id: str,
    observed_at: datetime,
    chain: AlignedEntryChain,
    l0_three_buy: StructuralPoint,
    l1_departure: CompletedL1TrendFact,
    l1_first_return: CompletedL1TrendFact,
    l2_locator: StructuralPoint,
) -> TechnicalEntrySnapshot:
    """Copy one certified independent-chart chain into the shared V3 core."""

    observed = normalize_datetime(observed_at, "observed_at")
    override = independent_timeframe_override()
    contract = independent_alignment_contract()
    override.validate_point(level="L0", point=l0_three_buy, observed_at=observed)
    override.validate_point(level="L2", point=l2_locator, observed_at=observed)
    expected_ids = (
        chain.l0_point_id,
        chain.l1_departure_trend_id,
        chain.l1_return_trend_id,
        chain.l2_locator_point_id,
    )
    actual_ids = (
        l0_three_buy.point_id,
        l1_departure.trend_id,
        l1_first_return.trend_id,
        l2_locator.point_id,
    )
    if expected_ids != actual_ids:
        raise ValueError("independent entry chain evidence identity mismatch")
    if chain.decision_at > observed:
        raise ValueError("independent entry chain is not yet observable")
    revisions = {
        l0_three_buy.price_basis_revision,
        l1_departure.price_basis_revision,
        l1_first_return.price_basis_revision,
        l2_locator.price_basis_revision,
    }
    if len(revisions) != 1:
        raise ValueError("independent entry chain cannot cross price bases")
    if l1_departure.direction != "up" or l1_first_return.direction != "down":
        raise ValueError("independent entry chain directions are invalid")
    if chain.return_low != l1_first_return.low_price:
        raise ValueError("independent entry chain return boundary changed")
    if l0_three_buy.center_zg is None or chain.l0_zg != Decimal(
        str(l0_three_buy.center_zg)
    ):
        raise ValueError("independent entry chain L0 boundary changed")
    if l0_three_buy.center_id is None or l0_three_buy.center_ordinal != 1:
        raise ValueError("independent entry requires a frozen first L0 center")
    if l2_locator.point_type not in {"1buy", "2buy"}:
        raise ValueError("independent entry locator must be first or allowed second buy")
    return TechnicalEntrySnapshot(
        structure_snapshot_id=structure_snapshot_id,
        observed_at=observed,
        price_basis_revision=l0_three_buy.price_basis_revision,
        pen_definition_mode="ORIGINAL_OLD_PEN",
        l0_source_frequency=override.l0_source_frequency,
        l1_source_frequency=override.l1_source_frequency,
        l2_source_frequency=override.l2_source_frequency,
        direct_recursive_levels_unique=False,
        all_components_completed=True,
        l0_center_id=l0_three_buy.center_id,
        l0_center_ordinal=l0_three_buy.center_ordinal,
        l0_center_completed=True,
        l0_point_type=l0_three_buy.point_type,
        l0_point_id=l0_three_buy.point_id,
        l0_point_confirmation_time=chain.decision_at,
        l1_departure_completed=True,
        l1_first_return_completed=True,
        first_return_low=chain.return_low,
        l0_zg=chain.l0_zg,
        l2_locator=(
            "L2_FIRST_BUY"
            if l2_locator.point_type == "1buy"
            else "L2_SECOND_BUY_AFTER_SMALL_TO_LARGE_REVERSAL"
        ),
        l2_point_id=l2_locator.point_id,
        l2_confirmation_bar_high=Decimal(str(l2_locator.structure_anchor_price)),
        level_relation_mode="USER_OVERRIDE_INDEPENDENT_TIMEFRAMES",
        level_relation_contract_id=contract.contract_id,
    )


def build_v3_direct_recursive_technical_entry_snapshot(
    *,
    structure_snapshot_id: str,
    observed_at: datetime,
    chain: "DirectRecursiveEntryChain",
    l0_three_buy: StructuralPoint,
    l2_locator: StructuralPoint,
) -> TechnicalEntrySnapshot:
    """Copy one 1m-base direct-recursive 30m/5m/1m chain into V3.

    Raw point provenance is deliberately not relabelled: both points retain
    their physical ``source_frequency='1m'`` and are distinguished by
    recursive levels 2 and 0.  Only this decision snapshot exposes the logical
    operating labels 30m/5m/1m required by the strategy specification.
    """

    observed = normalize_datetime(observed_at, "observed_at")
    if chain.decision_at > observed:
        raise ValueError("direct recursive chain is not yet observable")
    if (
        not l0_three_buy.confirmed
        or l0_three_buy.source_frequency != "1m"
        or l0_three_buy.recursive_level != 2
        or l0_three_buy.point_type != "3buy"
        or l0_three_buy.center_ordinal != 1
        or l0_three_buy.center_id is None
    ):
        raise ValueError("direct recursion requires a level-2 first-center 3buy")
    if (
        not l2_locator.confirmed
        or l2_locator.source_frequency != "1m"
        or l2_locator.recursive_level != 0
        or l2_locator.point_type not in {"1buy", "2buy"}
    ):
        raise ValueError("direct recursion requires a level-0 first/second buy")
    if (
        chain.l0_point_id != l0_three_buy.point_id
        or chain.l0_center_id != l0_three_buy.center_id
        or chain.l2_locator_point_id != l2_locator.point_id
    ):
        raise ValueError("direct recursive chain evidence identity mismatch")
    if l0_three_buy.price_basis_revision != l2_locator.price_basis_revision:
        raise ValueError("direct recursive chain cannot cross price bases")
    if l0_three_buy.center_zg is None or Decimal(
        str(l0_three_buy.center_zg)
    ) != chain.l0_zg:
        raise ValueError("direct recursive L0 boundary changed")
    if chain.first_return_low < chain.l0_zg:
        raise ValueError("direct recursive first return entered the L0 center")
    if chain.l2_confirmation_bar_high != Decimal(
        str(l2_locator.structure_anchor_price)
    ):
        raise ValueError("direct recursive locator execution boundary changed")
    return TechnicalEntrySnapshot(
        structure_snapshot_id=structure_snapshot_id,
        observed_at=observed,
        price_basis_revision=l0_three_buy.price_basis_revision,
        pen_definition_mode="ORIGINAL_OLD_PEN",
        l0_source_frequency="30m",
        l1_source_frequency="5m",
        l2_source_frequency="1m",
        direct_recursive_levels_unique=True,
        all_components_completed=True,
        l0_center_id=chain.l0_center_id,
        l0_center_ordinal=1,
        l0_center_completed=True,
        l0_point_type="3buy",
        l0_point_id=chain.l0_point_id,
        l0_point_confirmation_time=chain.decision_at,
        l1_departure_completed=True,
        l1_first_return_completed=True,
        first_return_low=chain.first_return_low,
        l0_zg=chain.l0_zg,
        l2_locator=(
            "L2_FIRST_BUY"
            if l2_locator.point_type == "1buy"
            else "L2_SECOND_BUY_AFTER_SMALL_TO_LARGE_REVERSAL"
        ),
        l2_point_id=chain.l2_locator_point_id,
        l2_confirmation_bar_high=chain.l2_confirmation_bar_high,
        level_relation_mode="DIRECT_RECURSIVE",
    )


__all__ = [
    "build_v3_direct_recursive_technical_entry_snapshot",
    "build_v3_independent_technical_entry_snapshot",
    "build_v3_technical_entry_snapshot",
]
