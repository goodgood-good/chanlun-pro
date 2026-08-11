from __future__ import annotations

from dataclasses import asdict, dataclass
from decimal import Decimal

from chanlun.decision_support.trading_system.parameters import (
    LIVE_STATUS,
    STRATEGY_ID,
    SelectionPath,
    StrategyParameters,
    snapshot_sha256,
)


RECURSIVE_1M_RESEARCH_ID = "CL-HIER-1M5M30M-RESEARCH"
RECURSIVE_1M_DIAGNOSTIC_EXECUTION_ID = (
    "CL-HIER-1M5M30M-DIAGNOSTIC-EXECUTION"
)
RESEARCH_STATUS = "RESEARCH_ONLY"


@dataclass(frozen=True, slots=True)
class Recursive1mResearchParameters:
    """Frozen user-authorized research override for the recursive hierarchy.

    The immutable strict strategy specification remains ``30m/5m/1m``.  This snapshot is a
    separate research contract for the user's later instruction
    ``L0=1m, L1=5m, L2=30m`` and can never be represented as the frozen strict strategy
    parameter set or enabled for live trading.
    """

    selection_path: SelectionPath
    research_id: str = RECURSIVE_1M_RESEARCH_ID
    source_strategy_spec_id: str = STRATEGY_ID
    source_frequency: str = "1m"
    l0_frequency: str = "1m"
    l1_frequency: str = "5m-derived"
    l2_frequency: str = "30m-derived"
    strategic_entry_rule: str = "L0_FIRST_COMPLETED_CENTER_THIRD_BUY"
    strategic_exit_rule: str = "L0_THIRD_SELL_ONLY_OTHER_EXITS_UNRESOLVED"
    higher_context_rule: str = "L1_AND_L2_VISIBLE_BEFORE_L0_DECISION"
    expansion_rule: str = "ACTIVE_EXPANSION_RECLASSIFYING_BLOCKS_ENTRY"
    nine_segment_rule: str = "NINE_CENTER_TOUCH_UNITS_DERIVE_HIGHER_CONTEXT"
    lower_locator_rule: str = "UNRESOLVED_BELOW_1M_L0"
    tactical_rule: str = "UNRESOLVED_DISABLED_CASH_RESERVED"
    entry_execution_boundary_rule: str = "RAW_CONFIRMATION_BAR_HIGH"
    entry_ttl_rule: str = "NEXT_COMPLETED_1M_BAR_OR_SESSION_END"
    persistent_exit_boundary_rule: str = "RAW_CONFIRMATION_BAR_LOW"
    strategic_fraction_of_slot: Decimal = Decimal("0.75")
    tactical_cash_reserve_fraction_of_slot: Decimal = Decimal("0.25")
    full_system_eligible: bool = False
    highest_status: str = RESEARCH_STATUS
    live_status: str = LIVE_STATUS

    def __post_init__(self) -> None:
        if self.selection_path not in {
            "INDIVIDUAL_THREE_PROGRAM",
            "ETF_PROXY",
        }:
            raise ValueError("unsupported recursive 1m selection path")
        expected = {
            "research_id": RECURSIVE_1M_RESEARCH_ID,
            "source_strategy_spec_id": STRATEGY_ID,
            "source_frequency": "1m",
            "l0_frequency": "1m",
            "l1_frequency": "5m-derived",
            "l2_frequency": "30m-derived",
            "strategic_entry_rule": "L0_FIRST_COMPLETED_CENTER_THIRD_BUY",
            "strategic_exit_rule": (
                "L0_THIRD_SELL_ONLY_OTHER_EXITS_UNRESOLVED"
            ),
            "higher_context_rule": "L1_AND_L2_VISIBLE_BEFORE_L0_DECISION",
            "expansion_rule": "ACTIVE_EXPANSION_RECLASSIFYING_BLOCKS_ENTRY",
            "nine_segment_rule": "NINE_CENTER_TOUCH_UNITS_DERIVE_HIGHER_CONTEXT",
            "lower_locator_rule": "UNRESOLVED_BELOW_1M_L0",
            "tactical_rule": "UNRESOLVED_DISABLED_CASH_RESERVED",
            "entry_execution_boundary_rule": "RAW_CONFIRMATION_BAR_HIGH",
            "entry_ttl_rule": "NEXT_COMPLETED_1M_BAR_OR_SESSION_END",
            "persistent_exit_boundary_rule": "RAW_CONFIRMATION_BAR_LOW",
            "strategic_fraction_of_slot": Decimal("0.75"),
            "tactical_cash_reserve_fraction_of_slot": Decimal("0.25"),
            "full_system_eligible": False,
            "highest_status": RESEARCH_STATUS,
            "live_status": LIVE_STATUS,
        }
        changed = tuple(
            name for name, value in expected.items() if getattr(self, name) != value
        )
        if changed:
            raise ValueError(
                "recursive 1m research contract changed: " + ",".join(changed)
            )
        if (
            self.strategic_fraction_of_slot
            + self.tactical_cash_reserve_fraction_of_slot
            != Decimal("1")
        ):
            raise ValueError("recursive 1m slot fractions must sum to one")

    @property
    def inherited_parameters(self) -> StrategyParameters:
        return StrategyParameters(self.selection_path)

    @property
    def strategic_slot_fraction(self) -> Decimal:
        return (
            self.inherited_parameters.slot_fraction
            * self.strategic_fraction_of_slot
        )

    def document(self) -> dict[str, object]:
        inherited = self.inherited_parameters
        return {
            "schema": "chanlun-recursive-1m-research-parameters",
            "research_id": self.research_id,
            "source_strategy_spec_id": self.source_strategy_spec_id,
            "selection_path": self.selection_path,
            "level_mapping": {
                "L0": self.l0_frequency,
                "L1": self.l1_frequency,
                "L2": self.l2_frequency,
            },
            "source_frequency": self.source_frequency,
            "strategic_entry_rule": self.strategic_entry_rule,
            "strategic_exit_rule": self.strategic_exit_rule,
            "higher_context_rule": self.higher_context_rule,
            "expansion_rule": self.expansion_rule,
            "nine_segment_rule": self.nine_segment_rule,
            "lower_locator_rule": self.lower_locator_rule,
            "tactical_rule": self.tactical_rule,
            "entry_execution_boundary_rule": self.entry_execution_boundary_rule,
            "entry_ttl_rule": self.entry_ttl_rule,
            "persistent_exit_boundary_rule": self.persistent_exit_boundary_rule,
            "strategic_fraction_of_slot": format(
                self.strategic_fraction_of_slot,
                "f",
            ),
            "tactical_cash_reserve_fraction_of_slot": (
                format(self.tactical_cash_reserve_fraction_of_slot, "f")
            ),
            "strategic_slot_fraction": format(self.strategic_slot_fraction, "f"),
            "inherited_parameter_set_id": inherited.parameter_set_id,
            "inherited_parameters": inherited.document(),
            "full_system_eligible": self.full_system_eligible,
            "highest_status": self.highest_status,
            "live_status": self.live_status,
        }

    @property
    def parameter_set_id(self) -> str:
        return snapshot_sha256(self.document())


@dataclass(frozen=True, slots=True)
class Recursive1mDiagnosticExecutionParameters:
    """Frozen counterfactual assumptions for a non-citable fill diagnostic.

    The local data do not contain broker-vintage ETF fee, quantity-increment,
    settlement, or statutory price-limit ledgers.  These values may therefore
    exercise the execution machinery, but can never make performance
    evaluable or promote the result above ``RESEARCH_ONLY``.
    """

    research_parameter_set_id: str
    execution_id: str = RECURSIVE_1M_DIAGNOSTIC_EXECUTION_ID
    entry_limit_rule: str = "RAW_CONFIRMATION_BAR_HIGH"
    entry_ttl_completed_bars: int = 1
    exit_floor_rule: str = "RAW_CONFIRMATION_BAR_LOW"
    persistent_exit: bool = True
    broker_latency_seconds: int = 0
    max_minute_volume_participation: Decimal = Decimal("0.05")
    buy_quantity_increment: int = 100
    sell_quantity_increment: int = 100
    settlement_t_plus_days: int = 1
    daily_limit_fraction: Decimal = Decimal("0.10")
    price_tick: Decimal = Decimal("0.001")
    commission_rate: Decimal = Decimal("0.0003")
    minimum_commission: Decimal = Decimal("5")
    etf_sell_stamp_rate: Decimal = Decimal("0")
    transfer_rate: Decimal = Decimal("0")
    fact_grade: str = "ASSUMPTION_ONLY_NOT_BROKER_VINTAGE"
    performance_evaluable: bool = False
    highest_status: str = RESEARCH_STATUS
    live_status: str = LIVE_STATUS

    def __post_init__(self) -> None:
        expected_parameter = recursive_1m_parameter_snapshot(
            "ETF_PROXY"
        ).parameter_set_id
        frozen = (
            self.research_parameter_set_id == expected_parameter
            and self.execution_id == RECURSIVE_1M_DIAGNOSTIC_EXECUTION_ID
            and self.entry_limit_rule == "RAW_CONFIRMATION_BAR_HIGH"
            and self.entry_ttl_completed_bars == 1
            and self.exit_floor_rule == "RAW_CONFIRMATION_BAR_LOW"
            and self.persistent_exit
            and self.broker_latency_seconds == 0
            and self.max_minute_volume_participation == Decimal("0.05")
            and self.buy_quantity_increment == 100
            and self.sell_quantity_increment == 100
            and self.settlement_t_plus_days == 1
            and self.daily_limit_fraction == Decimal("0.10")
            and self.price_tick == Decimal("0.001")
            and self.commission_rate == Decimal("0.0003")
            and self.minimum_commission == Decimal("5")
            and self.etf_sell_stamp_rate == 0
            and self.transfer_rate == 0
            and self.fact_grade == "ASSUMPTION_ONLY_NOT_BROKER_VINTAGE"
            and not self.performance_evaluable
            and self.highest_status == RESEARCH_STATUS
            and self.live_status == LIVE_STATUS
        )
        if not frozen:
            raise ValueError("recursive 1m diagnostic execution contract changed")

    def document(self) -> dict[str, object]:
        value = asdict(self)
        for name in (
            "max_minute_volume_participation",
            "daily_limit_fraction",
            "price_tick",
            "commission_rate",
            "minimum_commission",
            "etf_sell_stamp_rate",
            "transfer_rate",
        ):
            value[name] = format(getattr(self, name), "f")
        value["schema"] = "chanlun-recursive-1m-diagnostic-execution"
        return value

    @property
    def parameter_set_id(self) -> str:
        return snapshot_sha256(self.document())


def recursive_1m_diagnostic_execution_snapshot(
) -> Recursive1mDiagnosticExecutionParameters:
    research = recursive_1m_parameter_snapshot("ETF_PROXY")
    return Recursive1mDiagnosticExecutionParameters(research.parameter_set_id)


def recursive_1m_parameter_snapshot(
    selection_path: SelectionPath,
) -> Recursive1mResearchParameters:
    return Recursive1mResearchParameters(selection_path)


def recursive_1m_parameter_manifest() -> dict[str, object]:
    snapshots = {
        path: recursive_1m_parameter_snapshot(path)
        for path in ("INDIVIDUAL_THREE_PROGRAM", "ETF_PROXY")
    }
    if len({value.parameter_set_id for value in snapshots.values()}) != 2:
        raise RuntimeError("recursive 1m selection paths must remain distinct")
    payload: dict[str, object] = {
        "schema": "chanlun-recursive-1m-research-manifest",
        "research_id": RECURSIVE_1M_RESEARCH_ID,
        "source_strategy_spec_id": STRATEGY_ID,
        "highest_status": RESEARCH_STATUS,
        "live_status": LIVE_STATUS,
        "snapshots": {
            path: {
                "parameter_set_id": value.parameter_set_id,
                "parameters": value.document(),
            }
            for path, value in snapshots.items()
        },
        "diagnostic_execution": {
            "parameter_set_id": (
                recursive_1m_diagnostic_execution_snapshot().parameter_set_id
            ),
            "parameters": recursive_1m_diagnostic_execution_snapshot().document(),
        },
    }
    payload["manifest_sha256"] = snapshot_sha256(payload)
    return payload


__all__ = (
    "RECURSIVE_1M_RESEARCH_ID",
    "RECURSIVE_1M_DIAGNOSTIC_EXECUTION_ID",
    "RESEARCH_STATUS",
    "Recursive1mDiagnosticExecutionParameters",
    "Recursive1mResearchParameters",
    "recursive_1m_diagnostic_execution_snapshot",
    "recursive_1m_parameter_manifest",
    "recursive_1m_parameter_snapshot",
)
