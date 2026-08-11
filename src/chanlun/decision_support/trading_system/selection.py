from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal, ROUND_DOWN
from typing import Literal

from chanlun.decision_support.fingerprints import normalize_datetime
from chanlun.decision_support.trading_system.parameters import (
    STRICT_STROKE_MODE,
    SelectionPath,
    StrategyParameters,
)
from chanlun.decision_support.trading_system.sector_trigger import (
    SectorTriggerSnapshot,
)


IndustryOpportunity = Literal["PASS", "REJECT", "UNRESOLVED", "NOT_APPLICABLE"]
FundamentalRole = Literal[
    "LEADER",
    "GROWTH_CHALLENGER",
    "REJECT",
    "UNRESOLVED",
    "ETF_PROXY",
]
RelativeValue = Literal[
    "UNDERVALUED",
    "FAIR",
    "OVERVALUED",
    "UNRESOLVED",
    "ETF_PROXY",
]
RiskState = Literal[
    "NONE",
    "FORMED",
    "FORMED_UNRESOLVED",
    "PEN_RISK_CONFIRMED",
    "INTERMEDIATE",
    "RESOLVED_CONTINUATION",
]
RiskGate = Literal["GREEN", "AMBER", "RED", "UNRESOLVED"]
HIGHER_TIMEFRAME_RISK_STATES = frozenset(
    {
        "NONE",
        "FORMED",
        "FORMED_UNRESOLVED",
        "PEN_RISK_CONFIRMED",
        "INTERMEDIATE",
        "RESOLVED_CONTINUATION",
    }
)
LocatorType = Literal[
    "L2_FIRST_BUY",
    "L2_SECOND_BUY",
    "NONE",
]
ContinuityStatus = Literal["ACTIVE", "TERMINATION_CONFIRMED", "UNRESOLVED"]
TopRiskEvent = Literal[
    "TOP_FRACTAL_MAPPING_UNIQUE",
    "TOP_FRACTAL_MAPPING_UNRESOLVED",
    "MAPPING_LATER_UNIQUE",
    "CENTER_THIRD_SELL_UNEXTENDED",
    "CENTER_EXTENSION_WITH_BOTTOM_DIVERGENCE_BUY",
    "CENTER_THIRD_BUY",
    "OPPOSITE_FRACTAL_COMPLETES_DOWN_PEN",
    "NEW_TOP_FRACTAL_MAPPING_UNIQUE",
    "NEW_TOP_FRACTAL_MAPPING_UNRESOLVED",
]


@dataclass(frozen=True, slots=True)
class GateCheck:
    gate: str
    passed: bool
    code: str
    detail: str


@dataclass(frozen=True, slots=True)
class TopRiskTransition:
    previous: RiskState
    event: TopRiskEvent
    current: RiskState
    reason_code: str


def advance_top_risk_state(
    previous: RiskState,
    event: TopRiskEvent,
) -> TopRiskTransition:
    """Apply only the top-fractal transitions frozen in specification strict strategy."""

    transitions: dict[tuple[RiskState, TopRiskEvent], tuple[RiskState, str]] = {
        ("NONE", "TOP_FRACTAL_MAPPING_UNIQUE"): (
            "FORMED",
            "TOP_FRACTAL_FORMED_MAPPING_UNIQUE",
        ),
        ("NONE", "TOP_FRACTAL_MAPPING_UNRESOLVED"): (
            "FORMED_UNRESOLVED",
            "TOP_FRACTAL_FORMED_MAPPING_UNRESOLVED",
        ),
        ("FORMED_UNRESOLVED", "MAPPING_LATER_UNIQUE"): (
            "FORMED",
            "TOP_FRACTAL_MAPPING_RESOLVED",
        ),
        ("FORMED", "CENTER_THIRD_SELL_UNEXTENDED"): (
            "PEN_RISK_CONFIRMED",
            "MAPPED_CENTER_THIRD_SELL_UNEXTENDED",
        ),
        ("FORMED", "CENTER_EXTENSION_WITH_BOTTOM_DIVERGENCE_BUY"): (
            "INTERMEDIATE",
            "CENTER_EXTENSION_AND_BOTTOM_DIVERGENCE_RECOVERY",
        ),
        ("FORMED", "CENTER_THIRD_BUY"): (
            "RESOLVED_CONTINUATION",
            "MAPPED_CENTER_THIRD_BUY_CONTINUATION",
        ),
        ("PEN_RISK_CONFIRMED", "OPPOSITE_FRACTAL_COMPLETES_DOWN_PEN"): (
            "NONE",
            "HIGH_TIMEFRAME_DOWN_PEN_COMPLETED",
        ),
    }
    for state in ("INTERMEDIATE", "RESOLVED_CONTINUATION"):
        transitions[(state, "NEW_TOP_FRACTAL_MAPPING_UNIQUE")] = (
            "FORMED",
            "NEW_TOP_FRACTAL_MAPPING_UNIQUE",
        )
        transitions[(state, "NEW_TOP_FRACTAL_MAPPING_UNRESOLVED")] = (
            "FORMED_UNRESOLVED",
            "NEW_TOP_FRACTAL_MAPPING_UNRESOLVED",
        )
    target = transitions.get((previous, event))
    if target is None:
        raise ValueError(f"unresolved top-risk transition: {previous}+{event}")
    return TopRiskTransition(previous, event, target[0], target[1])


@dataclass(frozen=True, slots=True)
class SelectionResearchSnapshot:
    snapshot_id: str
    symbol: str
    path: SelectionPath
    effective_at: datetime
    known_at: datetime
    valid_until: datetime
    reviewer: str
    signature: str
    official_evidence_ids: tuple[str, ...]
    industry_opportunity_status: IndustryOpportunity
    fundamental_role: FundamentalRole
    relative_value_status: RelativeValue
    point_in_time_total_market_cap: Decimal | None
    peer_set_id: str | None
    basket_mapping_id: str | None = None

    def __post_init__(self) -> None:
        effective = normalize_datetime(self.effective_at, "effective_at")
        known = normalize_datetime(self.known_at, "known_at")
        valid_until = normalize_datetime(self.valid_until, "valid_until")
        if known > effective or effective > valid_until:
            raise ValueError("research snapshot time order is invalid")
        object.__setattr__(self, "effective_at", effective)
        object.__setattr__(self, "known_at", known)
        object.__setattr__(self, "valid_until", valid_until)
        if not all(
            value and value.strip()
            for value in (
                self.snapshot_id,
                self.symbol,
                self.reviewer,
                self.signature,
            )
        ):
            raise ValueError("signed research identity is required")
        if not self.official_evidence_ids:
            raise ValueError("official research evidence is required")
        if len(self.official_evidence_ids) != len(set(self.official_evidence_ids)):
            raise ValueError("official research evidence ids must be unique")
        if self.point_in_time_total_market_cap is not None and self.point_in_time_total_market_cap <= 0:
            raise ValueError("point-in-time market cap must be positive")
        if self.path == "INDIVIDUAL_THREE_PROGRAM":
            if self.point_in_time_total_market_cap is None or not self.peer_set_id:
                raise ValueError("individual research requires market cap and peer set")
            if self.basket_mapping_id is not None:
                raise ValueError("individual research cannot use an ETF basket mapping")
        elif self.path == "ETF_PROXY":
            if not self.basket_mapping_id:
                raise ValueError("ETF proxy research requires a point-in-time basket mapping")
        else:
            raise ValueError("unsupported selection path")

    def visible_at(self, decision_time: datetime) -> bool:
        observed = normalize_datetime(decision_time, "decision_time")
        return self.known_at <= observed and self.effective_at <= observed <= self.valid_until


@dataclass(frozen=True, slots=True)
class TradeabilitySnapshot:
    symbol: str
    observed_at: datetime
    listed: bool
    st: bool
    suspended: bool
    reliable_continuous_market_data: bool
    continuity_status: ContinuityStatus
    structure_history_sufficient: bool
    price_tick: Decimal | None
    buy_quantity_increment: int | None
    sell_quantity_increment: int | None
    fee_schedule_id: str | None
    price_limits_known: bool
    trading_calendar_known: bool
    completed_daily_volume_sessions: int
    completed_same_clock_l2_sessions: int
    median_daily_raw_volume: Decimal | None
    median_same_clock_l2_volume: Decimal | None
    quote_coverage: Decimal | None
    median_spread_ticks: Decimal | None
    current_quote_valid_and_fresh: bool
    q_liquidity_cap: int

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "observed_at",
            normalize_datetime(self.observed_at, "observed_at"),
        )
        if self.price_tick is not None and self.price_tick <= 0:
            raise ValueError("price_tick must be positive")
        for value in (self.buy_quantity_increment, self.sell_quantity_increment):
            if value is not None and value <= 0:
                raise ValueError("quantity increments must be positive")
        if self.completed_daily_volume_sessions < 0 or self.completed_same_clock_l2_sessions < 0:
            raise ValueError("liquidity session counts cannot be negative")
        if self.quote_coverage is not None and not Decimal("0") <= self.quote_coverage <= Decimal("1"):
            raise ValueError("quote coverage must be in [0, 1]")
        if self.q_liquidity_cap < 0:
            raise ValueError("liquidity cap cannot be negative")


def higher_timeframe_risk_gate(
    *,
    states: tuple[RiskState, RiskState, RiskState],
    completed_ma5_available: bool,
    mapping_unique: bool,
) -> RiskGate:
    """Derive the frozen M/W/D gate for both decisions and validators."""

    if type(completed_ma5_available) is not bool or type(mapping_unique) is not bool:
        raise TypeError("higher-timeframe gate flags must be exact bools")
    if len(states) != 3 or any(
        not isinstance(value, str)
        or value not in HIGHER_TIMEFRAME_RISK_STATES
        for value in states
    ):
        raise ValueError("invalid higher-timeframe risk state")
    if not completed_ma5_available:
        return "UNRESOLVED"
    if "PEN_RISK_CONFIRMED" in states:
        return "RED"
    if "FORMED" in states or "FORMED_UNRESOLVED" in states:
        return "AMBER"
    # A known non-unique active top mapping is represented by the explicit
    # FORMED_UNRESOLVED state above.  A false mapping flag with no such event
    # means the adapter itself did not resolve its structure facts.
    if not mapping_unique:
        return "UNRESOLVED"
    return "GREEN"


@dataclass(frozen=True, slots=True)
class HigherTimeframeRiskSnapshot:
    snapshot_id: str
    observed_at: datetime
    monthly: RiskState
    weekly: RiskState
    daily: RiskState
    monthly_ma5: Decimal | None
    weekly_ma5: Decimal | None
    daily_ma5: Decimal | None
    mapping_unique: bool

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "observed_at",
            normalize_datetime(self.observed_at, "observed_at"),
        )
        if not self.snapshot_id:
            raise ValueError("risk snapshot id is required")
        if any(value is not None and value <= 0 for value in (self.monthly_ma5, self.weekly_ma5, self.daily_ma5)):
            raise ValueError("MA5 values must be positive")

    @property
    def gate(self) -> RiskGate:
        return higher_timeframe_risk_gate(
            states=(self.monthly, self.weekly, self.daily),
            completed_ma5_available=all(
                value is not None
                for value in (
                    self.monthly_ma5,
                    self.weekly_ma5,
                    self.daily_ma5,
                )
            ),
            mapping_unique=self.mapping_unique,
        )


@dataclass(frozen=True, slots=True)
class SectorStrengthSnapshot:
    snapshot_id: str
    sector_id: str
    observed_at: datetime
    anchor_session: date
    member_count: int
    categories: tuple[tuple[str, int], ...]
    strength: Decimal | None
    rank: int | None
    unresolved_reasons: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "observed_at",
            normalize_datetime(self.observed_at, "observed_at"),
        )
        if not self.snapshot_id or not self.sector_id:
            raise ValueError("sector strength identity is required")
        if self.member_count < 0 or self.member_count != len(self.categories):
            raise ValueError("sector member count does not match categories")
        symbols = tuple(symbol for symbol, _category in self.categories)
        if symbols != tuple(sorted(set(symbols))):
            raise ValueError("sector members must be unique and sorted")
        if any(category < 1 or category > 9 for _, category in self.categories):
            raise ValueError("sector member category must be in [1, 9]")
        if self.unresolved_reasons:
            if self.strength is not None or self.rank is not None:
                raise ValueError("unresolved sector strength cannot carry a value or rank")
        elif self.member_count == 0 or self.strength is None or self.rank is None:
            raise ValueError("resolved sector strength requires members, value and rank")

    @property
    def resolved(self) -> bool:
        return not self.unresolved_reasons


@dataclass(frozen=True, slots=True)
class TechnicalEntrySnapshot:
    structure_snapshot_id: str
    observed_at: datetime
    price_basis_revision: str
    stroke_mode: str
    l0_source_frequency: str
    l1_source_frequency: str
    l2_source_frequency: str
    direct_recursive_levels_unique: bool
    all_components_completed: bool
    l0_center_id: str | None
    l0_center_ordinal: int | None
    l0_center_completed: bool
    l0_point_type: str | None
    l0_point_id: str | None
    l0_point_confirmation_time: datetime | None
    l1_departure_completed: bool
    l1_first_return_completed: bool
    first_return_low: Decimal | None
    l0_zg: Decimal | None
    l2_locator: LocatorType
    l2_point_id: str | None
    l2_confirmation_bar_high: Decimal | None
    level_relation_mode: Literal[
        "DIRECT_RECURSIVE",
        "USER_OVERRIDE_INDEPENDENT_TIMEFRAMES",
    ] = "DIRECT_RECURSIVE"
    level_relation_contract_id: str | None = None

    def __post_init__(self) -> None:
        observed = normalize_datetime(self.observed_at, "observed_at")
        object.__setattr__(self, "observed_at", observed)
        if self.l0_point_confirmation_time is not None:
            confirmed = normalize_datetime(
                self.l0_point_confirmation_time,
                "l0_point_confirmation_time",
            )
            object.__setattr__(self, "l0_point_confirmation_time", confirmed)
        if not self.structure_snapshot_id or not self.price_basis_revision:
            raise ValueError("structure snapshot identity is required")
        if self.l0_center_ordinal is not None and self.l0_center_ordinal <= 0:
            raise ValueError("center ordinal must be positive")
        if any(value is not None and value <= 0 for value in (self.first_return_low, self.l0_zg, self.l2_confirmation_bar_high)):
            raise ValueError("technical prices must be positive")
        if self.level_relation_mode == "DIRECT_RECURSIVE":
            if self.level_relation_contract_id is not None:
                raise ValueError("direct recursion cannot carry an override contract")
        elif self.level_relation_mode == "USER_OVERRIDE_INDEPENDENT_TIMEFRAMES":
            if self.direct_recursive_levels_unique:
                raise ValueError("independent charts cannot claim direct recursion")
            if not self.level_relation_contract_id:
                raise ValueError("independent charts require an override contract id")
        else:
            raise ValueError("unsupported strict strategy level relation mode")

    @property
    def level_relation_resolved(self) -> bool:
        return self.direct_recursive_levels_unique or (
            self.level_relation_mode == "USER_OVERRIDE_INDEPENDENT_TIMEFRAMES"
            and bool(self.level_relation_contract_id)
        )


@dataclass(frozen=True, slots=True)
class AccountEntryGate:
    observed_at: datetime
    operations_normal: bool
    reconciliation_passed: bool
    free_strategic_slot: bool
    drawdown: Decimal
    no_active_symbol_order: bool

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "observed_at",
            normalize_datetime(self.observed_at, "observed_at"),
        )
        if self.drawdown < 0:
            raise ValueError("drawdown cannot be negative")


@dataclass(frozen=True, slots=True)
class CandidateSnapshot:
    symbol: str
    market: str
    sector_id: str
    decision_time: datetime
    research: SelectionResearchSnapshot
    tradeability: TradeabilitySnapshot
    market_risk: HigherTimeframeRiskSnapshot
    sector_risk: HigherTimeframeRiskSnapshot
    symbol_risk: HigherTimeframeRiskSnapshot
    sector_strength: SectorStrengthSnapshot
    technical: TechnicalEntrySnapshot
    account: AccountEntryGate
    sector_trigger: SectorTriggerSnapshot | None = None

    def __post_init__(self) -> None:
        decision = normalize_datetime(self.decision_time, "decision_time")
        object.__setattr__(self, "decision_time", decision)
        if self.research.symbol != self.symbol or self.tradeability.symbol != self.symbol:
            raise ValueError("candidate symbol context mismatch")
        if self.sector_strength.sector_id != self.sector_id:
            raise ValueError("candidate sector context mismatch")
        if self.sector_trigger is not None and (
            self.sector_trigger.symbol != self.symbol
            or self.sector_trigger.sector_id != self.sector_id
        ):
            raise ValueError("candidate sector trigger context mismatch")


@dataclass(frozen=True, slots=True)
class CandidateDecision:
    symbol: str
    parameter_set_id: str
    selection_path: SelectionPath
    accepted: bool
    checks: tuple[GateCheck, ...]
    fundamental_role: FundamentalRole
    relative_value_status: RelativeValue
    sector_strength: Decimal | None
    confirmation_time: datetime | None
    # §3.3 的第一排序键。严格体系只有三重高周期风险门均为 GREEN 才可买；
    # 研究变体可以保留 AMBER 候选供人工观察，但必须排在严格可买候选之后。
    higher_timeframe_risk_buyable: bool

    def __post_init__(self) -> None:
        if type(self.higher_timeframe_risk_buyable) is not bool:
            raise TypeError("higher-timeframe risk buyability must be bool")

    @property
    def passed_reason_codes(self) -> tuple[str, ...]:
        return tuple(check.code for check in self.checks if check.passed)

    @property
    def rejected_reason_codes(self) -> tuple[str, ...]:
        return tuple(check.code for check in self.checks if not check.passed)


def _check(gate: str, passed: bool, pass_code: str, fail_code: str, detail: str) -> GateCheck:
    return GateCheck(gate, passed, pass_code if passed else fail_code, detail)


def _risk_check(label: str, snapshot: HigherTimeframeRiskSnapshot, decision: datetime) -> GateCheck:
    visible = snapshot.observed_at <= decision
    gate = snapshot.gate if visible else "UNRESOLVED"
    return _check(
        label,
        gate == "GREEN",
        f"PASS_{label.upper()}_GREEN",
        f"REJECT_{label.upper()}_{gate}",
        f"{label} gate={gate}; visible={visible}",
    )


def calculate_entry_liquidity_cap(
    tradeability: TradeabilitySnapshot,
    parameters: StrategyParameters,
) -> int:
    """Recompute the frozen optional-order cap from completed raw-volume facts."""

    increment = tradeability.buy_quantity_increment
    if (
        increment is None
        or tradeability.completed_daily_volume_sessions
        < parameters.liquidity_lookback_sessions
        or tradeability.completed_same_clock_l2_sessions
        < parameters.liquidity_lookback_sessions
        or tradeability.median_daily_raw_volume is None
        or tradeability.median_same_clock_l2_volume is None
        or tradeability.median_daily_raw_volume < 0
        or tradeability.median_same_clock_l2_volume < 0
    ):
        return 0
    raw_cap = min(
        tradeability.median_daily_raw_volume
        * parameters.max_order_fraction_of_median_daily_volume,
        tradeability.median_same_clock_l2_volume
        * parameters.max_order_fraction_of_median_same_clock_l2_volume,
    )
    return int(
        (raw_cap / Decimal(increment)).to_integral_value(rounding=ROUND_DOWN)
    ) * increment


def evaluate_candidate(
    candidate: CandidateSnapshot,
    parameters: StrategyParameters,
) -> CandidateDecision:
    decision = candidate.decision_time
    checks: list[GateCheck] = []
    same_path = candidate.research.path == parameters.selection_path
    checks.append(
        _check(
            "selection_path",
            same_path,
            "PASS_SELECTION_PATH_FROZEN",
            "REJECT_SELECTION_PATH_MISMATCH",
            f"snapshot={candidate.research.path}; parameters={parameters.selection_path}",
        )
    )
    visible_research = candidate.research.visible_at(decision)
    checks.append(
        _check(
            "research_visibility",
            visible_research,
            "PASS_RESEARCH_POINT_IN_TIME",
            "REJECT_RESEARCH_NOT_VISIBLE_OR_EXPIRED",
            candidate.research.snapshot_id,
        )
    )
    if parameters.selection_path == "INDIVIDUAL_THREE_PROGRAM":
        trigger = candidate.sector_trigger
        trigger_passed = trigger is not None and trigger.passes(decision)
        checks.append(
            _check(
                "sector_trigger",
                trigger_passed,
                "PASS_QMT_SECTOR_TRIGGER_POINT_IN_TIME",
                "REJECT_QMT_SECTOR_TRIGGER_MISSING_OR_INVALID",
                (
                    "UNRESOLVED"
                    if trigger is None
                    else (
                        f"{trigger.source}; sector={trigger.sector_id}; "
                        f"bar={trigger.latest_completed_bar_at.isoformat()}"
                    )
                ),
            )
        )
        checks.extend(
            (
                _check(
                    "industry_opportunity",
                    candidate.research.industry_opportunity_status == "PASS",
                    "PASS_INDUSTRY_LONG_TERM_OPPORTUNITY",
                    "REJECT_INDUSTRY_OPPORTUNITY",
                    candidate.research.industry_opportunity_status,
                ),
                _check(
                    "fundamental_role",
                    candidate.research.fundamental_role in {"LEADER", "GROWTH_CHALLENGER"},
                    "PASS_FUNDAMENTAL_ROLE",
                    "REJECT_FUNDAMENTAL_ROLE",
                    candidate.research.fundamental_role,
                ),
                _check(
                    "relative_value",
                    candidate.research.relative_value_status in {"UNDERVALUED", "FAIR"},
                    "PASS_RELATIVE_VALUE",
                    "REJECT_RELATIVE_VALUE",
                    candidate.research.relative_value_status,
                ),
            )
        )
    else:
        checks.append(
            _check(
                "sector_trigger",
                True,
                "PASS_ETF_PROXY_SECTOR_TRIGGER_NOT_APPLICABLE",
                "REJECT_ETF_PROXY_SECTOR_TRIGGER",
                "ETF proxy follows its separately frozen basket path",
            )
        )
        etf_fields = (
            candidate.research.industry_opportunity_status == "NOT_APPLICABLE"
            and candidate.research.fundamental_role == "ETF_PROXY"
            and candidate.research.relative_value_status == "ETF_PROXY"
            and bool(candidate.research.basket_mapping_id)
        )
        checks.append(
            _check(
                "etf_proxy",
                etf_fields,
                "PASS_ETF_PROXY_SNAPSHOT",
                "REJECT_ETF_PROXY_SNAPSHOT",
                candidate.research.basket_mapping_id or "UNRESOLVED",
            )
        )

    trade = candidate.tradeability
    trade_visible = trade.observed_at <= decision
    checks.extend(
        (
            _check("tradeability_visibility", trade_visible, "PASS_TRADEABILITY_POINT_IN_TIME", "REJECT_TRADEABILITY_FUTURE_SNAPSHOT", trade.observed_at.isoformat()),
            _check("listing", trade.listed, "PASS_LISTED", "REJECT_NOT_LISTED", str(trade.listed)),
            _check("st", not trade.st, "PASS_NOT_ST", "REJECT_ST", str(trade.st)),
            _check("suspension", not trade.suspended, "PASS_NOT_SUSPENDED", "REJECT_SUSPENDED", str(trade.suspended)),
            _check("market_data", trade.reliable_continuous_market_data, "PASS_RELIABLE_CONTINUOUS_DATA", "REJECT_UNRELIABLE_CONTINUOUS_DATA", str(trade.reliable_continuous_market_data)),
            _check("continuity", trade.continuity_status == "ACTIVE", "PASS_TRADING_CONTINUITY", f"REJECT_TRADING_CONTINUITY_{trade.continuity_status}", trade.continuity_status),
            _check("structure_history", trade.structure_history_sufficient, "PASS_STRUCTURE_HISTORY", "REJECT_STRUCTURE_HISTORY_INSUFFICIENT", str(trade.structure_history_sufficient)),
            _check("runtime_market_rules", trade.price_tick is not None and trade.buy_quantity_increment is not None and trade.sell_quantity_increment is not None and trade.fee_schedule_id is not None and trade.price_limits_known and trade.trading_calendar_known, "PASS_RUNTIME_MARKET_RULES", "REJECT_RUNTIME_MARKET_RULES_UNRESOLVED", trade.fee_schedule_id or "UNRESOLVED"),
            _check("liquidity_history", trade.completed_daily_volume_sessions >= parameters.liquidity_lookback_sessions and trade.completed_same_clock_l2_sessions >= parameters.liquidity_lookback_sessions, "PASS_LIQUIDITY_20_SESSIONS", "REJECT_LIQUIDITY_HISTORY_INSUFFICIENT", f"daily={trade.completed_daily_volume_sessions}; l2={trade.completed_same_clock_l2_sessions}"),
            _check("quote_coverage", trade.quote_coverage is not None and trade.quote_coverage >= parameters.valid_quote_coverage_min, "PASS_QUOTE_COVERAGE", "REJECT_QUOTE_COVERAGE", str(trade.quote_coverage)),
            _check("spread", trade.median_spread_ticks is not None and trade.median_spread_ticks <= parameters.median_effective_spread_ticks_max, "PASS_MEDIAN_SPREAD", "REJECT_MEDIAN_SPREAD", str(trade.median_spread_ticks)),
            _check("current_quote", trade.current_quote_valid_and_fresh, "PASS_CURRENT_QUOTE", "REJECT_CURRENT_QUOTE", str(trade.current_quote_valid_and_fresh)),
            _check(
                "liquidity_quantity",
                trade.q_liquidity_cap > 0
                and trade.q_liquidity_cap
                == calculate_entry_liquidity_cap(trade, parameters),
                "PASS_LIQUIDITY_QUANTITY",
                "REJECT_LIQUIDITY_CAP_UNRESOLVED_OR_INCONSISTENT",
                (
                    f"snapshot={trade.q_liquidity_cap}; "
                    f"recomputed={calculate_entry_liquidity_cap(trade, parameters)}"
                ),
            ),
        )
    )
    checks.extend(
        (
            _risk_check("market_risk", candidate.market_risk, decision),
            _risk_check("sector_risk", candidate.sector_risk, decision),
            _risk_check("symbol_risk", candidate.symbol_risk, decision),
        )
    )
    strength = candidate.sector_strength
    checks.append(
        _check(
            "sector_strength",
            strength.observed_at <= decision and strength.resolved,
            "PASS_SECTOR_STRENGTH_POINT_IN_TIME",
            "REJECT_SECTOR_STRENGTH_UNRESOLVED",
            ",".join(strength.unresolved_reasons) or str(strength.strength),
        )
    )
    technical = candidate.technical
    technical_visible = (
        technical.observed_at <= decision
        and technical.l0_point_confirmation_time is not None
        and technical.l0_point_confirmation_time <= decision
    )
    checks.extend(
        (
            _check("structure_visibility", technical_visible, "PASS_STRUCTURE_POINT_IN_TIME", "REJECT_STRUCTURE_FUTURE_OR_UNCONFIRMED", technical.structure_snapshot_id),
            _check("stroke_mode", technical.stroke_mode == STRICT_STROKE_MODE, "PASS_STRICT_STROKE_MODE", "REJECT_NON_STRICT_STROKE_MODE", technical.stroke_mode),
            _check("observation_windows", (technical.l0_source_frequency, technical.l1_source_frequency, technical.l2_source_frequency) == ("30m", "5m", "1m"), "PASS_30M_5M_1M_WINDOWS", "REJECT_OBSERVATION_WINDOWS", f"{technical.l0_source_frequency}/{technical.l1_source_frequency}/{technical.l2_source_frequency}"),
            _check(
                "level_relation",
                technical.level_relation_resolved,
                (
                    "PASS_DIRECT_RECURSIVE_LEVELS"
                    if technical.level_relation_mode == "DIRECT_RECURSIVE"
                    else "PASS_USER_OVERRIDE_INDEPENDENT_TIMEFRAMES"
                ),
                "REJECT_LEVEL_RELATION_UNRESOLVED",
                (
                    technical.level_relation_contract_id
                    or str(technical.direct_recursive_levels_unique)
                ),
            ),
            _check("completed_structure", technical.all_components_completed, "PASS_COMPLETED_STRUCTURE", "REJECT_INCOMPLETE_STRUCTURE", str(technical.all_components_completed)),
            _check("first_l0_center", technical.l0_center_completed and technical.l0_center_ordinal == 1 and bool(technical.l0_center_id), "PASS_FIRST_L0_CENTER", "REJECT_NOT_FIRST_COMPLETED_L0_CENTER", str(technical.l0_center_ordinal)),
            _check("l0_three_buy", technical.l0_point_type == "3buy" and bool(technical.l0_point_id), "PASS_L0_THREE_BUY", "REJECT_L0_THREE_BUY_MISSING", str(technical.l0_point_type)),
            _check("l1_departure_return", technical.l1_departure_completed and technical.l1_first_return_completed, "PASS_L1_DEPARTURE_FIRST_RETURN", "REJECT_L1_DEPARTURE_OR_RETURN_INCOMPLETE", f"departure={technical.l1_departure_completed}; return={technical.l1_first_return_completed}"),
            _check("third_buy_boundary", technical.first_return_low is not None and technical.l0_zg is not None and technical.first_return_low >= technical.l0_zg, "PASS_THIRD_BUY_ABOVE_OR_EQUAL_ZG", "REJECT_THIRD_BUY_RETURNED_INSIDE", f"low={technical.first_return_low}; ZG={technical.l0_zg}"),
            _check("l2_locator", technical.l2_locator in {"L2_FIRST_BUY", "L2_SECOND_BUY"} and bool(technical.l2_point_id) and technical.l2_confirmation_bar_high is not None, "PASS_L2_LOCATOR", "REJECT_L2_LOCATOR", technical.l2_locator),
        )
    )
    account = candidate.account
    checks.extend(
        (
            _check("account_visibility", account.observed_at <= decision, "PASS_ACCOUNT_POINT_IN_TIME", "REJECT_ACCOUNT_FUTURE_SNAPSHOT", account.observed_at.isoformat()),
            _check("operations", account.operations_normal and account.reconciliation_passed, "PASS_OPERATIONS_AND_RECONCILIATION", "REJECT_OPERATIONS_OR_RECONCILIATION", f"normal={account.operations_normal}; reconciliation={account.reconciliation_passed}"),
            _check("slot", account.free_strategic_slot, "PASS_STRATEGIC_SLOT", "REJECT_NO_STRATEGIC_SLOT", str(account.free_strategic_slot)),
            _check("drawdown", account.drawdown < parameters.entry_drawdown_halt, "PASS_DRAWDOWN_BELOW_10PCT", "REJECT_DRAWDOWN_10PCT_OR_MORE", str(account.drawdown)),
            _check("active_order", account.no_active_symbol_order, "PASS_NO_ACTIVE_SYMBOL_ORDER", "REJECT_ACTIVE_SYMBOL_ORDER", str(account.no_active_symbol_order)),
        )
    )
    accepted = all(check.passed for check in checks)
    return CandidateDecision(
        symbol=candidate.symbol,
        parameter_set_id=parameters.parameter_set_id,
        selection_path=parameters.selection_path,
        accepted=accepted,
        checks=tuple(checks),
        fundamental_role=candidate.research.fundamental_role,
        relative_value_status=candidate.research.relative_value_status,
        sector_strength=candidate.sector_strength.strength,
        confirmation_time=candidate.technical.l0_point_confirmation_time,
        higher_timeframe_risk_buyable=all(
            snapshot.gate == "GREEN"
            for snapshot in (
                candidate.market_risk,
                candidate.sector_risk,
                candidate.symbol_risk,
            )
        ),
    )


def candidate_decision_sort_key(
    decision: CandidateDecision | None,
) -> tuple[int, int, int, Decimal, datetime, str]:
    """Frozen cross-candidate cash/slot competition order from §3.3.

    The key is shared by page ranking and replay execution.  A missing
    candidate sorts last and is useful only for non-entry intents, which are
    already separated by the decision core's strategic priority.
    """

    role_rank = {"LEADER": 0, "GROWTH_CHALLENGER": 1, "ETF_PROXY": 0}
    value_rank = {"UNDERVALUED": 0, "FAIR": 1, "ETF_PROXY": 0}
    if decision is None:
        return (
            99,
            99,
            99,
            Decimal("0"),
            datetime.max.replace(tzinfo=timezone.utc),
            "",
        )
    return (
        0 if decision.higher_timeframe_risk_buyable else 1,
        role_rank.get(decision.fundamental_role, 99),
        value_rank.get(decision.relative_value_status, 99),
        -(decision.sector_strength or Decimal("0")),
        decision.confirmation_time
        or datetime.max.replace(tzinfo=timezone.utc),
        decision.symbol,
    )


def rank_candidate_decisions(decisions: tuple[CandidateDecision, ...]) -> tuple[CandidateDecision, ...]:
    accepted = tuple(decision for decision in decisions if decision.accepted)
    if len({decision.parameter_set_id for decision in accepted}) > 1:
        raise ValueError("candidate ranking cannot mix parameter snapshots")
    return tuple(
        sorted(
            accepted,
            key=candidate_decision_sort_key,
        )
    )


@dataclass(frozen=True, slots=True)
class CompletedDailyClose:
    session: date
    close: Decimal
    known_at: datetime
    completed: bool = True

    def __post_init__(self) -> None:
        object.__setattr__(self, "known_at", normalize_datetime(self.known_at, "known_at"))
        if self.close <= 0:
            raise ValueError("daily close must be positive")


def completed_ma5_at(
    rows: tuple[CompletedDailyClose, ...],
    *,
    decision_time: datetime,
) -> Decimal | None:
    """Return MA5 from the last five completed, point-in-time visible closes."""

    decision = normalize_datetime(decision_time, "decision_time")
    visible = tuple(
        row
        for row in rows
        if row.completed
        and row.known_at <= decision
        and row.session <= decision.date()
    )
    sessions = tuple(row.session for row in visible)
    if sessions != tuple(sorted(set(sessions))):
        raise ValueError("visible period closes must be unique and chronological")
    return completed_sma(tuple(row.close for row in visible), 5)


MemberHistoryStatus = Literal["COMPLETE", "NEW_LISTING", "SUSPENDED", "UNEXPLAINED_GAP"]


@dataclass(frozen=True, slots=True)
class SectorMemberHistory:
    symbol: str
    listed_on: date
    history_status: MemberHistoryStatus
    closes: tuple[CompletedDailyClose, ...]

    def __post_init__(self) -> None:
        sessions = tuple(row.session for row in self.closes)
        if sessions != tuple(sorted(set(sessions))):
            raise ValueError("daily closes must be unique and chronological")


def completed_sma(closes: tuple[Decimal, ...], period: int) -> Decimal | None:
    if period <= 0:
        raise ValueError("period must be positive")
    if len(closes) < period:
        return None
    window = closes[-period:]
    return sum(window, Decimal("0")) / Decimal(period)


_SECTOR_MA_PERIODS = (5, 13, 21, 34, 55, 89, 144, 233)


def member_ma_strength_category(
    member: SectorMemberHistory,
    *,
    anchor_session: date,
    decision_time: datetime,
) -> int | None:
    decision = normalize_datetime(decision_time, "decision_time")
    if member.history_status == "UNEXPLAINED_GAP":
        return None
    visible = tuple(
        row
        for row in member.closes
        if row.completed and row.known_at <= decision and row.session <= decision.date()
    )
    if len(visible) < 5:
        return 1
    conquered: list[bool] = []
    for period in _SECTOR_MA_PERIODS:
        attacked = False
        for index, row in enumerate(visible):
            if row.session < anchor_session:
                continue
            prefix = tuple(value.close for value in visible[: index + 1])
            average = completed_sma(prefix, period)
            if average is not None and row.close > average:
                attacked = True
                break
        conquered.append(attacked)
    for ordinal, attacked in enumerate(conquered, start=1):
        if not attacked:
            return ordinal
    return 9


def build_sector_strength_snapshot(
    *,
    snapshot_id: str,
    sector_id: str,
    anchor_session: date,
    decision_time: datetime,
    members: tuple[SectorMemberHistory, ...],
    rank: int | None,
) -> SectorStrengthSnapshot:
    decision = normalize_datetime(decision_time, "decision_time")
    symbols = tuple(member.symbol for member in members)
    if symbols != tuple(sorted(set(symbols))):
        raise ValueError("point-in-time sector members must be unique and sorted")
    categories: list[tuple[str, int]] = []
    unresolved: list[str] = []
    for member in members:
        category = member_ma_strength_category(
            member,
            anchor_session=anchor_session,
            decision_time=decision,
        )
        if category is None:
            unresolved.append(f"UNEXPLAINED_MEMBER_HISTORY:{member.symbol}")
        else:
            categories.append((member.symbol, category))
    if not members:
        unresolved.append("EMPTY_POINT_IN_TIME_BASKET")
    if unresolved:
        categories = [(member.symbol, 1) for member in members]
        return SectorStrengthSnapshot(
            snapshot_id=snapshot_id,
            sector_id=sector_id,
            observed_at=decision,
            anchor_session=anchor_session,
            member_count=len(members),
            categories=tuple(categories),
            strength=None,
            rank=None,
            unresolved_reasons=tuple(unresolved),
        )
    strength = sum((Decimal(category) for _, category in categories), Decimal("0")) / Decimal(len(categories))
    return SectorStrengthSnapshot(
        snapshot_id=snapshot_id,
        sector_id=sector_id,
        observed_at=decision,
        anchor_session=anchor_session,
        member_count=len(members),
        categories=tuple(categories),
        strength=strength,
        rank=rank,
    )


__all__ = [
    "AccountEntryGate",
    "CandidateDecision",
    "CandidateSnapshot",
    "CompletedDailyClose",
    "GateCheck",
    "HIGHER_TIMEFRAME_RISK_STATES",
    "HigherTimeframeRiskSnapshot",
    "SectorMemberHistory",
    "SectorStrengthSnapshot",
    "SelectionResearchSnapshot",
    "TechnicalEntrySnapshot",
    "TradeabilitySnapshot",
    "TopRiskTransition",
    "advance_top_risk_state",
    "build_sector_strength_snapshot",
    "candidate_decision_sort_key",
    "calculate_entry_liquidity_cap",
    "completed_sma",
    "completed_ma5_at",
    "evaluate_candidate",
    "higher_timeframe_risk_gate",
    "member_ma_strength_category",
    "rank_candidate_decisions",
]
