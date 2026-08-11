from __future__ import annotations

from dataclasses import asdict, dataclass
from decimal import Decimal
import hashlib
import json
from typing import Literal


STRATEGY_ID = "CL-HIER-30M5M1M"
LIVE_STATUS = "LIVE_DISABLED"
SelectionPath = Literal["INDIVIDUAL_THREE_PROGRAM", "ETF_PROXY"]


def _canonical_value(value: object) -> object:
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, dict):
        return {key: _canonical_value(item) for key, item in sorted(value.items())}
    if isinstance(value, (list, tuple)):
        return [_canonical_value(item) for item in value]
    return value


def canonical_snapshot_json(value: object) -> str:
    return json.dumps(
        _canonical_value(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def snapshot_sha256(value: object) -> str:
    payload = canonical_snapshot_json(value).encode("utf-8")
    return "sha256:" + hashlib.sha256(payload).hexdigest()


@dataclass(frozen=True, slots=True)
class StrategyParameters:
    selection_path: SelectionPath
    strategy_id: str = STRATEGY_ID
    pen_definition_mode: str = "ORIGINAL_OLD_PEN"
    account_exposure_cap: Decimal = Decimal("0.90")
    slot_count: int = 5
    slot_fraction: Decimal = Decimal("0.18")
    tactical_ratio: Decimal = Decimal("0.25")
    max_tactical_cycles_per_symbol_per_day_under_t1: int = 1
    liquidity_lookback_sessions: int = 20
    max_order_fraction_of_median_daily_volume: Decimal = Decimal("0.01")
    max_order_fraction_of_median_same_clock_l2_volume: Decimal = Decimal("0.05")
    median_effective_spread_ticks_max: Decimal = Decimal("2")
    valid_quote_coverage_min: Decimal = Decimal("0.99")
    entry_drawdown_halt: Decimal = Decimal("0.10")
    historical_fill_participation: Decimal = Decimal("0.05")
    tactical_pair_lookback: int = 20
    tactical_pair_minimum: int = 5
    tactical_lower_median_edge_ticks_min: Decimal = Decimal("1.0")
    fractal_ma_coarse: bool = False
    allow_leverage: bool = False
    allow_short: bool = False
    live_status: str = LIVE_STATUS

    def __post_init__(self) -> None:
        if self.selection_path not in {
            "INDIVIDUAL_THREE_PROGRAM",
            "ETF_PROXY",
        }:
            raise ValueError("unsupported strict strategy selection path")
        if self.strategy_id != STRATEGY_ID:
            raise ValueError("strategy_id is frozen")
        if self.pen_definition_mode != "ORIGINAL_OLD_PEN":
            raise ValueError("strict strategy only permits ORIGINAL_OLD_PEN")
        frozen_values = {
            "account_exposure_cap": (self.account_exposure_cap, Decimal("0.90")),
            "slot_count": (self.slot_count, 5),
            "slot_fraction": (self.slot_fraction, Decimal("0.18")),
            "tactical_ratio": (self.tactical_ratio, Decimal("0.25")),
            "max_tactical_cycles_per_symbol_per_day_under_t1": (
                self.max_tactical_cycles_per_symbol_per_day_under_t1,
                1,
            ),
            "liquidity_lookback_sessions": (
                self.liquidity_lookback_sessions,
                20,
            ),
            "max_order_fraction_of_median_daily_volume": (
                self.max_order_fraction_of_median_daily_volume,
                Decimal("0.01"),
            ),
            "max_order_fraction_of_median_same_clock_l2_volume": (
                self.max_order_fraction_of_median_same_clock_l2_volume,
                Decimal("0.05"),
            ),
            "median_effective_spread_ticks_max": (
                self.median_effective_spread_ticks_max,
                Decimal("2"),
            ),
            "valid_quote_coverage_min": (
                self.valid_quote_coverage_min,
                Decimal("0.99"),
            ),
            "entry_drawdown_halt": (self.entry_drawdown_halt, Decimal("0.10")),
            "historical_fill_participation": (
                self.historical_fill_participation,
                Decimal("0.05"),
            ),
            "tactical_pair_lookback": (self.tactical_pair_lookback, 20),
            "tactical_pair_minimum": (self.tactical_pair_minimum, 5),
            "tactical_lower_median_edge_ticks_min": (
                self.tactical_lower_median_edge_ticks_min,
                Decimal("1.0"),
            ),
        }
        changed = tuple(
            name for name, (actual, expected) in frozen_values.items() if actual != expected
        )
        if changed:
            raise ValueError(f"strict strategy frozen parameters changed: {','.join(changed)}")
        if self.slot_fraction * Decimal(self.slot_count) != self.account_exposure_cap:
            raise ValueError("slot fractions must equal the exposure cap")
        if not Decimal("0") < self.tactical_ratio < Decimal("1"):
            raise ValueError("tactical_ratio must be in (0, 1)")
        if not Decimal("0") < self.historical_fill_participation <= Decimal("1"):
            raise ValueError("historical fill participation must be in (0, 1]")
        if self.fractal_ma_coarse:
            raise ValueError("FRACTAL_MA_COARSE is frozen off in strict strategy")
        if self.allow_leverage or self.allow_short:
            raise ValueError("strict strategy is cash-only and long-only")
        if self.live_status != LIVE_STATUS:
            raise ValueError("strict strategy integration cannot enable live trading")

    def document(self) -> dict[str, object]:
        return _canonical_value(asdict(self))  # type: ignore[return-value]

    @property
    def parameter_set_id(self) -> str:
        return snapshot_sha256(self.document())


def individual_parameter_snapshot() -> StrategyParameters:
    return StrategyParameters("INDIVIDUAL_THREE_PROGRAM")


def etf_parameter_snapshot() -> StrategyParameters:
    return StrategyParameters("ETF_PROXY")


def parameter_snapshot_manifest() -> dict[str, object]:
    individual = individual_parameter_snapshot()
    etf = etf_parameter_snapshot()
    if individual.parameter_set_id == etf.parameter_set_id:
        raise RuntimeError("selection paths must have distinct parameter snapshots")
    manifest = {
        "schema": "chanlun-parameter-snapshots",
        "strategy_id": STRATEGY_ID,
        "live_status": LIVE_STATUS,
        "snapshots": {
            individual.selection_path: {
                "parameter_set_id": individual.parameter_set_id,
                "parameters": individual.document(),
            },
            etf.selection_path: {
                "parameter_set_id": etf.parameter_set_id,
                "parameters": etf.document(),
            },
        },
    }
    manifest["manifest_sha256"] = snapshot_sha256(manifest)
    return manifest


__all__ = [
    "LIVE_STATUS",
    "STRATEGY_ID",
    "SelectionPath",
    "StrategyParameters",
    "canonical_snapshot_json",
    "etf_parameter_snapshot",
    "individual_parameter_snapshot",
    "parameter_snapshot_manifest",
    "snapshot_sha256",
]
