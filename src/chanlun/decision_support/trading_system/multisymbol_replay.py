from __future__ import annotations

from dataclasses import asdict, dataclass, replace
from datetime import date, datetime
from decimal import Decimal, ROUND_DOWN
from math import sqrt
from statistics import mean, stdev
from typing import Literal

from chanlun.decision_support.fingerprints import normalize_datetime, sha256_json
from chanlun.decision_support.trading_system.bar_execution import (
    BarProxyExecutionStatus,
    BarProxyMatchResult,
    HistoricalMinuteExecutionBar,
    bar_proxy_parameter_snapshot,
    match_historical_minute_bars,
)
from chanlun.decision_support.trading_system.decision import (
    DecisionCore,
    DecisionInput,
    DecisionIntent,
)
from chanlun.decision_support.trading_system.direct_recursive_structure import (
    DirectRecursiveEntryChain,
    direct_recursive_alignment_contract,
)
from chanlun.decision_support.trading_system.execution import (
    InstrumentKind,
    FeeModel,
    OrderIntent,
)
from chanlun.decision_support.trading_system.parameters import (
    LIVE_STATUS,
    StrategyParameters,
    etf_parameter_snapshot,
    individual_parameter_snapshot,
)
from chanlun.decision_support.trading_system.portfolio import (
    CycleLedger,
    EntrySizingInput,
    HoldingLot,
    RestoreCohort,
    size_strategic_entry,
)
from chanlun.decision_support.trading_system.recent_year_research import (
    RECENT_YEAR_SELECTION_PATH,
    recent_year_research_parameters,
)
from chanlun.decision_support.trading_system.selection import (
    candidate_decision_sort_key,
)
from chanlun.decision_support.trading_system.technical_approximation import (
    ApproximateChanlunEntryChain,
    technical_approximation_alignment_contract,
    technical_approximation_parameters,
)
from chanlun.decision_support.trading_system.timeframe_override import (
    independent_timeframe_override,
)
from chanlun.decision_support.trading_system.timeframe_alignment import (
    ALIGNMENT_CONTRACT_ID,
    AlignedEntryChain,
    alignment_contract,
)


_ZERO = Decimal("0")
_ONE = Decimal("1")
_ORDER_ACTIONS = frozenset(
    {
        "ENTRY_INTENT",
        "STRATEGIC_REDUCE_INTENT",
        "STRATEGIC_EXIT_INTENT",
        "TACTICAL_SELL_INTENT",
        "TACTICAL_BUYBACK_INTENT",
        "PROTECTIVE_BUYBACK_INTENT",
        "THIRD_SELL_RECOVERY_BUYBACK_INTENT",
        "TACTICAL_THIRD_SELL_EXIT_INTENT",
    }
)
_BUY_ACTIONS = frozenset(
    {
        "ENTRY_INTENT",
        "TACTICAL_BUYBACK_INTENT",
        "PROTECTIVE_BUYBACK_INTENT",
        "THIRD_SELL_RECOVERY_BUYBACK_INTENT",
    }
)
_TACTICAL_BUY_ACTIONS = _BUY_ACTIONS - {"ENTRY_INTENT"}
_TACTICAL_SELL_ACTIONS = frozenset(
    {"TACTICAL_SELL_INTENT", "TACTICAL_THIRD_SELL_EXIT_INTENT"}
)
_STRATEGIC_SELL_ACTIONS = frozenset(
    {"STRATEGIC_REDUCE_INTENT", "STRATEGIC_EXIT_INTENT"}
)
_RISK_FACT_ACTIONS = frozenset(
    {
        "ENTRY_INTENT",
        "TACTICAL_SELL_INTENT",
        "TACTICAL_BUYBACK_INTENT",
        "PROTECTIVE_BUYBACK_INTENT",
        "THIRD_SELL_RECOVERY_BUYBACK_INTENT",
    }
)
ETF_REQUIRED_CANDIDATE_GATES = frozenset(
    {
        "selection_path",
        "research_visibility",
        "etf_proxy",
        "tradeability_visibility",
        "listing",
        "st",
        "suspension",
        "market_data",
        "continuity",
        "structure_history",
        "runtime_market_rules",
        "liquidity_history",
        "quote_coverage",
        "spread",
        "current_quote",
        "liquidity_quantity",
        "market_risk",
        "sector_risk",
        "symbol_risk",
        "sector_strength",
        "structure_visibility",
        "pen_definition",
        "observation_windows",
        "level_relation",
        "completed_structure",
        "first_l0_center",
        "l0_three_buy",
        "l1_departure_return",
        "third_buy_boundary",
        "l2_locator",
        "account_visibility",
        "operations",
        "slot",
        "drawdown",
        "active_order",
    }
)
INDIVIDUAL_REQUIRED_CANDIDATE_GATES = (
    ETF_REQUIRED_CANDIDATE_GATES
    - {"etf_proxy"}
    | {
        "sector_trigger",
        "industry_opportunity",
        "fundamental_role",
        "relative_value",
        "research_approximation",
    }
)
SECTOR_TECHNICAL_REQUIRED_CANDIDATE_GATES = (
    ETF_REQUIRED_CANDIDATE_GATES
    - {"etf_proxy", "research_visibility"}
    | {"sector_trigger"}
)


@dataclass(frozen=True, slots=True)
class StrictReplayContract:
    """Immutable identities for the user-authorized strict strategy replay path."""

    strategy_parameter_set_id: str
    timeframe_override_parameter_set_id: str
    effective_alignment_contract_id: str
    effective_alignment_parameter_set_id: str
    execution_parameter_set_id: str
    selection_path: str = "ETF_PROXY"
    l0_source_frequency: str = "30m"
    l1_source_frequency: str = "5m"
    l2_source_frequency: str = "1m"
    accepted_recursive_level: int = 0
    slot_count: int = 5
    slot_fraction: Decimal = Decimal("0.18")
    account_exposure_cap: Decimal = Decimal("0.90")
    tactical_ratio: Decimal = Decimal("0.25")
    settlement_t_plus_days: int = 1
    result_status: str = "RESEARCH_ONLY"
    live_status: str = LIVE_STATUS
    alignment_precedence: str = "EFFECTIVE_ALIGNMENT_IS_SIGNAL_AUTHORITY"

    def __post_init__(self) -> None:
        parent = etf_parameter_snapshot()
        override = independent_timeframe_override()
        alignment = alignment_contract()
        proxy = bar_proxy_parameter_snapshot(parent)
        expected = {
            "strategy_parameter_set_id": parent.parameter_set_id,
            "timeframe_override_parameter_set_id": override.parameter_set_id,
            "effective_alignment_contract_id": ALIGNMENT_CONTRACT_ID,
            "effective_alignment_parameter_set_id": alignment.parameter_set_id,
            "execution_parameter_set_id": proxy.execution_parameter_set_id,
        }
        changed = tuple(
            name
            for name, value in expected.items()
            if getattr(self, name) != value
        )
        if changed:
            raise ValueError(
                "strict strategy replay contract identity changed: " + ",".join(changed)
            )
        if (
            self.selection_path,
            self.l0_source_frequency,
            self.l1_source_frequency,
            self.l2_source_frequency,
            self.accepted_recursive_level,
        ) != ("ETF_PROXY", "30m", "5m", "1m", 0):
            raise ValueError("strict strategy replay timeframe/path mapping changed")
        frozen = (
            self.slot_count == parent.slot_count == 5
            and self.slot_fraction == parent.slot_fraction == Decimal("0.18")
            and self.account_exposure_cap
            == parent.account_exposure_cap
            == Decimal("0.90")
            and self.tactical_ratio == parent.tactical_ratio == Decimal("0.25")
            and self.settlement_t_plus_days == 1
        )
        if not frozen:
            raise ValueError("strict strategy replay portfolio parameters changed")
        if self.result_status != "RESEARCH_ONLY" or self.live_status != LIVE_STATUS:
            raise ValueError("strict strategy replay cannot enable live trading")

    @property
    def parameter_set_id(self) -> str:
        return sha256_json(asdict(self))


def strict_replay_contract() -> StrictReplayContract:
    parent = etf_parameter_snapshot()
    override = independent_timeframe_override()
    alignment = alignment_contract()
    proxy = bar_proxy_parameter_snapshot(parent)
    return StrictReplayContract(
        strategy_parameter_set_id=parent.parameter_set_id,
        timeframe_override_parameter_set_id=override.parameter_set_id,
        effective_alignment_contract_id=alignment.contract_id,
        effective_alignment_parameter_set_id=alignment.parameter_set_id,
        execution_parameter_set_id=proxy.execution_parameter_set_id,
    )


@dataclass(frozen=True, slots=True)
class StrictDirectReplayContract:
    """Replay identities for the one-graph direct-recursive strict strategy path."""

    strategy_parameter_set_id: str
    timeframe_override_parameter_set_id: None
    effective_alignment_contract_id: str
    effective_alignment_parameter_set_id: str
    execution_parameter_set_id: str
    selection_path: str = "ETF_PROXY"
    l0_source_frequency: str = "30m"
    l1_source_frequency: str = "5m"
    l2_source_frequency: str = "1m"
    accepted_recursive_level: int = 2
    slot_count: int = 5
    slot_fraction: Decimal = Decimal("0.18")
    account_exposure_cap: Decimal = Decimal("0.90")
    tactical_ratio: Decimal = Decimal("0.25")
    settlement_t_plus_days: int = 1
    result_status: str = "RESEARCH_ONLY"
    live_status: str = LIVE_STATUS
    level_relation_mode: str = "DIRECT_RECURSIVE"
    alignment_precedence: str = "DIRECT_RECURSION_IS_PRIMARY_SIGNAL_AUTHORITY"

    def __post_init__(self) -> None:
        parent = etf_parameter_snapshot()
        alignment = direct_recursive_alignment_contract()
        proxy = bar_proxy_parameter_snapshot(parent)
        identity = (
            self.strategy_parameter_set_id,
            self.timeframe_override_parameter_set_id,
            self.effective_alignment_contract_id,
            self.effective_alignment_parameter_set_id,
            self.execution_parameter_set_id,
        )
        if identity != (
            parent.parameter_set_id,
            None,
            alignment.contract_id,
            alignment.parameter_set_id,
            proxy.execution_parameter_set_id,
        ):
            raise ValueError("strict direct strategy replay contract identity changed")
        mapping = (
            self.selection_path,
            self.l0_source_frequency,
            self.l1_source_frequency,
            self.l2_source_frequency,
            self.accepted_recursive_level,
            self.level_relation_mode,
        )
        if mapping != (
            "ETF_PROXY",
            "30m",
            "5m",
            "1m",
            2,
            "DIRECT_RECURSIVE",
        ):
            raise ValueError("strict direct strategy replay mapping changed")
        if not (
            self.slot_count == parent.slot_count == 5
            and self.slot_fraction == parent.slot_fraction == Decimal("0.18")
            and self.account_exposure_cap
            == parent.account_exposure_cap
            == Decimal("0.90")
            and self.tactical_ratio == parent.tactical_ratio == Decimal("0.25")
            and self.settlement_t_plus_days == 1
        ):
            raise ValueError("strict direct strategy portfolio parameters changed")
        if self.result_status != "RESEARCH_ONLY" or self.live_status != LIVE_STATUS:
            raise ValueError("strict direct strategy replay cannot enable live trading")

    @property
    def parameter_set_id(self) -> str:
        return sha256_json(asdict(self))


def strict_direct_replay_contract() -> StrictDirectReplayContract:
    parent = etf_parameter_snapshot()
    alignment = direct_recursive_alignment_contract()
    proxy = bar_proxy_parameter_snapshot(parent)
    return StrictDirectReplayContract(
        strategy_parameter_set_id=parent.parameter_set_id,
        timeframe_override_parameter_set_id=None,
        effective_alignment_contract_id=alignment.contract_id,
        effective_alignment_parameter_set_id=alignment.parameter_set_id,
        execution_parameter_set_id=proxy.execution_parameter_set_id,
    )


@dataclass(frozen=True, slots=True)
class ResearchIndividualDirectReplayContract:
    """Frozen individual-stock replay contract for the explicit QMT proxy.

    This is not the signed ``INDIVIDUAL_THREE_PROGRAM`` authority required by
    the strict specification.  It exists solely so a reproducible research
    approximation can use the same decision, portfolio, T+1 and execution
    engine without being mislabeled as a complete-system result.
    """

    strategy_parameter_set_id: str
    research_approximation_parameter_set_id: str
    effective_alignment_contract_id: str
    effective_alignment_parameter_set_id: str
    execution_parameter_set_id: str
    timeframe_override_parameter_set_id: None = None
    selection_path: str = "INDIVIDUAL_THREE_PROGRAM"
    l0_source_frequency: str = "30m"
    l1_source_frequency: str = "5m"
    l2_source_frequency: str = "1m"
    accepted_recursive_level: int = 2
    slot_count: int = 5
    slot_fraction: Decimal = Decimal("0.18")
    account_exposure_cap: Decimal = Decimal("0.90")
    tactical_ratio: Decimal = Decimal("0.25")
    settlement_t_plus_days: int = 1
    result_status: str = "RESEARCH_ONLY"
    data_grade: str = "RESEARCH_APPROXIMATION"
    live_status: str = LIVE_STATUS
    level_relation_mode: str = "DIRECT_RECURSIVE"
    alignment_precedence: str = "DIRECT_RECURSION_IS_PRIMARY_SIGNAL_AUTHORITY"

    def __post_init__(self) -> None:
        parent = individual_parameter_snapshot()
        alignment = direct_recursive_alignment_contract()
        proxy = bar_proxy_parameter_snapshot(parent)
        if (
            self.strategy_parameter_set_id,
            self.effective_alignment_contract_id,
            self.effective_alignment_parameter_set_id,
            self.execution_parameter_set_id,
        ) != (
            parent.parameter_set_id,
            alignment.contract_id,
            alignment.parameter_set_id,
            proxy.execution_parameter_set_id,
        ):
            raise ValueError("research individual replay identity changed")
        if not self.research_approximation_parameter_set_id.startswith("sha256:"):
            raise ValueError("research approximation identity is required")
        if (
            self.selection_path,
            self.l0_source_frequency,
            self.l1_source_frequency,
            self.l2_source_frequency,
            self.accepted_recursive_level,
            self.level_relation_mode,
        ) != (
            "INDIVIDUAL_THREE_PROGRAM",
            "30m",
            "5m",
            "1m",
            2,
            "DIRECT_RECURSIVE",
        ):
            raise ValueError("research individual replay mapping changed")
        if not (
            self.slot_count == parent.slot_count == 5
            and self.slot_fraction == parent.slot_fraction == Decimal("0.18")
            and self.account_exposure_cap
            == parent.account_exposure_cap
            == Decimal("0.90")
            and self.tactical_ratio == parent.tactical_ratio == Decimal("0.25")
            and self.settlement_t_plus_days == 1
        ):
            raise ValueError("research individual portfolio parameters changed")
        if (
            self.result_status != "RESEARCH_ONLY"
            or self.data_grade != "RESEARCH_APPROXIMATION"
            or self.live_status != LIVE_STATUS
        ):
            raise ValueError("research individual replay cannot enable live trading")

    @property
    def parameter_set_id(self) -> str:
        return sha256_json(asdict(self))


def research_individual_direct_replay_contract(
    research_approximation_parameter_set_id: str,
) -> ResearchIndividualDirectReplayContract:
    parent = individual_parameter_snapshot()
    alignment = direct_recursive_alignment_contract()
    proxy = bar_proxy_parameter_snapshot(parent)
    return ResearchIndividualDirectReplayContract(
        strategy_parameter_set_id=parent.parameter_set_id,
        research_approximation_parameter_set_id=(
            research_approximation_parameter_set_id
        ),
        effective_alignment_contract_id=alignment.contract_id,
        effective_alignment_parameter_set_id=alignment.parameter_set_id,
        execution_parameter_set_id=proxy.execution_parameter_set_id,
    )


@dataclass(frozen=True, slots=True)
class ResearchSectorTechnicalDirectReplayContract:
    """Direct-recursive stock replay with the three-program gate disabled.

    This contract exists only for the separately frozen, user-authorized
    recent-year experiment.  It retains the stock portfolio, T+1, fee and bar
    execution rules while requiring a QMT sector trigger and technical chain.
    """

    strategy_parameter_set_id: str
    research_variant_parameter_set_id: str
    effective_alignment_contract_id: str
    effective_alignment_parameter_set_id: str
    execution_parameter_set_id: str
    timeframe_override_parameter_set_id: None = None
    selection_path: str = RECENT_YEAR_SELECTION_PATH
    l0_source_frequency: str = "30m"
    l1_source_frequency: str = "5m"
    l2_source_frequency: str = "1m"
    accepted_recursive_level: int = 2
    slot_count: int = 5
    slot_fraction: Decimal = Decimal("0.18")
    account_exposure_cap: Decimal = Decimal("0.90")
    tactical_ratio: Decimal = Decimal("0.25")
    settlement_t_plus_days: int = 1
    result_status: str = "RESEARCH_ONLY"
    data_grade: str = "RESEARCH_ONLY"
    live_status: str = LIVE_STATUS
    level_relation_mode: str = "DIRECT_RECURSIVE"
    alignment_precedence: str = "DIRECT_RECURSION_IS_PRIMARY_SIGNAL_AUTHORITY"

    def __post_init__(self) -> None:
        parent = individual_parameter_snapshot()
        variant = recent_year_research_parameters()
        alignment = direct_recursive_alignment_contract()
        proxy = bar_proxy_parameter_snapshot(parent)
        if (
            self.strategy_parameter_set_id,
            self.research_variant_parameter_set_id,
            self.effective_alignment_contract_id,
            self.effective_alignment_parameter_set_id,
            self.execution_parameter_set_id,
        ) != (
            parent.parameter_set_id,
            variant.parameter_set_id,
            alignment.contract_id,
            alignment.parameter_set_id,
            proxy.execution_parameter_set_id,
        ):
            raise ValueError("recent-year sector-technical replay identity changed")
        if (
            self.selection_path,
            self.l0_source_frequency,
            self.l1_source_frequency,
            self.l2_source_frequency,
            self.accepted_recursive_level,
            self.level_relation_mode,
        ) != (
            RECENT_YEAR_SELECTION_PATH,
            "30m",
            "5m",
            "1m",
            2,
            "DIRECT_RECURSIVE",
        ):
            raise ValueError("recent-year sector-technical mapping changed")
        if not (
            self.slot_count == parent.slot_count == variant.slot_count == 5
            and self.slot_fraction
            == parent.slot_fraction
            == variant.slot_fraction
            == Decimal("0.18")
            and self.account_exposure_cap
            == parent.account_exposure_cap
            == variant.account_exposure_cap
            == Decimal("0.90")
            and self.tactical_ratio
            == parent.tactical_ratio
            == variant.tactical_ratio
            == Decimal("0.25")
            and self.settlement_t_plus_days == variant.settlement_t_plus_days == 1
        ):
            raise ValueError("recent-year sector-technical portfolio changed")
        if (
            self.result_status != "RESEARCH_ONLY"
            or self.data_grade != "RESEARCH_ONLY"
            or self.live_status != LIVE_STATUS
        ):
            raise ValueError("recent-year sector-technical replay cannot enable live")

    @property
    def parameter_set_id(self) -> str:
        return sha256_json(asdict(self))


def research_sector_technical_direct_replay_contract(
) -> ResearchSectorTechnicalDirectReplayContract:
    parent = individual_parameter_snapshot()
    variant = recent_year_research_parameters()
    alignment = direct_recursive_alignment_contract()
    proxy = bar_proxy_parameter_snapshot(parent)
    return ResearchSectorTechnicalDirectReplayContract(
        strategy_parameter_set_id=parent.parameter_set_id,
        research_variant_parameter_set_id=variant.parameter_set_id,
        effective_alignment_contract_id=alignment.contract_id,
        effective_alignment_parameter_set_id=alignment.parameter_set_id,
        execution_parameter_set_id=proxy.execution_parameter_set_id,
    )


@dataclass(frozen=True, slots=True)
class ResearchSectorTechnicalApproxReplayContract:
    """Research-only replay contract for approximate technical points.

    Portfolio, T+1, fees, selection and execution remain identical to the
    recent-year stock experiment.  Only the structure alignment authority is
    replaced by the explicitly approximate, causal confirmed-point contract.
    """

    strategy_parameter_set_id: str
    research_variant_parameter_set_id: str
    technical_approximation_parameter_set_id: str
    effective_alignment_contract_id: str
    effective_alignment_parameter_set_id: str
    execution_parameter_set_id: str
    timeframe_override_parameter_set_id: None = None
    selection_path: str = RECENT_YEAR_SELECTION_PATH
    l0_source_frequency: str = "30m"
    l1_source_frequency: str = "5m"
    l2_source_frequency: str = "1m"
    accepted_recursive_level: int = 2
    slot_count: int = 5
    slot_fraction: Decimal = Decimal("0.18")
    account_exposure_cap: Decimal = Decimal("0.90")
    tactical_ratio: Decimal = Decimal("0.25")
    settlement_t_plus_days: int = 1
    result_status: str = "RESEARCH_ONLY"
    data_grade: str = "RESEARCH_APPROXIMATION"
    live_status: str = LIVE_STATUS
    level_relation_mode: str = "CAUSAL_CONFIRMED_POINT_APPROXIMATION"
    alignment_precedence: str = "APPROXIMATION_IS_RESEARCH_SIGNAL_AUTHORITY"

    def __post_init__(self) -> None:
        parent = individual_parameter_snapshot()
        variant = recent_year_research_parameters()
        technical = technical_approximation_parameters()
        alignment = technical_approximation_alignment_contract()
        proxy = bar_proxy_parameter_snapshot(parent)
        if (
            self.strategy_parameter_set_id,
            self.research_variant_parameter_set_id,
            self.technical_approximation_parameter_set_id,
            self.effective_alignment_contract_id,
            self.effective_alignment_parameter_set_id,
            self.execution_parameter_set_id,
        ) != (
            parent.parameter_set_id,
            variant.parameter_set_id,
            technical.parameter_set_id,
            alignment.contract_id,
            alignment.parameter_set_id,
            proxy.execution_parameter_set_id,
        ):
            raise ValueError("technical approximation replay identity changed")
        if (
            self.selection_path,
            self.l0_source_frequency,
            self.l1_source_frequency,
            self.l2_source_frequency,
            self.accepted_recursive_level,
            self.level_relation_mode,
        ) != (
            RECENT_YEAR_SELECTION_PATH,
            "30m",
            "5m",
            "1m",
            2,
            "CAUSAL_CONFIRMED_POINT_APPROXIMATION",
        ):
            raise ValueError("technical approximation replay mapping changed")
        if not (
            self.slot_count == parent.slot_count == variant.slot_count == 5
            and self.slot_fraction
            == parent.slot_fraction
            == variant.slot_fraction
            == Decimal("0.18")
            and self.account_exposure_cap
            == parent.account_exposure_cap
            == variant.account_exposure_cap
            == Decimal("0.90")
            and self.tactical_ratio
            == parent.tactical_ratio
            == variant.tactical_ratio
            == Decimal("0.25")
            and self.settlement_t_plus_days == variant.settlement_t_plus_days == 1
        ):
            raise ValueError("technical approximation portfolio parameters changed")
        if (
            self.result_status != "RESEARCH_ONLY"
            or self.data_grade != "RESEARCH_APPROXIMATION"
            or self.live_status != LIVE_STATUS
        ):
            raise ValueError("technical approximation replay cannot enable live")

    @property
    def parameter_set_id(self) -> str:
        return sha256_json(asdict(self))


def research_sector_technical_approx_replay_contract(
) -> ResearchSectorTechnicalApproxReplayContract:
    parent = individual_parameter_snapshot()
    variant = recent_year_research_parameters()
    technical = technical_approximation_parameters()
    alignment = technical_approximation_alignment_contract()
    proxy = bar_proxy_parameter_snapshot(parent)
    return ResearchSectorTechnicalApproxReplayContract(
        strategy_parameter_set_id=parent.parameter_set_id,
        research_variant_parameter_set_id=variant.parameter_set_id,
        technical_approximation_parameter_set_id=technical.parameter_set_id,
        effective_alignment_contract_id=alignment.contract_id,
        effective_alignment_parameter_set_id=alignment.parameter_set_id,
        execution_parameter_set_id=proxy.execution_parameter_set_id,
    )


ReplayContract = (
    StrictReplayContract
    | StrictDirectReplayContract
    | ResearchIndividualDirectReplayContract
    | ResearchSectorTechnicalDirectReplayContract
    | ResearchSectorTechnicalApproxReplayContract
)


def _validate_ids(values: tuple[str, ...], field_name: str) -> None:
    if any(not value or not value.strip() for value in values):
        raise ValueError(f"{field_name} contains an empty identity")
    if len(values) != len(set(values)):
        raise ValueError(f"{field_name} identities must be unique")


@dataclass(frozen=True, slots=True)
class ReplayFactBindings:
    """Point-in-time provenance attached to one strict strategy decision input."""

    timeframe_override_parameter_set_id: str | None
    alignment_contract_id: str | None
    alignment_parameter_set_id: str | None
    frozen_structure_fact_ids: tuple[str, ...]
    selection_fact_ids: tuple[str, ...]
    risk_fact_ids: tuple[str, ...]
    aligned_entry_chain: (
        AlignedEntryChain
        | DirectRecursiveEntryChain
        | ApproximateChanlunEntryChain
        | None
    ) = None
    all_required_facts_resolved: bool = True
    unresolved_reason_codes: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        for name in (
            "frozen_structure_fact_ids",
            "selection_fact_ids",
            "risk_fact_ids",
            "unresolved_reason_codes",
        ):
            values = tuple(getattr(self, name))
            object.__setattr__(self, name, values)
            _validate_ids(values, name)
        if not self.all_required_facts_resolved and not self.unresolved_reason_codes:
            raise ValueError("unresolved bindings require explicit reason codes")
        if self.all_required_facts_resolved and self.unresolved_reason_codes:
            raise ValueError("resolved bindings cannot carry unresolved reasons")


@dataclass(frozen=True, slots=True)
class ReplayPriceFact:
    symbol: str
    available_at: datetime
    raw_close: Decimal
    source_id: str
    complete: bool = True
    price_basis: str = "RAW_UNADJUSTED"

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "available_at",
            normalize_datetime(self.available_at, "available_at"),
        )
        if not self.symbol or not self.source_id or self.raw_close <= 0:
            raise ValueError("replay mark identity and positive price are required")
        if self.price_basis != "RAW_UNADJUSTED":
            raise ValueError("replay valuation only accepts raw point-in-time prices")


@dataclass(frozen=True, slots=True)
class ReplayCashDistributionFact:
    action_id: str
    symbol: str
    effective_at: datetime
    known_at: datetime
    cash_per_share: Decimal
    source_id: str
    source_ledger_sha256: str
    point_in_time_complete: bool = True

    def __post_init__(self) -> None:
        effective = normalize_datetime(self.effective_at, "effective_at")
        known = normalize_datetime(self.known_at, "known_at")
        object.__setattr__(self, "effective_at", effective)
        object.__setattr__(self, "known_at", known)
        if not all(
            value and value.strip()
            for value in (
                self.action_id,
                self.symbol,
                self.source_id,
                self.source_ledger_sha256,
            )
        ):
            raise ValueError("cash-distribution audit identity is required")
        if self.cash_per_share < 0:
            raise ValueError("cash distribution cannot be negative")
        if known > effective:
            raise ValueError("cash distribution was not known by its effective time")


@dataclass(frozen=True, slots=True)
class ReplayMandatoryShareActionFact:
    """Broker-visible non-trade quantity transformation at an ex-date.

    Rights subscriptions are deliberately excluded by the input adapter.  This
    fact is for mandatory bonus/split/consolidation multipliers only; the local
    cycle ledger performs the quantity and odd-lot normalization.
    """

    action_id: str
    symbol: str
    effective_at: datetime
    known_at: datetime
    share_multiplier: Decimal
    source_id: str
    source_ledger_sha256: str
    point_in_time_complete: bool = True

    def __post_init__(self) -> None:
        effective = normalize_datetime(self.effective_at, "effective_at")
        known = normalize_datetime(self.known_at, "known_at")
        object.__setattr__(self, "effective_at", effective)
        object.__setattr__(self, "known_at", known)
        if not all(
            value and value.strip()
            for value in (
                self.action_id,
                self.symbol,
                self.source_id,
                self.source_ledger_sha256,
            )
        ):
            raise ValueError("mandatory share action identity is required")
        if self.share_multiplier <= 0 or self.share_multiplier == _ONE:
            raise ValueError("mandatory share action requires a non-unit multiplier")
        if known > effective:
            raise ValueError("mandatory share action was not known by its effective time")


@dataclass(frozen=True, slots=True)
class ReplayDecisionEvent:
    event_id: str
    facts: DecisionInput
    bindings: ReplayFactBindings
    created_at: datetime
    broker_confirmed_at: datetime
    expires_at: datetime | None
    execution_status: BarProxyExecutionStatus
    broker_position_quantity: int | None
    bars: tuple[HistoricalMinuteExecutionBar, ...]
    persistent_intent_id: str | None = None
    account_position_source: Literal[
        "EXTERNAL_SNAPSHOT",
        "EVENT_SOURCED_REPLAY",
    ] = "EXTERNAL_SNAPSHOT"
    sellable_quantity_source: Literal[
        "EXTERNAL_SNAPSHOT",
        "EVENT_SOURCED_REPLAY",
    ] = "EXTERNAL_SNAPSHOT"

    def __post_init__(self) -> None:
        created = normalize_datetime(self.created_at, "created_at")
        confirmed = normalize_datetime(
            self.broker_confirmed_at,
            "broker_confirmed_at",
        )
        expires = (
            None
            if self.expires_at is None
            else normalize_datetime(self.expires_at, "expires_at")
        )
        object.__setattr__(self, "created_at", created)
        object.__setattr__(self, "broker_confirmed_at", confirmed)
        object.__setattr__(self, "expires_at", expires)
        object.__setattr__(self, "bars", tuple(self.bars))
        if not self.event_id:
            raise ValueError("replay event identity is invalid")
        if self.persistent_intent_id is not None and not self.persistent_intent_id.strip():
            raise ValueError("persistent replay intent identity cannot be empty")
        if self.account_position_source == "EXTERNAL_SNAPSHOT":
            if (
                self.broker_position_quantity is None
                or self.broker_position_quantity < 0
            ):
                raise ValueError("external replay account position is invalid")
        elif (
            self.account_position_source != "EVENT_SOURCED_REPLAY"
            or self.broker_position_quantity is not None
        ):
            raise ValueError("event-sourced replay position must be engine-derived")
        if self.sellable_quantity_source not in {
            "EXTERNAL_SNAPSHOT",
            "EVENT_SOURCED_REPLAY",
        }:
            raise ValueError("unsupported replay sellable-quantity source")
        if not self.facts.confirmation_time <= created <= confirmed:
            raise ValueError("replay order timing precedes completed decision evidence")
        if expires is not None and expires < confirmed:
            raise ValueError("replay expiry precedes broker confirmation")
        if any(bar.symbol != self.facts.symbol for bar in self.bars):
            raise ValueError("replay event contains another symbol's execution bar")


@dataclass(frozen=True, slots=True)
class ReplayBatch:
    batch_id: str
    decision_at: datetime
    valuation_at: datetime
    events: tuple[ReplayDecisionEvent, ...]
    decision_marks: tuple[ReplayPriceFact, ...] = ()
    valuation_marks: tuple[ReplayPriceFact, ...] = ()
    cash_distributions: tuple[ReplayCashDistributionFact, ...] = ()
    mandatory_share_actions: tuple[ReplayMandatoryShareActionFact, ...] = ()

    def __post_init__(self) -> None:
        decision = normalize_datetime(self.decision_at, "decision_at")
        valuation = normalize_datetime(self.valuation_at, "valuation_at")
        object.__setattr__(self, "decision_at", decision)
        object.__setattr__(self, "valuation_at", valuation)
        object.__setattr__(self, "events", tuple(self.events))
        object.__setattr__(self, "decision_marks", tuple(self.decision_marks))
        object.__setattr__(self, "valuation_marks", tuple(self.valuation_marks))
        object.__setattr__(
            self,
            "cash_distributions",
            tuple(self.cash_distributions),
        )
        object.__setattr__(
            self,
            "mandatory_share_actions",
            tuple(self.mandatory_share_actions),
        )
        if not self.batch_id or valuation < decision:
            raise ValueError("replay batch identity or time interval is invalid")
        event_ids = tuple(event.event_id for event in self.events)
        symbols = tuple(event.facts.symbol for event in self.events)
        if len(event_ids) != len(set(event_ids)):
            raise ValueError("replay batch event ids must be unique")
        if len(symbols) != len(set(symbols)):
            raise ValueError("one batch permits at most one event per symbol")
        for event in self.events:
            if event.facts.decision_time != decision:
                raise ValueError("event decision time differs from its batch")
            if event.broker_confirmed_at > valuation:
                raise ValueError("broker confirmation is after batch valuation")
            if any(bar.closed_at > valuation for bar in event.bars):
                raise ValueError("execution bar is after batch valuation")
        for marks, cutoff, name in (
            (self.decision_marks, decision, "decision"),
            (self.valuation_marks, valuation, "valuation"),
        ):
            mark_symbols = tuple(value.symbol for value in marks)
            if len(mark_symbols) != len(set(mark_symbols)):
                raise ValueError(f"{name} marks must be unique by symbol")
            if any(value.available_at > cutoff for value in marks):
                raise ValueError(f"future {name} mark is not admissible")
        action_ids = tuple(value.action_id for value in self.cash_distributions)
        if len(action_ids) != len(set(action_ids)):
            raise ValueError("cash-distribution ids must be unique in one batch")
        if any(
            value.effective_at > decision or value.known_at > decision
            for value in self.cash_distributions
        ):
            raise ValueError("future cash-distribution fact is not admissible")
        share_action_ids = tuple(
            value.action_id for value in self.mandatory_share_actions
        )
        if len(share_action_ids) != len(set(share_action_ids)):
            raise ValueError("mandatory share-action ids must be unique in one batch")
        if any(
            value.effective_at > decision or value.known_at > decision
            for value in self.mandatory_share_actions
        ):
            raise ValueError("future mandatory share-action fact is not admissible")


@dataclass(frozen=True, slots=True)
class ReplayIntentRecord:
    batch_id: str
    event_id: str
    intent: DecisionIntent
    fact_gate_reason_codes: tuple[str, ...]
    # Stable identity of the source signal whose exit may be retried.  This is
    # deliberately carried into the audit record: ``event_id`` changes at
    # every decision grid and therefore cannot distinguish one persistent
    # signal from dozens of retries of that same signal.
    persistent_intent_id: str | None = None
    # The decision core remains immutable.  Portfolio scheduling is a separate
    # decision-time phase, so both its requested quantity and the quantity that
    # was actually reserved before matching stay visible in the audit trail.
    requested_quantity: int = 0
    scheduled_quantity: int = 0
    reserved_cash_at_decision: Decimal = _ZERO
    reserved_entry_notional_at_decision: Decimal = _ZERO
    reserved_slot_number: int | None = None
    scheduler_capacity_rows: tuple[tuple[str, int], ...] = ()
    scheduler_reason_codes: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ReplayOrderRecord:
    batch_id: str
    event_id: str
    intent_action: str
    order: OrderIntent
    match: BarProxyMatchResult


@dataclass(frozen=True, slots=True)
class ReplayRejection:
    batch_id: str
    event_id: str
    symbol: str
    stage: Literal["FACT_GATE", "PORTFOLIO_GATE", "MATCHER"]
    intent_action: str
    reason_codes: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ReplayCorporateActionRecord:
    batch_id: str
    action_id: str
    symbol: str
    effective_at: datetime
    held_quantity: int
    cash_distribution: Decimal
    foregone_restore_distribution: Decimal
    applied: bool
    reason_codes: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ReplayMandatoryShareActionRecord:
    batch_id: str
    action_id: str
    symbol: str
    effective_at: datetime
    share_multiplier: Decimal
    quantity_before: int
    quantity_after: int
    applied: bool
    reason_codes: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ReplayEquityPoint:
    observed_at: datetime
    cash: Decimal
    market_value: Decimal
    equity: Decimal
    committed_exposure: Decimal
    restore_cash_reserve: Decimal
    occupied_slots: int
    complete: bool
    reason_codes: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ReplayPositionSnapshot:
    symbol: str
    slot_number: int
    cycle_id: str
    strategic_state: str
    quantity: int
    tactical_held_quantity: int
    pending_restore_quantity: int
    restore_cash_reserve: Decimal
    restore_cohort_ids: tuple[str, ...]
    completed_tactical_cycle_sessions: tuple[date, ...]
    opened_at: datetime
    cumulative_cash_flow: Decimal
    cumulative_fees: Decimal
    entry_cash: Decimal = _ZERO
    turnover_notional: Decimal = _ZERO
    tactical_cycles_completed: int = 0
    last_price: Decimal | None = None
    market_value: Decimal | None = None
    marked_at: datetime | None = None
    mark_complete: bool = False


@dataclass(frozen=True, slots=True)
class ReplayClosedCycle:
    symbol: str
    cycle_id: str
    slot_number: int
    opened_at: datetime
    closed_at: datetime
    entry_cash: Decimal
    net_pnl: Decimal
    net_return: Decimal
    total_fees: Decimal
    turnover_notional: Decimal
    tactical_cycles_completed: int


@dataclass(frozen=True, slots=True)
class ReplayMetrics:
    #: 账本自洽性。它只说明净值恒等式没有 unresolved 事实且权益恒为正，
    #: **不表示** ``net_return``/``max_drawdown`` 是可引用的策略绩效。
    ledger_valid: bool
    #: 只有真正产生过成交、形成过战略周期时，收益字段才可被引用。
    performance_evaluable: bool
    #: 零订单/零成交的空回放。空回放的 0 收益、0 回撤是恒等式产物。
    empty_replay: bool
    net_return: Decimal
    max_drawdown: Decimal
    annualized_return: Decimal | None
    sharpe: Decimal | None
    win_rate: Decimal | None
    payoff_ratio: Decimal | None
    profit_factor: Decimal | None
    turnover: Decimal
    total_fees: Decimal
    strategic_cycle_count: int
    tactical_cycle_count: int
    open_cycle_count: int
    order_count: int
    fill_count: int
    rejection_count: int
    strategic_sample_insufficient: bool
    tactical_sample_insufficient: bool
    warnings: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ReplayRunResult:
    contract: ReplayContract
    initial_cash: Decimal
    final_cash: Decimal
    intents: tuple[ReplayIntentRecord, ...]
    orders: tuple[ReplayOrderRecord, ...]
    rejections: tuple[ReplayRejection, ...]
    corporate_actions: tuple[ReplayCorporateActionRecord, ...]
    mandatory_share_actions: tuple[ReplayMandatoryShareActionRecord, ...]
    equity_curve: tuple[ReplayEquityPoint, ...]
    positions: tuple[ReplayPositionSnapshot, ...]
    closed_cycles: tuple[ReplayClosedCycle, ...]
    metrics: ReplayMetrics
    unresolved_reason_codes: tuple[str, ...]
    # A persistent source signal is resolved either after its requested order
    # has filled completely or after the event-sourced ledger proves that it
    # has no position/inventory to act on.  Later events with the same id are
    # idempotent retries and are suppressed from the decision audit.
    resolved_persistent_intent_ids: tuple[str, ...] = ()
    suppressed_persistent_event_counts: tuple[tuple[str, int], ...] = ()
    result_status: str = "RESEARCH_ONLY"
    live_status: str = LIVE_STATUS


@dataclass(slots=True)
class _PositionState:
    symbol: str
    slot_number: int
    ledger: CycleLedger
    opened_at: datetime
    cumulative_cash_flow: Decimal
    cumulative_fees: Decimal
    turnover_notional: Decimal
    entry_cash: Decimal
    tactical_cycles_completed: int = 0


@dataclass(slots=True)
class _ReplayState:
    cash: Decimal
    positions: dict[str, _PositionState]
    last_marks: dict[str, ReplayPriceFact]
    high_water_equity: Decimal
    intents: list[ReplayIntentRecord]
    orders: list[ReplayOrderRecord]
    rejections: list[ReplayRejection]
    corporate_actions: list[ReplayCorporateActionRecord]
    mandatory_share_actions: list[ReplayMandatoryShareActionRecord]
    applied_corporate_action_ids: set[str]
    equity_curve: list[ReplayEquityPoint]
    closed_cycles: list[ReplayClosedCycle]
    unresolved_reasons: list[str]
    resolved_persistent_intent_ids: set[str]
    suppressed_persistent_event_counts: dict[str, int]


@dataclass(slots=True)
class _BatchAllocationState:
    """Resources visible and reservable at one immutable decision snapshot."""

    cash_remaining: Decimal
    restore_cash_remaining: dict[str, Decimal]
    occupied_slots: set[int]
    reserved_entry_symbols: set[str]
    reserved_entry_notional: Decimal


@dataclass(frozen=True, slots=True)
class _OrderSchedule:
    scheduled_intent: DecisionIntent | None
    rejection_reason_codes: tuple[str, ...] = ()
    scheduler_reason_codes: tuple[str, ...] = ()
    reserved_cash: Decimal = _ZERO
    reserved_entry_notional: Decimal = _ZERO
    reserved_slot_number: int | None = None
    capacity_rows: tuple[tuple[str, int], ...] = ()


def _allocate_amount(
    total: Decimal,
    notionals: tuple[Decimal, ...],
    quantum: Decimal,
) -> tuple[Decimal, ...]:
    if total < 0 or quantum <= 0 or any(value <= 0 for value in notionals):
        raise ValueError("allocation values are invalid")
    if not notionals:
        return ()
    denominator = sum(notionals, _ZERO)
    allocated: list[Decimal] = []
    consumed = _ZERO
    for notional in notionals[:-1]:
        value = (total * notional / denominator).quantize(
            quantum,
            rounding=ROUND_DOWN,
        )
        allocated.append(value)
        consumed += value
    allocated.append(total - consumed)
    return tuple(allocated)


def _local_sellable_quantity(ledger: CycleLedger) -> int:
    cutoff = ledger.session.toordinal() - ledger.t_plus_days
    return sum(
        lot.quantity
        for lot in ledger.holding_lots
        if lot.acquired_on.toordinal() <= cutoff
    )


def _consume_tactical_lots(
    ledger: CycleLedger,
    *,
    match: BarProxyMatchResult,
    allow_existing_restore: bool,
    currency_quantum: Decimal,
) -> CycleLedger:
    fills = match.fills
    quantity = sum(fill.quantity for fill in fills)
    if not fills or quantity != match.filled_quantity:
        raise ValueError("tactical fill batch is inconsistent")
    if ledger.strategic_state != "S_ACTIVE_FULL":
        raise ValueError("tactical sell requires a full strategic position")
    if not allow_existing_restore and ledger.pending_restore_qty:
        raise ValueError("ordinary tactical sell cannot add to pending restore")
    if not allow_existing_restore and ledger.tactical_cycles_completed_today:
        raise ValueError("ordinary tactical daily cycle limit was consumed")
    if quantity > ledger.tactical_eligible_qty:
        raise ValueError("tactical fill exceeds local T+1 inventory")
    if any(fill.quantity % ledger.sell_quantity_increment for fill in fills):
        raise ValueError("tactical fill violates the sell increment")

    remaining = quantity
    lots: list[HoldingLot] = []
    cutoff = ledger.session.toordinal() - ledger.t_plus_days
    for lot in ledger.holding_lots:
        if (
            remaining
            and lot.bucket == "TACTICAL"
            and lot.acquired_on.toordinal() <= cutoff
        ):
            consumed = min(remaining, lot.quantity)
            remaining -= consumed
            if consumed < lot.quantity:
                lots.append(replace(lot, quantity=lot.quantity - consumed))
        else:
            lots.append(lot)
    if remaining:
        raise ValueError("tactical lot consumption was incomplete")

    notionals = tuple(
        Decimal(fill.quantity) * fill.execution_price for fill in fills
    )
    costs = _allocate_amount(match.total_fees, notionals, currency_quantum)
    reserves = (
        tuple(notional - cost for notional, cost in zip(notionals, costs, strict=True))
        if match.total_fees <= sum(notionals, _ZERO)
        else tuple(_ZERO for _value in notionals)
    )
    cohorts = tuple(
        RestoreCohort(
            restore_cohort_id=(
                f"{ledger.cycle_id}:restore:{fill.execution_id}"
            ),
            sell_execution_id=fill.execution_id,
            sell_exchange_time=fill.exchange_time,
            open_qty=fill.quantity,
            remaining_qty=fill.quantity,
            gross_sell_cash=notional,
            allocated_sell_cost=cost,
            cash_reserve_remaining=reserve,
        )
        for fill, notional, cost, reserve in zip(
            fills,
            notionals,
            costs,
            reserves,
            strict=True,
        )
    )
    return replace(
        ledger,
        holding_lots=tuple(lots),
        restore_cohorts=ledger.restore_cohorts + cohorts,
    )


def _prepare_order(
    intent: DecisionIntent,
    event: ReplayDecisionEvent,
    *,
    parameters: StrategyParameters,
    instrument_kind: InstrumentKind,
) -> OrderIntent:
    if intent.action not in _ORDER_ACTIONS:
        raise ValueError("non-order strict strategy intent cannot be prepared")
    if intent.quantity <= 0 or intent.price_cap_or_floor is None:
        raise ValueError("order-producing intent lacks quantity or price boundary")
    if intent.persistence == "OPTIONAL" and event.expires_at is None:
        raise ValueError("optional strict strategy replay order requires an explicit expiry")
    if intent.persistence == "PERSISTENT_EXIT" and event.expires_at is not None:
        raise ValueError("persistent strict strategy replay exit cannot expire")
    side: Literal["buy", "sell"] = (
        "buy" if intent.action in _BUY_ACTIONS else "sell"
    )
    increment = (
        event.execution_status.buy_quantity_increment
        if side == "buy"
        else event.execution_status.sell_quantity_increment
    )
    identity = {
        "schema": "strict-multisymbol-order",
        "strategy_parameter_set_id": parameters.parameter_set_id,
        "event_id": event.event_id,
        "action": intent.action,
        "rule_id": intent.rule_id,
        "symbol": intent.symbol,
        "quantity": intent.quantity,
        "limit": intent.price_cap_or_floor,
        "confirmation_time": intent.confirmation_time,
        "created_at": event.created_at,
        "broker_confirmed_at": event.broker_confirmed_at,
    }
    digest = sha256_json(identity)[7:]
    replay_intent_id = f"replay-intent:{digest}"
    if intent.persistence == "PERSISTENT_EXIT":
        replay_intent_id = event.persistent_intent_id or replay_intent_id
    return OrderIntent(
        client_order_id=f"replay-order:{digest}",
        intent_id=replay_intent_id,
        parameter_set_id=parameters.parameter_set_id,
        rule_id=intent.rule_id,
        structure_snapshot_id=intent.structure_snapshot_id,
        selection_snapshot_id=intent.selection_snapshot_id,
        account_snapshot_id=intent.account_snapshot_id,
        symbol=intent.symbol,
        instrument_kind=instrument_kind,
        side=side,
        quantity=intent.quantity,
        limit_price=intent.price_cap_or_floor,
        signal_bar_end=intent.confirmation_time,
        created_at=event.created_at,
        broker_confirmed_at=event.broker_confirmed_at,
        expires_at=event.expires_at,
        persistence=intent.persistence,  # type: ignore[arg-type]
        quantity_increment=increment,
    )


class StrictMultiSymbolReplayEngine:
    """Causal five-slot strict strategy replay over externally frozen decision facts.

    The engine never derives a Chanlun signal.  It injects its event-sourced
    account ledger into the supplied point-in-time facts, calls the shared
    :class:`DecisionCore` exactly once per event, and delegates every fill
    decision to the existing completed-minute matcher.
    """

    def __init__(
        self,
        *,
        initial_cash: Decimal,
        started_at: datetime,
        fee_model: FeeModel,
        decision_core: DecisionCore | None = None,
        contract: ReplayContract | None = None,
    ) -> None:
        self.initial_cash = Decimal(initial_cash)
        self.started_at = normalize_datetime(started_at, "started_at")
        if self.initial_cash <= 0:
            raise ValueError("strict strategy replay initial cash must be positive")
        self.fee_model = fee_model
        self.decision_core = decision_core or DecisionCore()
        stock_paths = {
            "INDIVIDUAL_THREE_PROGRAM",
            RECENT_YEAR_SELECTION_PATH,
        }
        self.parameters = (
            individual_parameter_snapshot()
            if contract is not None and contract.selection_path in stock_paths
            else etf_parameter_snapshot()
        )
        self.proxy_parameters = bar_proxy_parameter_snapshot(self.parameters)
        self.contract = contract or strict_replay_contract()
        self.instrument_kind: InstrumentKind = (
            "A_SHARE_STOCK"
            if self.contract.selection_path in stock_paths
            else "EXCHANGE_TRADED_FUND"
        )

    def replay(self, batches: tuple[ReplayBatch, ...]) -> ReplayRunResult:
        ordered = tuple(sorted(batches, key=lambda row: (row.decision_at, row.batch_id)))
        if tuple(batches) != ordered:
            raise ValueError("replay batches must already be in causal order")
        previous_at = self.started_at
        for batch in ordered:
            if batch.decision_at < previous_at:
                raise ValueError("replay batches overlap or move backwards")
            previous_at = batch.valuation_at

        state = _ReplayState(
            cash=self.initial_cash,
            positions={},
            last_marks={},
            high_water_equity=self.initial_cash,
            intents=[],
            orders=[],
            rejections=[],
            corporate_actions=[],
            mandatory_share_actions=[],
            applied_corporate_action_ids=set(),
            equity_curve=[
                ReplayEquityPoint(
                    observed_at=self.started_at,
                    cash=self.initial_cash,
                    market_value=_ZERO,
                    equity=self.initial_cash,
                    committed_exposure=_ZERO,
                    restore_cash_reserve=_ZERO,
                    occupied_slots=0,
                    complete=True,
                )
            ],
            closed_cycles=[],
            unresolved_reasons=[],
            resolved_persistent_intent_ids=set(),
            suppressed_persistent_event_counts={},
        )
        for batch in ordered:
            self._process_batch(batch, state)
        return self._result(state)

    def _process_batch(self, batch: ReplayBatch, state: _ReplayState) -> None:
        self._apply_mandatory_share_actions(batch, state)
        self._apply_cash_distributions(batch, state)
        self._apply_marks(batch.decision_marks, state)
        prepared: list[
            tuple[ReplayDecisionEvent, DecisionIntent, tuple[str, ...], int]
        ] = []
        for event in batch.events:
            persistent_id = event.persistent_intent_id
            if (
                persistent_id is not None
                and persistent_id in state.resolved_persistent_intent_ids
            ):
                state.suppressed_persistent_event_counts[persistent_id] = (
                    state.suppressed_persistent_event_counts.get(persistent_id, 0)
                    + 1
                )
                continue
            account_reasons: list[str] = []
            position = state.positions.get(event.facts.symbol)
            if position is not None:
                if event.execution_status.effective_session < position.ledger.session:
                    account_reasons.append("UNRESOLVED_LEDGER_SESSION_REGRESSION")
                else:
                    position.ledger = position.ledger.roll_session(
                        event.execution_status.effective_session
                    )
            if event.account_position_source == "EVENT_SOURCED_REPLAY":
                event = replace(
                    event,
                    broker_position_quantity=(
                        0 if position is None else position.ledger.q_current
                    ),
                    account_position_source="EXTERNAL_SNAPSHOT",
                )
            if event.sellable_quantity_source == "EVENT_SOURCED_REPLAY":
                event = replace(
                    event,
                    execution_status=replace(
                        event.execution_status,
                        sellable_quantity=(
                            0
                            if position is None
                            else _local_sellable_quantity(position.ledger)
                        ),
                    ),
                    sellable_quantity_source="EXTERNAL_SNAPSHOT",
                )
            if position is None:
                facts = replace(
                    event.facts,
                    cycle_ledger=None,
                    strategic_state=(
                        event.facts.strategic_state
                        if event.facts.strategic_state
                        in {
                            "S_FLAT",
                            "S_WAIT_CENTER",
                            "S_WAIT_DEPARTURE",
                            "S_WAIT_RETURN",
                            "S_ENTRY_READY",
                        }
                        else "S_FLAT"
                    ),
                )
                if event.broker_position_quantity != 0:
                    account_reasons.append(
                        "UNRESOLVED_BROKER_POSITION_WITHOUT_LOCAL_CYCLE"
                    )
            else:
                ledger = position.ledger
                facts = replace(
                    event.facts,
                    cycle_ledger=ledger,
                    strategic_state=ledger.strategic_state,
                    strategic=(
                        replace(
                            event.facts.strategic,
                            existing_persistent_exit=True,
                        )
                        if ledger.strategic_state == "S_EXIT_WORKING"
                        else replace(
                            event.facts.strategic,
                            l0_upmove_divergence=True,
                        )
                        if ledger.strategic_state == "S_REDUCE_WORKING"
                        else event.facts.strategic
                    ),
                )
                if event.broker_position_quantity != ledger.q_current:
                    account_reasons.append("UNRESOLVED_BROKER_POSITION_MISMATCH")

            intent = self.decision_core.decide(facts)
            fact_reasons = tuple(
                dict.fromkeys(
                    account_reasons
                    + list(self._fact_gate(event, intent, position))
                )
            )
            state.intents.append(
                ReplayIntentRecord(
                    batch_id=batch.batch_id,
                    event_id=event.event_id,
                    intent=intent,
                    fact_gate_reason_codes=fact_reasons,
                    persistent_intent_id=persistent_id,
                    requested_quantity=intent.quantity,
                )
            )
            intent_record_index = len(state.intents) - 1
            # A stale exit must never attach itself to a future position.  A
            # flat event-sourced ledger, or a held cycle whose tactical slice
            # is structurally zero after lot rounding, proves that this source
            # signal has nothing to execute.  Record the first causal decision
            # and retire only its later retries.  Fact-gate failures remain
            # active because their account evidence is not trustworthy yet.
            if persistent_id is not None and not fact_reasons:
                resolved_noop = position is None and intent.quantity == 0
                resolved_noop = resolved_noop or (
                    position is not None
                    and position.ledger.tactical_held_qty == 0
                    and intent.action == "WAIT"
                    and "NO_SELLABLE_TACTICAL_INVENTORY" in intent.reason_codes
                )
                if resolved_noop:
                    state.resolved_persistent_intent_ids.add(persistent_id)
            if fact_reasons:
                self._reject(
                    state,
                    batch=batch,
                    event=event,
                    stage="FACT_GATE",
                    action=intent.action,
                    reasons=fact_reasons,
                )
            prepared.append((event, intent, fact_reasons, intent_record_index))

        prepared.sort(
            key=lambda row: (
                row[1].priority,
                candidate_decision_sort_key(
                    row[0].facts.candidate
                    if row[1].action == "ENTRY_INTENT"
                    else None
                ),
                row[1].confirmation_time,
                row[1].symbol,
                row[0].event_id,
            )
        )
        allocation = _BatchAllocationState(
            cash_remaining=state.cash,
            restore_cash_remaining={
                symbol: position.ledger.restore_cash_reserve
                for symbol, position in state.positions.items()
            },
            occupied_slots={
                position.slot_number for position in state.positions.values()
            },
            reserved_entry_symbols=set(),
            reserved_entry_notional=_ZERO,
        )
        scheduled: list[
            tuple[
                ReplayDecisionEvent,
                DecisionIntent,
                int | None,
            ]
        ] = []
        for event, intent, fact_reasons, intent_record_index in prepared:
            if (
                intent.action not in _ORDER_ACTIONS
                or intent.quantity == 0
                or fact_reasons
            ):
                continue
            schedule = self._schedule_order_at_decision(
                event,
                intent,
                state,
                allocation,
            )
            state.intents[intent_record_index] = replace(
                state.intents[intent_record_index],
                scheduled_quantity=(
                    0
                    if schedule.scheduled_intent is None
                    else schedule.scheduled_intent.quantity
                ),
                reserved_cash_at_decision=schedule.reserved_cash,
                reserved_entry_notional_at_decision=(
                    schedule.reserved_entry_notional
                ),
                reserved_slot_number=schedule.reserved_slot_number,
                scheduler_capacity_rows=schedule.capacity_rows,
                scheduler_reason_codes=schedule.scheduler_reason_codes,
            )
            if schedule.rejection_reason_codes:
                self._reject(
                    state,
                    batch=batch,
                    event=event,
                    stage="PORTFOLIO_GATE",
                    action=intent.action,
                    reasons=schedule.rejection_reason_codes,
                )
                continue
            scheduled_intent = schedule.scheduled_intent
            if scheduled_intent is None:
                raise RuntimeError("accepted decision-time schedule lacks an intent")
            if scheduled_intent.action in _STRATEGIC_SELL_ACTIONS:
                # Strategic exits cancel restore obligations at intent time.
                # This release is causal even though the later sell fill is
                # not; slots, sale proceeds and exposure remain reserved from
                # the immutable pre-fill account snapshot.
                self._apply_strategic_transition(scheduled_intent, state)
                position = state.positions.get(scheduled_intent.symbol)
                allocation.restore_cash_remaining[scheduled_intent.symbol] = (
                    _ZERO
                    if position is None
                    else position.ledger.restore_cash_reserve
                )
            scheduled.append(
                (
                    event,
                    scheduled_intent,
                    schedule.reserved_slot_number,
                )
            )

        # Only after every order has been scheduled from the same decision-time
        # account snapshot may future minute bars be matched.  A fill, no-fill,
        # or sale proceeds can therefore never change another order from this
        # batch.
        for event, intent, reserved_slot_number in scheduled:
            order = _prepare_order(
                intent,
                event,
                parameters=self.parameters,
                instrument_kind=self.instrument_kind,
            )
            match = match_historical_minute_bars(
                order,
                bars=event.bars,
                status=event.execution_status,
                fee_model=self.fee_model,
                fee_session=event.execution_status.effective_session,
                strategy_parameters=self.parameters,
                proxy_parameters=self.proxy_parameters,
            )
            state.orders.append(
                ReplayOrderRecord(
                    batch_id=batch.batch_id,
                    event_id=event.event_id,
                    intent_action=intent.action,
                    order=order,
                    match=match,
                )
            )
            if match.rejection_and_unfilled_reasons:
                self._reject(
                    state,
                    batch=batch,
                    event=event,
                    stage="MATCHER",
                    action=intent.action,
                    reasons=match.rejection_and_unfilled_reasons,
                )
            if match.fills:
                self._apply_fills(
                    event,
                    intent,
                    match,
                    state,
                    reserved_slot_number=reserved_slot_number,
                )
            if (
                event.persistent_intent_id is not None
                and match.remaining_quantity == 0
            ):
                # Full completion resolves the order lifecycle.  A tactical
                # buyback is a separate later signal; keeping the completed
                # sell source alive would only emit zero-inventory WAITs.
                state.resolved_persistent_intent_ids.add(
                    event.persistent_intent_id
                )

        bar_marks = tuple(
            ReplayPriceFact(
                symbol=event.facts.symbol,
                available_at=bar.closed_at,
                raw_close=bar.raw_close,
                source_id=bar.source_id,
                complete=bar.complete,
            )
            for event in batch.events
            for bar in (
                max(
                    (
                        value
                        for value in event.bars
                        if value.complete and value.closed_at <= batch.valuation_at
                    ),
                    key=lambda value: (value.closed_at, value.sequence),
                    default=None,
                ),
            )
            if bar is not None
        )
        self._apply_marks(bar_marks, state)
        self._apply_marks(batch.valuation_marks, state)
        self._append_equity(batch, state)

    def _fact_gate(
        self,
        event: ReplayDecisionEvent,
        intent: DecisionIntent,
        position: _PositionState | None,
    ) -> tuple[str, ...]:
        bindings = event.bindings
        reasons: list[str] = []
        if not bindings.all_required_facts_resolved:
            reasons.extend(
                code
                if code.startswith("UNRESOLVED")
                else f"UNRESOLVED_{code}"
                for code in bindings.unresolved_reason_codes
            )
        if not event.facts.all_structure_inputs_completed:
            reasons.append("UNRESOLVED_INCOMPLETE_OR_PROVISIONAL_STRUCTURE")
        if (
            bindings.timeframe_override_parameter_set_id
            != self.contract.timeframe_override_parameter_set_id
        ):
            reasons.append("UNRESOLVED_TIMEFRAME_OVERRIDE_BINDING")
        if event.facts.structure_snapshot_id not in bindings.frozen_structure_fact_ids:
            reasons.append("UNRESOLVED_FROZEN_STRUCTURE_SNAPSHOT_PROVENANCE")
        if intent.action not in _ORDER_ACTIONS:
            return tuple(dict.fromkeys(reasons))
        if not bindings.frozen_structure_fact_ids:
            reasons.append("UNRESOLVED_FROZEN_SIGNAL_FACTS")
        if intent.action in _RISK_FACT_ACTIONS and not bindings.risk_fact_ids:
            reasons.append("UNRESOLVED_POINT_IN_TIME_RISK_FACTS")
        if not event.execution_status.point_in_time_state_complete:
            reasons.append("UNRESOLVED_POINT_IN_TIME_EXECUTION_STATE")
        if not event.execution_status.corporate_action_state_complete:
            reasons.append("UNRESOLVED_CORPORATE_ACTION_STATE")
        if event.execution_status.known_at > event.broker_confirmed_at:
            reasons.append("UNRESOLVED_EXECUTION_STATUS_NOT_YET_KNOWN")
        if not event.execution_status.fee_schedule_id:
            reasons.append("UNRESOLVED_EFFECTIVE_FEE_SCHEDULE")
        if intent.persistence == "OPTIONAL" and event.expires_at is None:
            reasons.append("UNRESOLVED_OPTIONAL_ORDER_EXPIRY")
        if intent.persistence == "PERSISTENT_EXIT" and event.expires_at is not None:
            reasons.append("UNRESOLVED_PERSISTENT_EXIT_MUST_NOT_EXPIRE")
        # A persistent exit is replayed on every later executable opportunity
        # until the position is gone.  Once the event-sourced account and the
        # broker binding both prove that quantity is zero, the same signal is
        # a resolved idempotent no-op, not an unknown order boundary.  Without
        # this distinction every retry after a completed exit (and every exit
        # following an unfilled entry) polluted the whole ledger with
        # ``UNRESOLVED_*`` even though there was provably nothing to sell.
        verified_flat_persistent_exit = (
            position is None
            and event.broker_position_quantity == 0
            and intent.persistence == "PERSISTENT_EXIT"
            and intent.quantity == 0
            and intent.target_position_quantity == 0
        )
        if (
            intent.quantity <= 0 or intent.price_cap_or_floor is None
        ) and not verified_flat_persistent_exit:
            reasons.append("UNRESOLVED_ORDER_QUANTITY_OR_PRICE_BOUNDARY")

        if intent.action == "ENTRY_INTENT":
            candidate = event.facts.candidate
            chain = bindings.aligned_entry_chain
            if not bindings.selection_fact_ids:
                reasons.append("UNRESOLVED_POINT_IN_TIME_SELECTION_FACTS")
            if (
                event.facts.selection_snapshot_id is None
                or event.facts.selection_snapshot_id
                not in bindings.selection_fact_ids
            ):
                reasons.append("UNRESOLVED_SELECTION_SNAPSHOT_PROVENANCE")
            if self.contract.selection_path == "INDIVIDUAL_THREE_PROGRAM":
                required_candidate_gates = INDIVIDUAL_REQUIRED_CANDIDATE_GATES
            elif self.contract.selection_path == RECENT_YEAR_SELECTION_PATH:
                required_candidate_gates = (
                    SECTOR_TECHNICAL_REQUIRED_CANDIDATE_GATES
                )
            else:
                required_candidate_gates = ETF_REQUIRED_CANDIDATE_GATES
            if (
                candidate is None
                or not candidate.accepted
                or candidate.symbol != event.facts.symbol
                or candidate.selection_path != self.contract.selection_path
                or candidate.parameter_set_id != self.parameters.parameter_set_id
            ):
                reasons.append("UNRESOLVED_PARENT_CANDIDATE_BINDING")
            elif (
                len({check.gate for check in candidate.checks})
                != len(candidate.checks)
                or not required_candidate_gates.issubset(
                    {check.gate for check in candidate.checks if check.passed}
                )
            ):
                reasons.append("UNRESOLVED_INCOMPLETE_CANDIDATE_GATE_TRACE")
            if (
                candidate is None
                or candidate.confirmation_time is None
                or candidate.confirmation_time > event.facts.decision_time
            ):
                reasons.append("UNRESOLVED_CANDIDATE_CONFIRMATION_TIME")
            if (
                bindings.alignment_contract_id
                != self.contract.effective_alignment_contract_id
                or bindings.alignment_parameter_set_id
                != self.contract.effective_alignment_parameter_set_id
            ):
                reasons.append("UNRESOLVED_EFFECTIVE_ALIGNMENT_BINDING")
            if chain is None:
                reasons.append("UNRESOLVED_ALIGNED_30M_5M_1M_ENTRY_CHAIN")
            elif isinstance(chain, AlignedEntryChain):
                required = {
                    chain.l0_point_id,
                    chain.l0_center_id,
                    chain.l1_departure_evidence_id,
                    chain.l1_return_evidence_id,
                    chain.l2_locator_point_id,
                    event.facts.structure_snapshot_id,
                }
                if not required.issubset(bindings.frozen_structure_fact_ids):
                    reasons.append("UNRESOLVED_ALIGNED_CHAIN_FACT_PROVENANCE")
                if chain.decision_at > event.facts.decision_time:
                    reasons.append("UNRESOLVED_FUTURE_ALIGNMENT_EVIDENCE")
                if event.facts.confirmation_time < chain.decision_at:
                    reasons.append("UNRESOLVED_CONFIRMATION_PRECEDES_ALIGNMENT")
                if event.facts.price_cap_or_floor != chain.l2_confirmation_bar_high:
                    reasons.append("UNRESOLVED_ENTRY_BOUNDARY_DIFFERS_FROM_L2_BAR")
            elif isinstance(chain, DirectRecursiveEntryChain):
                required = {
                    chain.l0_point_id,
                    chain.l0_center_id,
                    chain.l1_departure_unit_id,
                    chain.l1_return_unit_id,
                    chain.l2_locator_point_id,
                    event.facts.structure_snapshot_id,
                    *chain.provenance_unit_ids,
                    *chain.nine_segment_evidence_ids,
                }
                if not required.issubset(bindings.frozen_structure_fact_ids):
                    reasons.append("UNRESOLVED_DIRECT_CHAIN_FACT_PROVENANCE")
                if chain.decision_at > event.facts.decision_time:
                    reasons.append("UNRESOLVED_FUTURE_ALIGNMENT_EVIDENCE")
                if event.facts.confirmation_time < chain.decision_at:
                    reasons.append("UNRESOLVED_CONFIRMATION_PRECEDES_ALIGNMENT")
                if (
                    event.facts.price_cap_or_floor
                    != chain.l2_confirmation_bar_high
                ):
                    reasons.append("UNRESOLVED_ENTRY_BOUNDARY_DIFFERS_FROM_L2_BAR")
            elif isinstance(chain, ApproximateChanlunEntryChain):
                required = {
                    chain.strategic_point_id,
                    chain.strategic_center_id,
                    chain.strategic_anchor_unit_id,
                    chain.locator_point_id,
                    chain.locator_anchor_unit_id,
                    chain.strict_structure_snapshot_id,
                    chain.technical_parameter_set_id,
                    *chain.provenance_fact_ids,
                }
                if not required.issubset(bindings.frozen_structure_fact_ids):
                    reasons.append("UNRESOLVED_APPROXIMATE_CHAIN_FACT_PROVENANCE")
                if chain.decision_at > event.facts.decision_time:
                    reasons.append("UNRESOLVED_FUTURE_APPROXIMATION_EVIDENCE")
                if event.facts.confirmation_time < chain.decision_at:
                    reasons.append("UNRESOLVED_CONFIRMATION_PRECEDES_APPROXIMATION")
                if event.facts.price_cap_or_floor != chain.confirmation_bar_high:
                    reasons.append(
                        "UNRESOLVED_ENTRY_BOUNDARY_DIFFERS_FROM_APPROXIMATE_LOCATOR_BAR"
                    )
                if not isinstance(
                    self.contract,
                    ResearchSectorTechnicalApproxReplayContract,
                ) or (
                    chain.technical_parameter_set_id
                    != self.contract.technical_approximation_parameter_set_id
                ):
                    reasons.append("UNRESOLVED_TECHNICAL_APPROXIMATION_BINDING")
            else:  # pragma: no cover - ReplayFactBindings is a closed union
                reasons.append("UNRESOLVED_UNSUPPORTED_ALIGNMENT_CHAIN")

        if (
            intent.action != "ENTRY_INTENT"
            and position is None
            and not verified_flat_persistent_exit
        ):
            reasons.append("UNRESOLVED_ORDER_WITHOUT_LOCAL_CYCLE")
        if intent.action not in _BUY_ACTIONS and position is not None:
            local_sellable = _local_sellable_quantity(position.ledger)
            if event.execution_status.sellable_quantity > local_sellable:
                reasons.append("UNRESOLVED_BROKER_SELLABLE_EXCEEDS_LOCAL_T1")
        return tuple(dict.fromkeys(reasons))

    def _schedule_order_at_decision(
        self,
        event: ReplayDecisionEvent,
        intent: DecisionIntent,
        state: _ReplayState,
        allocation: _BatchAllocationState,
    ) -> _OrderSchedule:
        """Reserve quantity, cash, exposure and slots before any bar match.

        The decision core states what it wants.  This scheduler applies the
        engineering account constraints from specification sections 3.2/3.3
        and may only shrink a buy quantity to a complete exchange increment.
        It never enlarges or otherwise changes the core intent.
        """

        boundary = intent.price_cap_or_floor
        if boundary is None:
            return _OrderSchedule(
                scheduled_intent=None,
                rejection_reason_codes=("UNRESOLVED_ORDER_PRICE_BOUNDARY",),
                scheduler_reason_codes=("DECISION_TIME_ORDER_REJECTED",),
            )
        increment = (
            event.execution_status.buy_quantity_increment
            if intent.action in _BUY_ACTIONS
            else event.execution_status.sell_quantity_increment
        )
        if intent.quantity % increment:
            return _OrderSchedule(
                scheduled_intent=None,
                rejection_reason_codes=("ORDER_QUANTITY_INCREMENT_MISMATCH",),
                scheduler_reason_codes=("DECISION_TIME_ORDER_REJECTED",),
            )

        # Sells neither release cash/exposure nor expose a slot until a later
        # broker fill.  They therefore need only their static order checks in
        # this decision-time allocation phase.
        if intent.action not in _BUY_ACTIONS:
            return _OrderSchedule(
                scheduled_intent=intent,
                scheduler_reason_codes=("DECISION_TIME_ORDER_SCHEDULED",),
            )

        session = event.execution_status.effective_session
        try:
            self.fee_model.rate_at(session)
        except LookupError:
            return _OrderSchedule(
                scheduled_intent=None,
                rejection_reason_codes=("UNRESOLVED_EFFECTIVE_FEE_RATE",),
                scheduler_reason_codes=("DECISION_TIME_ORDER_REJECTED",),
            )
        bound_buy_cost = self.fee_model.bound_buy_cost(
            instrument_kind=self.instrument_kind,
            session=session,
        )

        if intent.action in _TACTICAL_BUY_ACTIONS:
            other_restore_cash = sum(
                (
                    reserve
                    for symbol, reserve in allocation.restore_cash_remaining.items()
                    if symbol != intent.symbol
                ),
                _ZERO,
            )
            available_cash = max(
                _ZERO,
                allocation.cash_remaining - other_restore_cash,
            )
            scheduled_quantity = intent.quantity
            while scheduled_quantity > 0:
                required = (
                    Decimal(scheduled_quantity) * boundary
                    + bound_buy_cost(scheduled_quantity, boundary)
                )
                if required <= available_cash:
                    break
                scheduled_quantity -= increment
            capacities = (
                ("decision_quantity_cap", intent.quantity),
                ("cash_with_bound_cost_cap", scheduled_quantity),
            )
            if scheduled_quantity <= 0:
                return _OrderSchedule(
                    scheduled_intent=None,
                    rejection_reason_codes=("BUYBACK_CASH_INSUFFICIENT",),
                    scheduler_reason_codes=("DECISION_TIME_ORDER_REJECTED",),
                    capacity_rows=capacities,
                )
            reserved_cash = (
                Decimal(scheduled_quantity) * boundary
                + bound_buy_cost(scheduled_quantity, boundary)
            )
            allocation.cash_remaining -= reserved_cash
            own_restore = allocation.restore_cash_remaining.get(
                intent.symbol,
                _ZERO,
            )
            allocation.restore_cash_remaining[intent.symbol] = max(
                _ZERO,
                own_restore - reserved_cash,
            )
            scheduler_reasons = ["DECISION_TIME_ORDER_SCHEDULED"]
            if scheduled_quantity < intent.quantity:
                scheduler_reasons.append("SCHEDULED_QUANTITY_REDUCED")
            return _OrderSchedule(
                scheduled_intent=replace(intent, quantity=scheduled_quantity),
                scheduler_reason_codes=tuple(scheduler_reasons),
                reserved_cash=reserved_cash,
                capacity_rows=capacities,
            )

        if intent.symbol in state.positions or (
            intent.symbol in allocation.reserved_entry_symbols
        ):
            return _OrderSchedule(
                scheduled_intent=None,
                rejection_reason_codes=("DUPLICATE_SYMBOL_CYCLE",),
                scheduler_reason_codes=("DECISION_TIME_ORDER_REJECTED",),
            )

        current_equity, complete = self._current_equity(state)
        if not complete:
            return _OrderSchedule(
                scheduled_intent=None,
                rejection_reason_codes=(
                    "UNRESOLVED_DECISION_MARK_FOR_EXISTING_POSITION",
                ),
                scheduler_reason_codes=("DECISION_TIME_ORDER_REJECTED",),
            )
        if current_equity <= 0:
            return _OrderSchedule(
                scheduled_intent=None,
                rejection_reason_codes=("NON_POSITIVE_ACCOUNT_EQUITY",),
                scheduler_reason_codes=("DECISION_TIME_ORDER_REJECTED",),
            )

        current_market_value = _ZERO
        restore_exposure = _ZERO
        for symbol, position in state.positions.items():
            mark = state.last_marks.get(symbol)
            if mark is None or not mark.complete:
                return _OrderSchedule(
                    scheduled_intent=None,
                    rejection_reason_codes=(
                        "UNRESOLVED_COMMITTED_EXPOSURE_MARK",
                    ),
                    scheduler_reason_codes=("DECISION_TIME_ORDER_REJECTED",),
                )
            current_market_value += (
                Decimal(position.ledger.q_current) * mark.raw_close
            )
            restore_exposure += (
                Decimal(position.ledger.pending_restore_qty) * mark.raw_close
            )
        drawdown = (
            _ZERO
            if state.high_water_equity <= 0
            else max(
                _ZERO,
                (state.high_water_equity - current_equity)
                / state.high_water_equity,
            )
        )
        sizing = size_strategic_entry(
            EntrySizingInput(
                account_equity_at_decision=current_equity,
                broker_available_cash=allocation.cash_remaining,
                current_gross_market_value=current_market_value,
                restore_exposure_commitment=restore_exposure,
                restore_cash_reserve=sum(
                    allocation.restore_cash_remaining.values(),
                    _ZERO,
                ),
                reserved_strategic_entry_notional=(
                    allocation.reserved_entry_notional
                ),
                # Prior same-batch buy reservations are already subtracted
                # from ``cash_remaining``; feeding them again would double
                # count protected cash.
                active_buy_worst_cash_required=_ZERO,
                active_buy_restore_cash_allocated=_ZERO,
                buy_price_cap=boundary,
                q_liquidity_cap=intent.quantity,
                buy_quantity_increment=increment,
                occupied_slots=len(allocation.occupied_slots),
                drawdown=drawdown,
            ),
            parameters=self.parameters,
            bound_buy_cost=bound_buy_cost,
        )
        scheduled_quantity = sizing.q_plan
        if scheduled_quantity <= 0:
            reasons: list[str] = []
            if len(allocation.occupied_slots) >= self.contract.slot_count:
                reasons.append("FIVE_SLOT_CAP_REACHED")
            if drawdown >= self.parameters.entry_drawdown_halt:
                reasons.append("ENTRY_DRAWDOWN_HALT")
            one_increment_cash = (
                Decimal(increment) * boundary
                + bound_buy_cost(increment, boundary)
            )
            if sizing.entry_cash_available < one_increment_cash:
                reasons.append("ENTRY_GENERAL_CASH_RESERVATION_INSUFFICIENT")
            if sizing.u_remain < Decimal(increment) * boundary:
                reasons.append("ACCOUNT_EXPOSURE_CAP_EXCEEDED")
            if not reasons:
                reasons.append("LESS_THAN_ONE_BUY_INCREMENT_AFTER_ALL_CAPS")
            return _OrderSchedule(
                scheduled_intent=None,
                rejection_reason_codes=tuple(dict.fromkeys(reasons)),
                scheduler_reason_codes=(
                    "DECISION_TIME_ORDER_REJECTED",
                    *sizing.reason_codes,
                ),
                capacity_rows=sizing.capacity_rows,
            )

        free_slots = set(range(1, self.contract.slot_count + 1)) - (
            allocation.occupied_slots
        )
        if not free_slots:
            return _OrderSchedule(
                scheduled_intent=None,
                rejection_reason_codes=("FIVE_SLOT_CAP_REACHED",),
                scheduler_reason_codes=("DECISION_TIME_ORDER_REJECTED",),
                capacity_rows=sizing.capacity_rows,
            )
        reserved_slot = min(free_slots)
        reserved_notional = Decimal(scheduled_quantity) * boundary
        reserved_cash = reserved_notional + bound_buy_cost(
            scheduled_quantity,
            boundary,
        )
        allocation.cash_remaining -= reserved_cash
        allocation.reserved_entry_notional += reserved_notional
        allocation.occupied_slots.add(reserved_slot)
        allocation.reserved_entry_symbols.add(intent.symbol)
        scheduler_reasons = ["DECISION_TIME_ORDER_SCHEDULED"]
        scheduler_reasons.extend(sizing.reason_codes)
        if scheduled_quantity < intent.quantity:
            scheduler_reasons.append("SCHEDULED_QUANTITY_REDUCED")
        return _OrderSchedule(
            scheduled_intent=replace(intent, quantity=scheduled_quantity),
            scheduler_reason_codes=tuple(dict.fromkeys(scheduler_reasons)),
            reserved_cash=reserved_cash,
            reserved_entry_notional=reserved_notional,
            reserved_slot_number=reserved_slot,
            capacity_rows=sizing.capacity_rows,
        )

    def _apply_strategic_transition(
        self,
        intent: DecisionIntent,
        state: _ReplayState,
    ) -> None:
        position = state.positions.get(intent.symbol)
        if position is None or intent.action not in _STRATEGIC_SELL_ACTIONS:
            return
        target = (
            "S_REDUCE_WORKING"
            if intent.action == "STRATEGIC_REDUCE_INTENT"
            else "S_EXIT_WORKING"
        )
        position.ledger = position.ledger.terminate_restore_obligations(
            target_state=target
        )

    def _apply_fills(
        self,
        event: ReplayDecisionEvent,
        intent: DecisionIntent,
        match: BarProxyMatchResult,
        state: _ReplayState,
        *,
        reserved_slot_number: int | None,
    ) -> None:
        notionals = tuple(
            Decimal(fill.quantity) * fill.execution_price for fill in match.fills
        )
        gross = sum(notionals, _ZERO)
        fee_allocations = _allocate_amount(
            match.total_fees,
            notionals,
            self.fee_model.currency_quantum,
        )
        if intent.action in _BUY_ACTIONS:
            state.cash -= gross + match.total_fees
        else:
            state.cash += gross - match.total_fees
        if state.cash < 0:
            raise RuntimeError("strict strategy replay cash became negative")

        if intent.action == "ENTRY_INTENT":
            if intent.symbol in state.positions:
                raise RuntimeError("entry fill reached an occupied symbol")
            if reserved_slot_number is None:
                raise RuntimeError("entry fill lacks its decision-time slot")
            if reserved_slot_number in {
                value.slot_number for value in state.positions.values()
            }:
                raise RuntimeError("decision-time entry slot became occupied")
            slot = reserved_slot_number
            first_fill = min(match.fills, key=lambda row: row.exchange_time)
            cycle_id = f"cycle:{intent.symbol}:{first_fill.execution_id}"
            ledger = CycleLedger.from_entry_fill(
                cycle_id=cycle_id,
                session=event.execution_status.effective_session,
                fill_qty=match.filled_quantity,
                buy_quantity_increment=event.execution_status.buy_quantity_increment,
                sell_quantity_increment=event.execution_status.sell_quantity_increment,
                t_plus_days=self.contract.settlement_t_plus_days,
                tactical_ratio=self.parameters.tactical_ratio,
            )
            entry_cash = gross + match.total_fees
            state.positions[intent.symbol] = _PositionState(
                symbol=intent.symbol,
                slot_number=slot,
                ledger=ledger,
                opened_at=first_fill.exchange_time,
                cumulative_cash_flow=-entry_cash,
                cumulative_fees=match.total_fees,
                turnover_notional=gross,
                entry_cash=entry_cash,
            )
            return

        position = state.positions[intent.symbol]
        position.cumulative_fees += match.total_fees
        position.turnover_notional += gross
        position.cumulative_cash_flow += (
            -(gross + match.total_fees)
            if intent.action in _BUY_ACTIONS
            else gross - match.total_fees
        )
        if intent.action in _TACTICAL_SELL_ACTIONS:
            position.ledger = _consume_tactical_lots(
                position.ledger,
                match=match,
                allow_existing_restore=(
                    intent.action == "TACTICAL_THIRD_SELL_EXIT_INTENT"
                ),
                currency_quantum=self.fee_model.currency_quantum,
            )
        elif intent.action in _TACTICAL_BUY_ACTIONS:
            ledger = position.ledger
            before = len(ledger.completed_tactical_cycle_sessions)
            for fill, fee in zip(match.fills, fee_allocations, strict=True):
                ledger, _realization = ledger.apply_tactical_buyback_fill(
                    quantity=fill.quantity,
                    execution_id=fill.execution_id,
                    exchange_time=fill.exchange_time,
                    buy_cash_and_cost=(
                        Decimal(fill.quantity) * fill.execution_price + fee
                    ),
                )
            position.ledger = ledger
            position.tactical_cycles_completed += (
                len(ledger.completed_tactical_cycle_sessions) - before
            )
        elif intent.action in _STRATEGIC_SELL_ACTIONS:
            ledger = position.ledger
            for fill in match.fills:
                ledger = ledger.apply_strategic_sell_fill(quantity=fill.quantity)
            position.ledger = ledger
            if ledger.strategic_state == "S_CLOSED":
                self._close_position(position, match.fills[-1].exchange_time, state)
        else:
            raise RuntimeError(f"unsupported filled action: {intent.action}")

    def _close_position(
        self,
        position: _PositionState,
        closed_at: datetime,
        state: _ReplayState,
    ) -> None:
        net_return = (
            _ZERO
            if position.entry_cash == 0
            else position.cumulative_cash_flow / position.entry_cash
        )
        state.closed_cycles.append(
            ReplayClosedCycle(
                symbol=position.symbol,
                cycle_id=position.ledger.cycle_id,
                slot_number=position.slot_number,
                opened_at=position.opened_at,
                closed_at=closed_at,
                entry_cash=position.entry_cash,
                net_pnl=position.cumulative_cash_flow,
                net_return=net_return,
                total_fees=position.cumulative_fees,
                turnover_notional=position.turnover_notional,
                tactical_cycles_completed=position.tactical_cycles_completed,
            )
        )
        del state.positions[position.symbol]

    @staticmethod
    def _apply_marks(
        marks: tuple[ReplayPriceFact, ...],
        state: _ReplayState,
    ) -> None:
        for mark in marks:
            current = state.last_marks.get(mark.symbol)
            if current is None or current.available_at <= mark.available_at:
                state.last_marks[mark.symbol] = mark

    @staticmethod
    def _apply_mandatory_share_actions(
        batch: ReplayBatch,
        state: _ReplayState,
    ) -> None:
        previous_at = state.equity_curve[-1].observed_at
        for action in sorted(
            batch.mandatory_share_actions,
            key=lambda row: (row.effective_at, row.symbol, row.action_id),
        ):
            if action.action_id in state.applied_corporate_action_ids:
                continue
            reasons: list[str] = []
            if not action.point_in_time_complete:
                reasons.append("UNRESOLVED_CORPORATE_ACTION_FACT_INCOMPLETE")
            if action.effective_at <= previous_at:
                reasons.append("UNRESOLVED_LATE_CORPORATE_ACTION_FACT")
            position = state.positions.get(action.symbol)
            before = 0 if position is None else position.ledger.q_current
            after = before
            applied = not reasons
            if applied and position is not None:
                after = int(
                    (Decimal(before) * action.share_multiplier).to_integral_value(
                        rounding=ROUND_DOWN
                    )
                )
                position.ledger = position.ledger.apply_mandatory_share_action(
                    share_multiplier=action.share_multiplier,
                    broker_position_qty=after,
                )
                after = position.ledger.q_current
            state.mandatory_share_actions.append(
                ReplayMandatoryShareActionRecord(
                    batch_id=batch.batch_id,
                    action_id=action.action_id,
                    symbol=action.symbol,
                    effective_at=action.effective_at,
                    share_multiplier=action.share_multiplier,
                    quantity_before=before,
                    quantity_after=after,
                    applied=applied,
                    reason_codes=tuple(reasons),
                )
            )
            if applied:
                state.applied_corporate_action_ids.add(action.action_id)
            state.unresolved_reasons.extend(reasons)

    @staticmethod
    def _apply_cash_distributions(
        batch: ReplayBatch,
        state: _ReplayState,
    ) -> None:
        previous_at = state.equity_curve[-1].observed_at
        for action in sorted(
            batch.cash_distributions,
            key=lambda row: (row.effective_at, row.symbol, row.action_id),
        ):
            if action.action_id in state.applied_corporate_action_ids:
                continue
            reasons: list[str] = []
            if not action.point_in_time_complete:
                reasons.append("UNRESOLVED_CORPORATE_ACTION_FACT_INCOMPLETE")
            if action.effective_at <= previous_at:
                reasons.append("UNRESOLVED_LATE_CORPORATE_ACTION_FACT")
            position = state.positions.get(action.symbol)
            held_quantity = 0 if position is None else position.ledger.q_current
            cash = Decimal(held_quantity) * action.cash_per_share
            foregone = _ZERO
            if position is not None:
                foregone = Decimal(position.ledger.pending_restore_qty) * (
                    action.cash_per_share
                )
            applied = not reasons
            if applied and position is not None:
                cohorts = tuple(
                    replace(
                        cohort,
                        foregone_cash_distribution=(
                            cohort.foregone_cash_distribution
                            + Decimal(cohort.remaining_qty) * action.cash_per_share
                        ),
                    )
                    if cohort.status in {"OPEN", "PARTIAL"}
                    else cohort
                    for cohort in position.ledger.restore_cohorts
                )
                position.ledger = replace(
                    position.ledger,
                    restore_cohorts=cohorts,
                )
                position.cumulative_cash_flow += cash
                state.cash += cash
            state.corporate_actions.append(
                ReplayCorporateActionRecord(
                    batch_id=batch.batch_id,
                    action_id=action.action_id,
                    symbol=action.symbol,
                    effective_at=action.effective_at,
                    held_quantity=held_quantity,
                    cash_distribution=cash if applied else _ZERO,
                    foregone_restore_distribution=(
                        foregone if applied else _ZERO
                    ),
                    applied=applied,
                    reason_codes=tuple(reasons),
                )
            )
            if applied:
                state.applied_corporate_action_ids.add(action.action_id)
            state.unresolved_reasons.extend(reasons)

    @staticmethod
    def _current_equity(state: _ReplayState) -> tuple[Decimal, bool]:
        market = _ZERO
        complete = True
        for symbol, position in state.positions.items():
            mark = state.last_marks.get(symbol)
            if mark is None or not mark.complete:
                complete = False
                continue
            market += Decimal(position.ledger.q_current) * mark.raw_close
        return state.cash + market, complete

    def _append_equity(self, batch: ReplayBatch, state: _ReplayState) -> None:
        reasons: list[str] = []
        market = _ZERO
        committed = _ZERO
        for symbol, position in sorted(state.positions.items()):
            mark = state.last_marks.get(symbol)
            if (
                mark is None
                or not mark.complete
                or mark.available_at <= state.equity_curve[-1].observed_at
            ):
                reasons.append(f"UNRESOLVED_VALUATION_MARK:{symbol}")
                if mark is None:
                    continue
            market += Decimal(position.ledger.q_current) * mark.raw_close
            committed += Decimal(
                position.ledger.q_current + position.ledger.pending_restore_qty
            ) * mark.raw_close
        equity = state.cash + market
        if equity <= 0:
            reasons.append("NON_POSITIVE_ACCOUNT_EQUITY")
        state.high_water_equity = max(state.high_water_equity, equity)
        restore = sum(
            (position.ledger.restore_cash_reserve for position in state.positions.values()),
            _ZERO,
        )
        state.equity_curve.append(
            ReplayEquityPoint(
                observed_at=batch.valuation_at,
                cash=state.cash,
                market_value=market,
                equity=equity,
                committed_exposure=committed,
                restore_cash_reserve=restore,
                occupied_slots=len(state.positions),
                complete=not reasons,
                reason_codes=tuple(reasons),
            )
        )
        state.unresolved_reasons.extend(
            reason for reason in reasons if reason.startswith("UNRESOLVED")
        )

    @staticmethod
    def _reject(
        state: _ReplayState,
        *,
        batch: ReplayBatch,
        event: ReplayDecisionEvent,
        stage: Literal["FACT_GATE", "PORTFOLIO_GATE", "MATCHER"],
        action: str,
        reasons: tuple[str, ...],
    ) -> None:
        state.rejections.append(
            ReplayRejection(
                batch_id=batch.batch_id,
                event_id=event.event_id,
                symbol=event.facts.symbol,
                stage=stage,
                intent_action=action,
                reason_codes=tuple(reasons),
            )
        )
        state.unresolved_reasons.extend(
            reason for reason in reasons if reason.startswith("UNRESOLVED")
        )

    def _result(self, state: _ReplayState) -> ReplayRunResult:
        def position_snapshot(value: _PositionState) -> ReplayPositionSnapshot:
            mark = state.last_marks.get(value.symbol)
            market_value = (
                None
                if mark is None
                else Decimal(value.ledger.q_current) * mark.raw_close
            )
            return ReplayPositionSnapshot(
                symbol=value.symbol,
                slot_number=value.slot_number,
                cycle_id=value.ledger.cycle_id,
                strategic_state=value.ledger.strategic_state,
                quantity=value.ledger.q_current,
                tactical_held_quantity=value.ledger.tactical_held_qty,
                pending_restore_quantity=value.ledger.pending_restore_qty,
                restore_cash_reserve=value.ledger.restore_cash_reserve,
                restore_cohort_ids=tuple(
                    cohort.restore_cohort_id
                    for cohort in value.ledger.restore_cohorts
                ),
                completed_tactical_cycle_sessions=(
                    value.ledger.completed_tactical_cycle_sessions
                ),
                opened_at=value.opened_at,
                cumulative_cash_flow=value.cumulative_cash_flow,
                cumulative_fees=value.cumulative_fees,
                entry_cash=value.entry_cash,
                turnover_notional=value.turnover_notional,
                tactical_cycles_completed=value.tactical_cycles_completed,
                last_price=None if mark is None else mark.raw_close,
                market_value=market_value,
                marked_at=None if mark is None else mark.available_at,
                mark_complete=False if mark is None else mark.complete,
            )

        positions = tuple(
            position_snapshot(value)
            for value in sorted(state.positions.values(), key=lambda row: row.slot_number)
        )
        metrics = self._metrics(state)
        return ReplayRunResult(
            contract=self.contract,
            initial_cash=self.initial_cash,
            final_cash=state.cash,
            intents=tuple(state.intents),
            orders=tuple(state.orders),
            rejections=tuple(state.rejections),
            corporate_actions=tuple(state.corporate_actions),
            mandatory_share_actions=tuple(state.mandatory_share_actions),
            equity_curve=tuple(state.equity_curve),
            positions=positions,
            closed_cycles=tuple(state.closed_cycles),
            metrics=metrics,
            unresolved_reason_codes=tuple(dict.fromkeys(state.unresolved_reasons)),
            resolved_persistent_intent_ids=tuple(
                sorted(state.resolved_persistent_intent_ids)
            ),
            suppressed_persistent_event_counts=tuple(
                sorted(state.suppressed_persistent_event_counts.items())
            ),
        )

    def _metrics(self, state: _ReplayState) -> ReplayMetrics:
        points = tuple(state.equity_curve)
        final_equity = points[-1].equity
        net_return = final_equity / self.initial_cash - _ONE
        peak = points[0].equity
        max_drawdown = _ZERO
        for point in points:
            peak = max(peak, point.equity)
            if peak > 0:
                max_drawdown = max(max_drawdown, (peak - point.equity) / peak)

        span_days = Decimal(
            str((points[-1].observed_at - points[0].observed_at).total_seconds())
        ) / Decimal("86400")
        annualized = None
        if span_days >= Decimal("365") and net_return > -_ONE:
            annualized = Decimal(
                str(
                    (1.0 + float(net_return))
                    ** float(Decimal("365") / span_days)
                    - 1.0
                )
            )

        daily_end: dict[date, ReplayEquityPoint] = {}
        for point in points:
            daily_end[point.observed_at.date()] = point
        previous = self.initial_cash
        daily_returns: list[float] = []
        for session in sorted(daily_end):
            ending = daily_end[session].equity
            daily_returns.append(float(ending / previous - _ONE))
            previous = ending
        sharpe = None
        if len(daily_returns) >= 2 and stdev(daily_returns) > 0:
            sharpe = Decimal(
                str(mean(daily_returns) / stdev(daily_returns) * sqrt(252))
            )

        cycles = tuple(state.closed_cycles)
        winners = tuple(value for value in cycles if value.net_pnl > 0)
        losers = tuple(value for value in cycles if value.net_pnl < 0)
        win_rate = (
            None
            if not cycles
            else Decimal(len(winners)) / Decimal(len(cycles))
        )
        average_win = (
            None
            if not winners
            else sum((value.net_pnl for value in winners), _ZERO)
            / Decimal(len(winners))
        )
        average_loss = (
            None
            if not losers
            else abs(sum((value.net_pnl for value in losers), _ZERO))
            / Decimal(len(losers))
        )
        payoff = (
            None
            if average_win is None or average_loss in (None, _ZERO)
            else average_win / average_loss
        )
        gross_profit = sum((value.net_pnl for value in winners), _ZERO)
        gross_loss = abs(sum((value.net_pnl for value in losers), _ZERO))
        profit_factor = None if gross_loss == 0 else gross_profit / gross_loss
        total_fees = sum((record.match.total_fees for record in state.orders), _ZERO)
        turnover_notional = sum(
            (
                Decimal(fill.quantity) * fill.execution_price
                for record in state.orders
                for fill in record.match.fills
            ),
            _ZERO,
        )
        tactical_count = sum(
            value.tactical_cycles_completed for value in state.closed_cycles
        ) + sum(
            value.tactical_cycles_completed for value in state.positions.values()
        )
        strategic_count = len(cycles)
        unresolved = tuple(dict.fromkeys(state.unresolved_reasons))
        warnings: list[str] = []
        if span_days < Decimal("365"):
            warnings.append("INSUFFICIENT_CALENDAR_SPAN_FOR_ANNUALIZATION")
        if strategic_count < 100:
            warnings.append("STRATEGIC_SAMPLE_BELOW_100")
        if tactical_count < 200:
            warnings.append("TACTICAL_SAMPLE_BELOW_200")
        if unresolved:
            warnings.append("UNRESOLVED_FACTS_OR_VALUATIONS_PRESENT")
        if any(point.equity <= 0 for point in points):
            warnings.append("NON_POSITIVE_EQUITY_PRESENT")
        ledger_valid = not unresolved and all(point.equity > 0 for point in points)
        fill_count = sum(len(value.match.fills) for value in state.orders)
        # 空回放的 0 收益/0 回撤只是账本恒等式，不是策略绩效。只有真正成交并
        # 形成过战略周期，收益字段才允许被引用。
        empty_replay = not state.orders or fill_count == 0
        performance_evaluable = (
            ledger_valid and not empty_replay and strategic_count > 0
        )
        if empty_replay:
            warnings.append("EMPTY_REPLAY_RETURNS_NOT_EVALUABLE")
        elif not performance_evaluable:
            warnings.append("PERFORMANCE_NOT_EVALUABLE")
        return ReplayMetrics(
            ledger_valid=ledger_valid,
            performance_evaluable=performance_evaluable,
            empty_replay=empty_replay,
            net_return=net_return,
            max_drawdown=max_drawdown,
            annualized_return=annualized,
            sharpe=sharpe,
            win_rate=win_rate,
            payoff_ratio=payoff,
            profit_factor=profit_factor,
            turnover=turnover_notional / self.initial_cash,
            total_fees=total_fees,
            strategic_cycle_count=strategic_count,
            tactical_cycle_count=tactical_count,
            open_cycle_count=len(state.positions),
            order_count=len(state.orders),
            fill_count=fill_count,
            rejection_count=len(state.rejections),
            strategic_sample_insufficient=strategic_count < 100,
            tactical_sample_insufficient=tactical_count < 200,
            warnings=tuple(warnings),
        )


__all__ = [
    "ReplayBatch",
    "ReplayCashDistributionFact",
    "ReplayClosedCycle",
    "ReplayCorporateActionRecord",
    "ReplayMandatoryShareActionFact",
    "ReplayMandatoryShareActionRecord",
    "ReplayDecisionEvent",
    "ReplayEquityPoint",
    "ReplayFactBindings",
    "ReplayIntentRecord",
    "ReplayMetrics",
    "ReplayOrderRecord",
    "ReplayPositionSnapshot",
    "ReplayPriceFact",
    "ReplayRejection",
    "ReplayRunResult",
    "ResearchIndividualDirectReplayContract",
    "ResearchSectorTechnicalApproxReplayContract",
    "ResearchSectorTechnicalDirectReplayContract",
    "StrictMultiSymbolReplayEngine",
    "StrictDirectReplayContract",
    "StrictReplayContract",
    "ETF_REQUIRED_CANDIDATE_GATES",
    "INDIVIDUAL_REQUIRED_CANDIDATE_GATES",
    "SECTOR_TECHNICAL_REQUIRED_CANDIDATE_GATES",
    "research_individual_direct_replay_contract",
    "research_sector_technical_approx_replay_contract",
    "research_sector_technical_direct_replay_contract",
    "strict_direct_replay_contract",
    "strict_replay_contract",
]
