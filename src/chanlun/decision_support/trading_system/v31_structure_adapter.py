from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from chanlun.decision_support.fingerprints import normalize_datetime
from chanlun.decision_support.trading_system.models import StructuralPoint
from chanlun.decision_support.trading_system.v3_selection import TechnicalEntrySnapshot
from chanlun.decision_support.trading_system.v31_timeframe_alignment import (
    V31AlignedEntryChain,
    v31_alignment_contract,
)


def build_v31_technical_entry_snapshot(
    *,
    structure_snapshot_id: str,
    observed_at: datetime,
    chain: V31AlignedEntryChain,
    l0_three_buy: StructuralPoint,
    l2_locator: StructuralPoint,
) -> TechnicalEntrySnapshot:
    """Copy a certified V3.1 chain into the unchanged V3 candidate model."""

    observed = normalize_datetime(observed_at, "observed_at")
    if chain.decision_at > observed:
        raise ValueError("V3.1 chain is not yet observable")
    if (
        not l0_three_buy.confirmed
        or l0_three_buy.source_frequency != "30m"
        or l0_three_buy.recursive_level != 0
        or l0_three_buy.point_type != "3buy"
        or l0_three_buy.center_ordinal != 1
    ):
        raise ValueError("V3.1 requires a confirmed independent 30m first-center 3buy")
    if (
        not l2_locator.confirmed
        or l2_locator.source_frequency != "1m"
        or l2_locator.recursive_level != 0
        or l2_locator.point_type not in {"1buy", "2buy"}
    ):
        raise ValueError("V3.1 requires a confirmed independent 1m locator")
    if (
        chain.l0_point_id != l0_three_buy.point_id
        or chain.l2_locator_point_id != l2_locator.point_id
        or chain.l0_center_id != l0_three_buy.center_id
    ):
        raise ValueError("V3.1 chain identities do not match frozen points")
    if l0_three_buy.price_basis_revision != l2_locator.price_basis_revision:
        raise ValueError("V3.1 cannot cross price bases")
    if l0_three_buy.center_zg is None or Decimal(
        str(l0_three_buy.center_zg)
    ) != chain.l0_zg:
        raise ValueError("V3.1 L0 ZG changed after alignment")
    contract = v31_alignment_contract()
    return TechnicalEntrySnapshot(
        structure_snapshot_id=structure_snapshot_id,
        observed_at=observed,
        price_basis_revision=l0_three_buy.price_basis_revision,
        pen_definition_mode="ORIGINAL_OLD_PEN",
        l0_source_frequency="30m",
        l1_source_frequency="5m",
        l2_source_frequency="1m",
        direct_recursive_levels_unique=False,
        all_components_completed=True,
        l0_center_id=chain.l0_center_id,
        l0_center_ordinal=1,
        l0_center_completed=True,
        l0_point_type="3buy",
        l0_point_id=chain.l0_point_id,
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
        l2_point_id=chain.l2_locator_point_id,
        l2_confirmation_bar_high=chain.l2_confirmation_bar_high,
        level_relation_mode="USER_OVERRIDE_INDEPENDENT_TIMEFRAMES",
        level_relation_contract_id=contract.contract_id,
    )


__all__ = ["build_v31_technical_entry_snapshot"]
