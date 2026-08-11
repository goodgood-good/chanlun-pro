from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Literal

from chanlun.decision_support.fingerprints import normalize_datetime
from chanlun.decision_support.trading_system.models import StructuralPoint
from chanlun.decision_support.trading_system.parameters import (
    LIVE_STATUS,
    STRATEGY_ID,
    snapshot_sha256,
)


Level = Literal["L0", "L1", "L2"]
USER_OVERRIDE_VARIANT_ID = (
    "CL-HIER-30M5M1M-USER-OVERRIDE-INDEPENDENT-TIMEFRAMES"
)
ENTRY_ALIGNMENT_CONTRACT_ID = (
    "CL-HIER-30M5M1M-INDEPENDENT-CAUSAL-ALIGNMENT"
)


@dataclass(frozen=True, slots=True)
class IndependentTimeframeOverride:
    """Audited user override for independent 30m/5m/1m structure charts.

    The base strategy specification remains immutable.  This contract changes
    only the source of strict strategy level facts and never changes the frozen Chanlun
    algorithms.  Each chart contributes only its own structural level zero so
    recursive products inside one chart cannot be mixed with another chart.
    """

    variant_id: str = USER_OVERRIDE_VARIANT_ID
    base_strategy_id: str = STRATEGY_ID
    l0_source_frequency: str = "30m"
    l1_source_frequency: str = "5m"
    l2_source_frequency: str = "1m"
    accepted_recursive_level: int = 0
    direct_recursive_relation_required: bool = False
    entry_alignment_contract_id: str = ENTRY_ALIGNMENT_CONTRACT_ID
    l0_confirmation_window: str = "ANCHOR_AT_TO_AVAILABLE_AT_INCLUSIVE"
    l1_departure_rule: str = (
        "FIRST_COMPLETE_UP_TREND_STARTING_INSIDE_L0_CENTER_AND_ENDING_ABOVE_ZG"
    )
    l1_return_rule: str = (
        "FIRST_SUBSEQUENT_COMPLETE_DOWN_TREND_WITH_LOW_GREATER_OR_EQUAL_L0_ZG"
    )
    l2_terminal_rule: str = (
        "CONFIRMED_1BUY_OR_EVIDENCED_ALLOWED_2BUY_ANCHORED_IN_RETURN_TERMINAL_UNIT"
    )
    user_authorized_on: str = "2026-07-26"
    highest_status: str = "RESEARCH_ONLY"
    live_status: str = LIVE_STATUS

    def __post_init__(self) -> None:
        if self.variant_id != USER_OVERRIDE_VARIANT_ID:
            raise ValueError("independent-timeframe variant id is frozen")
        if self.base_strategy_id != STRATEGY_ID:
            raise ValueError("override must reference the frozen strict strategy base strategy")
        if (
            self.l0_source_frequency,
            self.l1_source_frequency,
            self.l2_source_frequency,
        ) != ("30m", "5m", "1m"):
            raise ValueError("independent-timeframe mapping is frozen at 30m/5m/1m")
        if self.accepted_recursive_level != 0:
            raise ValueError("only each independent chart's level zero is admissible")
        if self.direct_recursive_relation_required:
            raise ValueError("the user override explicitly waives direct recursion")
        if self.entry_alignment_contract_id != ENTRY_ALIGNMENT_CONTRACT_ID:
            raise ValueError("independent-timeframe alignment contract is frozen")
        if self.l0_confirmation_window != "ANCHOR_AT_TO_AVAILABLE_AT_INCLUSIVE":
            raise ValueError("independent-timeframe L0 window is frozen")
        if self.highest_status != "RESEARCH_ONLY" or self.live_status != LIVE_STATUS:
            raise ValueError("the override cannot enable live trading")

    def document(self) -> dict[str, object]:
        return asdict(self)

    @property
    def parameter_set_id(self) -> str:
        return snapshot_sha256(self.document())

    def frequency_for(self, level: Level) -> str:
        return {
            "L0": self.l0_source_frequency,
            "L1": self.l1_source_frequency,
            "L2": self.l2_source_frequency,
        }[level]

    def validate_point(
        self,
        *,
        level: Level,
        point: StructuralPoint,
        observed_at: datetime,
    ) -> StructuralPoint:
        observed = normalize_datetime(observed_at, "observed_at")
        if not point.confirmed:
            raise ValueError("independent-timeframe facts must be confirmed")
        if point.source_frequency != self.frequency_for(level):
            raise ValueError("point frequency does not match the strict strategy level mapping")
        if point.recursive_level != self.accepted_recursive_level:
            raise ValueError("only independent chart level-zero points are admissible")
        if point.available_at > observed:
            raise ValueError("future structural points are not admissible")
        return point


def independent_timeframe_override() -> IndependentTimeframeOverride:
    return IndependentTimeframeOverride()


__all__ = [
    "ENTRY_ALIGNMENT_CONTRACT_ID",
    "IndependentTimeframeOverride",
    "USER_OVERRIDE_VARIANT_ID",
    "Level",
    "independent_timeframe_override",
]
