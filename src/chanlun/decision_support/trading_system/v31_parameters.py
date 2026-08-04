from __future__ import annotations

from dataclasses import asdict, dataclass
from decimal import Decimal
import json
from typing import Literal

from chanlun.decision_support.trading_system.v3_parameters import (
    SelectionPath,
    canonical_snapshot_json,
    etf_parameter_snapshot,
    individual_parameter_snapshot,
    snapshot_sha256,
)


STRATEGY_V31_ID = "CL-HIER-30M5M1M-v3.1-paper-candidate"
LIVE_STATUS = "LIVE_DISABLED"


@dataclass(frozen=True, slots=True)
class StrategyV31Parameters:
    """Frozen research defaults for the V3.1 paper candidate.

    These values are engineering controls, not Chan-theory claims.  They may
    only change in a new parameter snapshot and never after inspecting a
    holdout result.
    """

    selection_path: SelectionPath
    strategy_id: str = STRATEGY_V31_ID
    level_relation_mode: str = "INDEPENDENT_CHART_EVIDENCE_V31"
    pen_definition_mode: str = "ORIGINAL_OLD_PEN"
    slot_count: int = 5
    slot_notional_cap: Decimal = Decimal("0.18")
    gross_exposure_cap: Decimal = Decimal("0.90")
    per_position_open_risk_fraction: Decimal = Decimal("0.005")
    portfolio_open_risk_cap: Decimal = Decimal("0.025")
    cluster_open_risk_cap: Decimal = Decimal("0.010")
    cluster_exposure_cap: Decimal = Decimal("0.36")
    max_slots_per_cluster: int = 2
    structural_gap_buffer_fraction: Decimal = Decimal("0.01")
    structural_gap_buffer_ticks_min: int = 2
    caution_drawdown: Decimal = Decimal("0.05")
    entry_halt_drawdown: Decimal = Decimal("0.10")
    deleverage_drawdown: Decimal = Decimal("0.12")
    caution_new_risk_multiplier: Decimal = Decimal("0.50")
    deleverage_target_exposure: Decimal = Decimal("0.45")
    tactical_enabled: bool = False
    program_trading_report_required: bool = True
    require_licensed_market_data: bool = True
    live_status: str = LIVE_STATUS

    def __post_init__(self) -> None:
        if self.selection_path not in {
            "INDIVIDUAL_THREE_PROGRAM",
            "ETF_PROXY",
        }:
            raise ValueError("unsupported V3.1 selection path")
        if self.strategy_id != STRATEGY_V31_ID:
            raise ValueError("V3.1 strategy identity is frozen")
        if self.pen_definition_mode != "ORIGINAL_OLD_PEN":
            raise ValueError("V3.1 only permits ORIGINAL_OLD_PEN")
        if self.live_status != LIVE_STATUS:
            raise ValueError("V3.1 cannot enable live trading")
        if self.slot_count != 5:
            raise ValueError("V3.1 retains five maximum strategic slots")
        fractions = (
            self.slot_notional_cap,
            self.gross_exposure_cap,
            self.per_position_open_risk_fraction,
            self.portfolio_open_risk_cap,
            self.cluster_open_risk_cap,
            self.cluster_exposure_cap,
            self.structural_gap_buffer_fraction,
            self.caution_drawdown,
            self.entry_halt_drawdown,
            self.deleverage_drawdown,
            self.caution_new_risk_multiplier,
            self.deleverage_target_exposure,
        )
        if any(value <= 0 or value > 1 for value in fractions):
            raise ValueError("V3.1 fractions must be in (0, 1]")
        if not (
            self.caution_drawdown
            < self.entry_halt_drawdown
            < self.deleverage_drawdown
        ):
            raise ValueError("V3.1 drawdown thresholds must increase strictly")
        if self.slot_notional_cap * self.slot_count != self.gross_exposure_cap:
            raise ValueError("V3.1 slot and gross caps disagree")
        if (
            self.per_position_open_risk_fraction * self.slot_count
            != self.portfolio_open_risk_cap
        ):
            raise ValueError("V3.1 position and portfolio risk caps disagree")
        if self.cluster_open_risk_cap < self.per_position_open_risk_fraction:
            raise ValueError("cluster risk cap cannot be below one position budget")
        if self.max_slots_per_cluster <= 0 or self.max_slots_per_cluster > self.slot_count:
            raise ValueError("invalid V3.1 cluster slot cap")
        if self.structural_gap_buffer_ticks_min <= 0:
            raise ValueError("V3.1 gap buffer ticks must be positive")
        if self.tactical_enabled:
            raise ValueError("V3.1 paper candidate starts with tactical trading disabled")

    @property
    def parent_v3_parameter_set_id(self) -> str:
        parent = (
            individual_parameter_snapshot()
            if self.selection_path == "INDIVIDUAL_THREE_PROGRAM"
            else etf_parameter_snapshot()
        )
        return parent.parameter_set_id

    def document(self) -> dict[str, object]:
        payload = asdict(self)
        payload["parent_v3_parameter_set_id"] = self.parent_v3_parameter_set_id
        return json.loads(canonical_snapshot_json(payload))

    @property
    def parameter_set_id(self) -> str:
        return snapshot_sha256(self.document())


def v31_parameter_snapshot(
    selection_path: Literal["INDIVIDUAL_THREE_PROGRAM", "ETF_PROXY"],
) -> StrategyV31Parameters:
    return StrategyV31Parameters(selection_path=selection_path)


def v31_parameter_manifest() -> dict[str, object]:
    individual = v31_parameter_snapshot("INDIVIDUAL_THREE_PROGRAM")
    etf = v31_parameter_snapshot("ETF_PROXY")
    if individual.parameter_set_id == etf.parameter_set_id:
        raise RuntimeError("V3.1 selection paths must have distinct snapshots")
    manifest: dict[str, object] = {
        "schema": "chanlun-v31-parameter-snapshots/v1",
        "strategy_id": STRATEGY_V31_ID,
        "live_status": LIVE_STATUS,
        "scope": "RESEARCH_ONLY/PAPER_CANDIDATE",
        "snapshots": {
            value.selection_path: {
                "parameter_set_id": value.parameter_set_id,
                "parent_v3_parameter_set_id": value.parent_v3_parameter_set_id,
                "parameters": value.document(),
            }
            for value in (individual, etf)
        },
    }
    manifest["manifest_sha256"] = snapshot_sha256(manifest)
    return manifest


__all__ = [
    "LIVE_STATUS",
    "STRATEGY_V31_ID",
    "StrategyV31Parameters",
    "v31_parameter_manifest",
    "v31_parameter_snapshot",
]
