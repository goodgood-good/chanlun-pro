from __future__ import annotations

from decimal import Decimal
from typing import Iterable, Mapping

import pandas as pd

from chanlun.core.strict_structure.errors import StrictStructureContractError
from chanlun.decision_support.fingerprints import normalize_datetime
from chanlun.decision_support.trading_system.backtest.fixed_year import strict_state
from chanlun.decision_support.trading_system.models import StructuralPoint
from chanlun.decision_support.trading_system.v31_timeframe_alignment import (
    CompletedL1UnitFact,
    completed_l1_unit_fact,
)


def completed_l1_unit_snapshots_at_l0_decisions(
    *,
    code: str,
    five_minute_frame: pd.DataFrame,
    l0_points: Iterable[StructuralPoint],
) -> Mapping[str, tuple[CompletedL1UnitFact, ...]]:
    """Capture the exact 5m strict snapshot at each L0 decision prefix.

    This is causal current-state evidence: each snapshot is materialized before
    any later bar is processed and is keyed by the immutable L0 point.  A later
    terminal projection therefore cannot replace the historical decision input.
    """

    if five_minute_frame.empty or "date" not in five_minute_frame.columns:
        raise ValueError("V3.1 requires a non-empty completed 5m frame")
    ordered_points = tuple(
        sorted(l0_points, key=lambda item: (item.available_at, item.point_id))
    )
    if any(point.source_frequency != "30m" for point in ordered_points):
        raise ValueError("V3.1 L1 checkpoints require 30m L0 points")
    dates = pd.to_datetime(five_minute_frame["date"])
    if not dates.is_monotonic_increasing or dates.duplicated().any():
        raise ValueError("V3.1 5m frame must be strictly ordered and unique")
    state = strict_state(code, "5m", five_minute_frame)
    cursor = 0
    quantum = Decimal(str(five_minute_frame.attrs["structure_price_quantum"]))
    output: dict[str, tuple[CompletedL1UnitFact, ...]] = {}
    for point in ordered_points:
        checkpoint = normalize_datetime(point.available_at, "l0_available_at")
        end = int(dates.searchsorted(checkpoint, side="right"))
        if end < cursor:
            raise ValueError("V3.1 L0 checkpoints cannot move backwards")
        if end > cursor:
            state.process_klines(five_minute_frame.iloc[cursor:end])
            cursor = end
        try:
            structure = state.get_strict_structure_levels()
        except StrictStructureContractError:
            output[point.point_id] = ()
            continue
        level = structure.levels[0]
        facts = tuple(
            sorted(
                (
                    completed_l1_unit_fact(unit, price_quantum=quantum)
                    for unit in level.units
                    if unit.locked
                    and unit.confirmed_at is not None
                    and unit.available_at <= checkpoint
                ),
                key=lambda item: (item.market_start, item.market_end, item.unit_id),
            )
        )
        output[point.point_id] = facts
    return output


__all__ = ["completed_l1_unit_snapshots_at_l0_decisions"]
