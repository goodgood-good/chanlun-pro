"""Frozen, point-in-time research proxy for an otherwise unavailable program.

The unique strict strategy specification correctly says that the original Chanlun lessons
do not define numerical formulae for industry opportunity, leadership, growth,
or relative valuation.  A signed analyst adjudication remains the strict
``INDIVIDUAL_THREE_PROGRAM`` authority.

For a reproducible research backtest only, this module supplies an explicitly
separate approximation based on disclosure-dated QMT per-share metrics and
completed daily liquidity.  It never emits ``FULL_SYSTEM_ELIGIBLE`` and can
never enable live trading.  The approximation is frozen, hash identified, and
returns a reason for every accepted or rejected stock.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime
from decimal import Decimal
from statistics import median
from bisect import bisect_right
from typing import Literal, Sequence

from chanlun.decision_support.fingerprints import normalize_datetime, sha256_json


ApproximateRole = Literal["LEADER", "GROWTH_CHALLENGER", "REJECT", "UNRESOLVED"]
ApproximateValue = Literal["UNDERVALUED", "FAIR", "OVERVALUED", "UNRESOLVED"]
ApproximateOpportunity = Literal["PASS", "REJECT", "UNRESOLVED"]


@dataclass(frozen=True, slots=True)
class ResearchApproximationParameters:
    schema: str = "chanlun-qmt-research-approximation-parameters"
    rebalance_rule: str = "MONTH_END_LAST_COMPLETED_DAILY_BAR"
    finance_visibility_rule: str = "ANNOUNCEMENT_DATE_PLUS_ONE_CALENDAR_DAY"
    liquidity_lookback_sessions: int = 20
    minimum_peer_count: int = 8
    minimum_complete_peer_coverage: Decimal = Decimal("0.50")
    leader_liquidity_percentile_min: Decimal = Decimal("0.80")
    leader_roe_percentile_min: Decimal = Decimal("0.50")
    challenger_liquidity_percentile_min: Decimal = Decimal("0.40")
    challenger_growth_percentile_min: Decimal = Decimal("0.70")
    undervalued_pb_percentile_max: Decimal = Decimal("0.30")
    fair_pb_percentile_max: Decimal = Decimal("0.70")
    sector_opportunity_rule: str = "MEDIAN_REVENUE_YOY_OR_PARENT_PROFIT_YOY_POSITIVE"
    leader_size_proxy: str = "20_SESSION_MEDIAN_DAILY_AMOUNT_NOT_MARKET_CAP"
    status_ceiling: str = "RESEARCH_ONLY"
    live_status: str = "LIVE_DISABLED"

    def __post_init__(self) -> None:
        if self.liquidity_lookback_sessions <= 0 or self.minimum_peer_count < 2:
            raise ValueError("research approximation sample sizes are invalid")
        for field in (
            "minimum_complete_peer_coverage",
            "leader_liquidity_percentile_min",
            "leader_roe_percentile_min",
            "challenger_liquidity_percentile_min",
            "challenger_growth_percentile_min",
            "undervalued_pb_percentile_max",
            "fair_pb_percentile_max",
        ):
            value = getattr(self, field)
            if not Decimal("0") < value <= Decimal("1"):
                raise ValueError(f"{field} must be in (0, 1]")
        if self.undervalued_pb_percentile_max > self.fair_pb_percentile_max:
            raise ValueError("undervalued threshold cannot exceed fair threshold")
        if self.status_ceiling != "RESEARCH_ONLY" or self.live_status != "LIVE_DISABLED":
            raise ValueError("research approximation cannot promote live status")

    @property
    def parameter_set_id(self) -> str:
        return sha256_json(asdict(self))


@dataclass(frozen=True, slots=True)
class ResearchApproximationObservation:
    symbol: str
    sector_id: str
    observed_at: datetime
    last_completed_daily_close: Decimal | None
    median_daily_amount_20: Decimal | None
    book_value_per_share: Decimal | None
    roe: Decimal | None
    revenue_yoy: Decimal | None
    parent_profit_yoy: Decimal | None
    daily_known_at: datetime | None
    finance_known_at: datetime | None
    source_revision: str

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "observed_at",
            normalize_datetime(self.observed_at, "observed_at"),
        )
        for field in ("daily_known_at", "finance_known_at"):
            value = getattr(self, field)
            if value is not None:
                normalized = normalize_datetime(value, field)
                object.__setattr__(self, field, normalized)
                if normalized > self.observed_at:
                    raise ValueError(f"{field} cannot come from the future")
        if not self.symbol or not self.sector_id:
            raise ValueError("research approximation identity is required")
        if not self.source_revision.startswith("sha256:"):
            raise ValueError("research approximation source revision is required")
        for field in (
            "last_completed_daily_close",
            "median_daily_amount_20",
            "book_value_per_share",
        ):
            value = getattr(self, field)
            if value is not None and value <= 0:
                raise ValueError(f"{field} must be positive when supplied")

    @property
    def complete(self) -> bool:
        return all(
            getattr(self, field) is not None
            for field in (
                "last_completed_daily_close",
                "median_daily_amount_20",
                "book_value_per_share",
                "roe",
                "revenue_yoy",
                "parent_profit_yoy",
                "daily_known_at",
                "finance_known_at",
            )
        )


@dataclass(frozen=True, slots=True)
class ResearchApproximationDecision:
    symbol: str
    sector_id: str
    observed_at: datetime
    industry_opportunity_status: ApproximateOpportunity
    fundamental_role: ApproximateRole
    relative_value_status: ApproximateValue
    liquidity_percentile: Decimal | None
    roe_percentile: Decimal | None
    revenue_growth_percentile: Decimal | None
    profit_growth_percentile: Decimal | None
    pb_ratio: Decimal | None
    pb_percentile: Decimal | None
    accepted: bool
    reason_codes: tuple[str, ...]
    parameter_set_id: str
    source_revision: str
    data_grade: str = "RESEARCH_APPROXIMATION"
    highest_status: str = "RESEARCH_ONLY"
    live_status: str = "LIVE_DISABLED"

    @property
    def decision_id(self) -> str:
        return sha256_json(asdict(self))


@dataclass(frozen=True, slots=True)
class ResearchApproximationEvent:
    observed_at: datetime
    decisions: tuple[ResearchApproximationDecision, ...]

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "observed_at",
            normalize_datetime(self.observed_at, "observed_at"),
        )
        symbols = tuple(value.symbol for value in self.decisions)
        if symbols != tuple(sorted(set(symbols))):
            raise ValueError("research approximation event symbols must be sorted")
        if any(value.observed_at != self.observed_at for value in self.decisions):
            raise ValueError("research approximation event crossed a timestamp")


@dataclass(frozen=True, slots=True)
class ResearchApproximationLedger:
    parameters: ResearchApproximationParameters
    sector_scope_sha256: str
    pit_snapshot_sha256: str
    trigger_ledger_sha256: str
    events: tuple[ResearchApproximationEvent, ...]
    schema: str = "chanlun-qmt-research-approximation-ledger"
    data_grade: str = "RESEARCH_APPROXIMATION"
    highest_status: str = "RESEARCH_ONLY"
    live_status: str = "LIVE_DISABLED"

    def __post_init__(self) -> None:
        times = tuple(value.observed_at for value in self.events)
        if times != tuple(sorted(set(times))):
            raise ValueError("research approximation events must be chronological")
        for field in (
            "sector_scope_sha256",
            "pit_snapshot_sha256",
            "trigger_ledger_sha256",
        ):
            if not getattr(self, field).startswith("sha256:"):
                raise ValueError(f"{field} must be hash identified")
        if self.highest_status != "RESEARCH_ONLY" or self.live_status != "LIVE_DISABLED":
            raise ValueError("research approximation ledger cannot enable live trading")

    @property
    def content_sha256(self) -> str:
        return sha256_json(asdict(self))

    @property
    def ever_accepted_symbols(self) -> tuple[str, ...]:
        return tuple(
            sorted(
                {
                    value.symbol
                    for event in self.events
                    for value in event.decisions
                    if value.accepted
                }
            )
        )

    def decision_at(
        self,
        symbol: str,
        observed_at: datetime,
    ) -> ResearchApproximationDecision | None:
        decision = normalize_datetime(observed_at, "observed_at")
        times = tuple(value.observed_at for value in self.events)
        position = bisect_right(times, decision) - 1
        if position < 0:
            return None
        matches = tuple(
            value
            for value in self.events[position].decisions
            if value.symbol == symbol
        )
        if len(matches) > 1:
            raise ValueError("research approximation decision identity collision")
        return None if not matches else matches[0]


def _percentile(value: Decimal, peers: Sequence[Decimal]) -> Decimal:
    """Inclusive empirical percentile; ties share the upper rank."""

    if not peers:
        raise ValueError("percentile requires peers")
    return Decimal(sum(item <= value for item in peers)) / Decimal(len(peers))


def evaluate_sector_research_approximation(
    observations: Sequence[ResearchApproximationObservation],
    *,
    sector_triggered: bool,
    parameters: ResearchApproximationParameters | None = None,
) -> tuple[ResearchApproximationDecision, ...]:
    """Evaluate one point-in-time peer set without deleting missing members."""

    params = parameters or ResearchApproximationParameters()
    rows = tuple(sorted(observations, key=lambda value: value.symbol))
    if not rows:
        return ()
    if len({row.symbol for row in rows}) != len(rows):
        raise ValueError("research approximation symbols must be unique")
    if len({(row.sector_id, row.observed_at) for row in rows}) != 1:
        raise ValueError("research approximation cannot mix sectors or timestamps")

    complete = tuple(row for row in rows if row.complete)
    coverage = Decimal(len(complete)) / Decimal(len(rows))
    peer_gate = (
        len(rows) >= params.minimum_peer_count
        and coverage >= params.minimum_complete_peer_coverage
        and len(complete) >= params.minimum_peer_count
    )
    if peer_gate:
        revenue_values = tuple(row.revenue_yoy for row in complete)
        profit_values = tuple(row.parent_profit_yoy for row in complete)
        assert all(value is not None for value in (*revenue_values, *profit_values))
        opportunity: ApproximateOpportunity = (
            "PASS"
            if median(revenue_values) > 0 or median(profit_values) > 0  # type: ignore[arg-type]
            else "REJECT"
        )
    else:
        opportunity = "UNRESOLVED"

    amounts = tuple(row.median_daily_amount_20 for row in complete)
    roes = tuple(row.roe for row in complete)
    revenues = tuple(row.revenue_yoy for row in complete)
    profits = tuple(row.parent_profit_yoy for row in complete)
    pbs = tuple(
        row.last_completed_daily_close / row.book_value_per_share
        for row in complete
        if row.last_completed_daily_close is not None
        and row.book_value_per_share is not None
    )
    decisions: list[ResearchApproximationDecision] = []
    for row in rows:
        liquidity_rank = roe_rank = revenue_rank = profit_rank = None
        pb = pb_rank = None
        role: ApproximateRole = "UNRESOLVED"
        value_status: ApproximateValue = "UNRESOLVED"
        reasons: list[str] = []
        if not sector_triggered:
            reasons.append("REJECT_SECTOR_NOT_TRIGGERED")
        if not peer_gate:
            reasons.append("REJECT_RESEARCH_PEER_COVERAGE_INSUFFICIENT")
        if not row.complete:
            reasons.append("REJECT_POINT_IN_TIME_RESEARCH_FIELDS_MISSING")
        if peer_gate and row.complete:
            assert row.median_daily_amount_20 is not None
            assert row.roe is not None
            assert row.revenue_yoy is not None
            assert row.parent_profit_yoy is not None
            assert row.last_completed_daily_close is not None
            assert row.book_value_per_share is not None
            liquidity_rank = _percentile(row.median_daily_amount_20, amounts)  # type: ignore[arg-type]
            roe_rank = _percentile(row.roe, roes)  # type: ignore[arg-type]
            revenue_rank = _percentile(row.revenue_yoy, revenues)  # type: ignore[arg-type]
            profit_rank = _percentile(row.parent_profit_yoy, profits)  # type: ignore[arg-type]
            leader = (
                liquidity_rank >= params.leader_liquidity_percentile_min
                and roe_rank >= params.leader_roe_percentile_min
                and row.revenue_yoy >= 0
            )
            challenger = (
                liquidity_rank >= params.challenger_liquidity_percentile_min
                and revenue_rank >= params.challenger_growth_percentile_min
                and profit_rank >= params.challenger_growth_percentile_min
                and row.roe > 0
            )
            role = "LEADER" if leader else "GROWTH_CHALLENGER" if challenger else "REJECT"
            pb = row.last_completed_daily_close / row.book_value_per_share
            pb_rank = _percentile(pb, pbs)
            value_status = (
                "UNDERVALUED"
                if pb_rank <= params.undervalued_pb_percentile_max
                else "FAIR"
                if pb_rank <= params.fair_pb_percentile_max
                else "OVERVALUED"
            )
        if opportunity != "PASS":
            reasons.append(f"REJECT_INDUSTRY_OPPORTUNITY_{opportunity}")
        if role not in {"LEADER", "GROWTH_CHALLENGER"}:
            reasons.append(f"REJECT_FUNDAMENTAL_ROLE_{role}")
        if value_status not in {"UNDERVALUED", "FAIR"}:
            reasons.append(f"REJECT_RELATIVE_VALUE_{value_status}")
        accepted = not reasons
        if accepted:
            reasons.append("PASS_QMT_RESEARCH_APPROXIMATION")
        decisions.append(
            ResearchApproximationDecision(
                symbol=row.symbol,
                sector_id=row.sector_id,
                observed_at=row.observed_at,
                industry_opportunity_status=opportunity,
                fundamental_role=role,
                relative_value_status=value_status,
                liquidity_percentile=liquidity_rank,
                roe_percentile=roe_rank,
                revenue_growth_percentile=revenue_rank,
                profit_growth_percentile=profit_rank,
                pb_ratio=pb,
                pb_percentile=pb_rank,
                accepted=accepted,
                reason_codes=tuple(reasons),
                parameter_set_id=params.parameter_set_id,
                source_revision=row.source_revision,
            )
        )
    return tuple(decisions)


__all__ = (
    "ResearchApproximationDecision",
    "ResearchApproximationEvent",
    "ResearchApproximationLedger",
    "ResearchApproximationObservation",
    "ResearchApproximationParameters",
    "evaluate_sector_research_approximation",
)
