from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import TYPE_CHECKING

from chanlun.decision_support.fingerprints import normalize_datetime
from chanlun.core.strict_structure.base_profile import STRICT_STROKE_MODE
from chanlun.decision_support.trading_system.models import StructuralPoint
from chanlun.decision_support.trading_system.selection import TechnicalEntrySnapshot

if TYPE_CHECKING:
    from chanlun.decision_support.trading_system.direct_recursive_structure import (
        DirectRecursiveEntryChain,
    )


def build_direct_recursive_technical_entry_snapshot(
    *,
    structure_snapshot_id: str,
    observed_at: datetime,
    chain: "DirectRecursiveEntryChain",
    l0_three_buy: StructuralPoint,
    l2_locator: StructuralPoint,
) -> TechnicalEntrySnapshot:
    """Copy one 1m-base direct-recursive 30m/5m/1m chain.

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
        stroke_mode=STRICT_STROKE_MODE,
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
            else "L2_SECOND_BUY"
        ),
        l2_point_id=chain.l2_locator_point_id,
        l2_confirmation_bar_high=chain.l2_confirmation_bar_high,
        level_relation_mode="DIRECT_RECURSIVE",
    )


__all__ = [
    "build_direct_recursive_technical_entry_snapshot",
]
