"""Frozen parameters for the user-authorized recent-year research replay.

This is deliberately separate from the immutable strict strategy live specification.  It
uses the current QMT GICS3 membership capture as a historical research proxy,
does not require the individual three-program service, and matches orders only
against later completed one-minute bars.  The variant can never enable live
trading or claim survivor-bias-free full-system evidence.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import date
from decimal import Decimal

from chanlun.decision_support.fingerprints import sha256_json


RECENT_YEAR_RESEARCH_SCHEMA = "chanlun-recent-year-current-sector-research"
RECENT_YEAR_SELECTION_PATH = "QMT_CURRENT_SECTOR_TECHNICAL_ONLY"


@dataclass(frozen=True, slots=True)
class RecentYearResearchParameters:
    """One immutable, explicitly biased research-only parameter snapshot."""

    # The evaluated interval remains the latest requested year.  Warmup is
    # deliberately outside that interval and supplies at least the frozen 480
    # completed daily bars required by the independent M/W/D convergence gate.
    warmup_start: date = date(2023, 5, 1)
    requested_start: date = date(2025, 7, 25)
    effective_start: date = date(2025, 8, 1)
    requested_end: date = date(2026, 7, 24)
    selection_path: str = RECENT_YEAR_SELECTION_PATH
    sector_taxonomy: str = "QMT_GICS3"
    sector_membership_mode: str = "CURRENT_CAPTURE_BACKFILLED_USER_AUTHORIZED"
    three_program_mode: str = "DISABLED_USER_AUTHORIZED"
    strategic_frequency: str = "30m"
    tactical_frequency: str = "5m"
    locator_frequency: str = "1m"
    recursive_base_frequency: str = "1m"
    strategic_recursive_level: int = 2
    tactical_recursive_level: int = 1
    locator_recursive_level: int = 0
    execution_observation: str = "COMPLETED_1M_BAR"
    tick_data_used: bool = False
    signal_bar_fill_allowed: bool = False
    strict_limit_crossing: bool = True
    settlement_t_plus_days: int = 1
    slot_count: int = 5
    slot_fraction: Decimal = Decimal("0.18")
    account_exposure_cap: Decimal = Decimal("0.90")
    tactical_ratio: Decimal = Decimal("0.25")
    data_grade: str = "RESEARCH_ONLY"
    live_status: str = "LIVE_DISABLED"
    schema: str = RECENT_YEAR_RESEARCH_SCHEMA

    def __post_init__(self) -> None:
        if not (
            self.warmup_start
            <= self.requested_start
            <= self.effective_start
            <= self.requested_end
        ):
            raise ValueError("recent-year research dates are inconsistent")
        if self.selection_path != RECENT_YEAR_SELECTION_PATH:
            raise ValueError("recent-year research selection path changed")
        if (
            self.sector_taxonomy,
            self.sector_membership_mode,
            self.three_program_mode,
        ) != (
            "QMT_GICS3",
            "CURRENT_CAPTURE_BACKFILLED_USER_AUTHORIZED",
            "DISABLED_USER_AUTHORIZED",
        ):
            raise ValueError("recent-year research universe contract changed")
        if (
            self.recursive_base_frequency,
            self.strategic_frequency,
            self.tactical_frequency,
            self.locator_frequency,
            self.strategic_recursive_level,
            self.tactical_recursive_level,
            self.locator_recursive_level,
        ) != ("1m", "30m", "5m", "1m", 2, 1, 0):
            raise ValueError("recent-year direct-recursive mapping changed")
        if (
            self.execution_observation != "COMPLETED_1M_BAR"
            or self.tick_data_used
            or self.signal_bar_fill_allowed
            or not self.strict_limit_crossing
            or self.settlement_t_plus_days != 1
        ):
            raise ValueError("recent-year causal execution contract changed")
        if not (
            self.slot_count == 5
            and self.slot_fraction == Decimal("0.18")
            and self.account_exposure_cap == Decimal("0.90")
            and self.slot_fraction * self.slot_count == self.account_exposure_cap
            and self.tactical_ratio == Decimal("0.25")
        ):
            raise ValueError("recent-year portfolio parameters changed")
        if self.data_grade != "RESEARCH_ONLY" or self.live_status != "LIVE_DISABLED":
            raise ValueError("recent-year research can never enable live trading")
        if self.schema != RECENT_YEAR_RESEARCH_SCHEMA:
            raise ValueError("recent-year research schema changed")

    @property
    def parameter_set_id(self) -> str:
        return sha256_json(self._stable_values())

    def _stable_values(self) -> dict[str, object]:
        values = asdict(self)
        for name in (
            "warmup_start",
            "requested_start",
            "effective_start",
            "requested_end",
        ):
            values[name] = getattr(self, name).isoformat()
        return values

    def document(self) -> dict[str, object]:
        stable = self._stable_values()
        return {
            **stable,
            "parameter_set_id": self.parameter_set_id,
            "known_biases": (
                "CURRENT_QMT_GICS3_MEMBERSHIP_BACKFILLED_OVER_TEST_YEAR",
                "SURVIVORSHIP_AND_MEMBERSHIP_LOOKAHEAD_BIAS_ACCEPTED_FOR_RESEARCH",
                "INDIVIDUAL_THREE_PROGRAM_NOT_EVALUATED",
                "NO_HISTORICAL_TICK_OR_ORDER_BOOK_DATA",
                "COMPLETED_1M_BAR_EXECUTION_PROXY",
            ),
        }


def recent_year_research_parameters() -> RecentYearResearchParameters:
    return RecentYearResearchParameters()


__all__ = (
    "RECENT_YEAR_RESEARCH_SCHEMA",
    "RECENT_YEAR_SELECTION_PATH",
    "RecentYearResearchParameters",
    "recent_year_research_parameters",
)
