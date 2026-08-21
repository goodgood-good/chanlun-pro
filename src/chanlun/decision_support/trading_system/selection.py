from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Literal

from chanlun.decision_support.fingerprints import normalize_datetime
from chanlun.decision_support.trading_system.parameters import (
    SelectionPath,
)


SELECTION_RESEARCH_LEDGER_SCHEMA = "chanlun-selection-research-ledger"
INDUSTRY_OPPORTUNITY_STATUSES = frozenset(
    {"PASS", "REJECT", "UNRESOLVED", "NOT_APPLICABLE"}
)
FUNDAMENTAL_ROLES = frozenset(
    {"LEADER", "GROWTH_CHALLENGER", "REJECT", "UNRESOLVED", "ETF_PROXY"}
)
RELATIVE_VALUE_STATUSES = frozenset(
    {"UNDERVALUED", "FAIR", "OVERVALUED", "UNRESOLVED", "ETF_PROXY"}
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
FormalSelectionStatus = Literal["PASS", "REJECT", "UNRESOLVED"]
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
class TopRiskTransition:
    previous: RiskState
    event: TopRiskEvent
    current: RiskState
    reason_code: str


def advance_top_risk_state(
    previous: RiskState,
    event: TopRiskEvent,
) -> TopRiskTransition:
    """只应用严格策略规范冻结的顶分型状态转换。"""

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
        if self.industry_opportunity_status not in INDUSTRY_OPPORTUNITY_STATUSES:
            raise ValueError("industry opportunity status is invalid")
        if self.fundamental_role not in FUNDAMENTAL_ROLES:
            raise ValueError("fundamental role is invalid")
        if self.relative_value_status not in RELATIVE_VALUE_STATUSES:
            raise ValueError("relative value status is invalid")
        if self.point_in_time_total_market_cap is not None and self.point_in_time_total_market_cap <= 0:
            raise ValueError("point-in-time market cap must be positive")
        if self.path == "INDIVIDUAL_THREE_PROGRAM":
            if self.point_in_time_total_market_cap is None or not self.peer_set_id:
                raise ValueError("individual research requires market cap and peer set")
            if self.basket_mapping_id is not None:
                raise ValueError("individual research cannot use an ETF basket mapping")
            if (
                self.industry_opportunity_status == "NOT_APPLICABLE"
                or self.fundamental_role == "ETF_PROXY"
                or self.relative_value_status == "ETF_PROXY"
            ):
                raise ValueError("individual research statuses are invalid")
        elif self.path == "ETF_PROXY":
            if not self.basket_mapping_id:
                raise ValueError("ETF proxy research requires a point-in-time basket mapping")
            if (
                self.industry_opportunity_status != "NOT_APPLICABLE"
                or self.fundamental_role != "ETF_PROXY"
                or self.relative_value_status != "ETF_PROXY"
                or self.point_in_time_total_market_cap is not None
                or self.peer_set_id is not None
            ):
                raise ValueError("ETF proxy research statuses are invalid")
        else:
            raise ValueError("unsupported selection path")

    def visible_at(self, decision_time: datetime) -> bool:
        observed = normalize_datetime(decision_time, "decision_time")
        return self.known_at <= observed and self.effective_at <= observed <= self.valid_until

    def document(self) -> dict[str, object]:
        return {
            "snapshot_id": self.snapshot_id,
            "symbol": self.symbol,
            "path": self.path,
            "effective_at": self.effective_at.isoformat(),
            "known_at": self.known_at.isoformat(),
            "valid_until": self.valid_until.isoformat(),
            "reviewer": self.reviewer,
            "signature": self.signature,
            "official_evidence_ids": list(self.official_evidence_ids),
            "industry_opportunity_status": self.industry_opportunity_status,
            "fundamental_role": self.fundamental_role,
            "relative_value_status": self.relative_value_status,
            "point_in_time_total_market_cap": (
                None
                if self.point_in_time_total_market_cap is None
                else str(self.point_in_time_total_market_cap)
            ),
            "peer_set_id": self.peer_set_id,
            "basket_mapping_id": self.basket_mapping_id,
        }


def selection_research_snapshot_from_document(
    raw: object,
) -> SelectionResearchSnapshot:
    """从决策文档严格重建正式研究快照，不接受缺字段的旧格式。"""

    if not isinstance(raw, dict):
        raise ValueError("正式研究快照必须是对象")
    expected_fields = {
        "snapshot_id",
        "symbol",
        "path",
        "effective_at",
        "known_at",
        "valid_until",
        "reviewer",
        "signature",
        "official_evidence_ids",
        "industry_opportunity_status",
        "fundamental_role",
        "relative_value_status",
        "point_in_time_total_market_cap",
        "peer_set_id",
        "basket_mapping_id",
    }
    if set(raw) != expected_fields:
        raise ValueError("正式研究快照字段发生变化")
    evidence_ids = raw["official_evidence_ids"]
    if not isinstance(evidence_ids, list) or any(
        not isinstance(value, str) for value in evidence_ids
    ):
        raise ValueError("正式研究证据身份无效")
    market_cap_raw = raw["point_in_time_total_market_cap"]
    try:
        market_cap = None if market_cap_raw is None else Decimal(str(market_cap_raw))
        snapshot = SelectionResearchSnapshot(
            snapshot_id=str(raw["snapshot_id"]),
            symbol=str(raw["symbol"]),
            path=raw["path"],  # type: ignore[arg-type]
            effective_at=datetime.fromisoformat(str(raw["effective_at"])),
            known_at=datetime.fromisoformat(str(raw["known_at"])),
            valid_until=datetime.fromisoformat(str(raw["valid_until"])),
            reviewer=str(raw["reviewer"]),
            signature=str(raw["signature"]),
            official_evidence_ids=tuple(evidence_ids),
            industry_opportunity_status=raw["industry_opportunity_status"],  # type: ignore[arg-type]
            fundamental_role=raw["fundamental_role"],  # type: ignore[arg-type]
            relative_value_status=raw["relative_value_status"],  # type: ignore[arg-type]
            point_in_time_total_market_cap=market_cap,
            peer_set_id=(
                None if raw["peer_set_id"] is None else str(raw["peer_set_id"])
            ),
            basket_mapping_id=(
                None
                if raw["basket_mapping_id"] is None
                else str(raw["basket_mapping_id"])
            ),
        )
    except (ArithmeticError, TypeError, ValueError) as exc:
        raise ValueError("正式研究快照内容无效") from exc
    if snapshot.document() != raw:
        raise ValueError("正式研究快照不是规范格式")
    return snapshot


def selection_research_ledger_document(
    snapshots: tuple[SelectionResearchSnapshot, ...],
) -> dict[str, object]:
    """生成按时点有序、可直接供实时与回放共用的正式研究账本。"""

    normalized = _validate_selection_research_ledger(snapshots)
    return {
        "schema": SELECTION_RESEARCH_LEDGER_SCHEMA,
        "snapshots": [snapshot.document() for snapshot in normalized],
    }


def selection_research_ledger_from_document(
    raw: object,
) -> tuple[SelectionResearchSnapshot, ...]:
    """严格读取唯一正式研究账本，不接受缺字段或旧格式。"""

    if not isinstance(raw, dict) or set(raw) != {"schema", "snapshots"}:
        raise ValueError("正式研究账本字段发生变化")
    if raw.get("schema") != SELECTION_RESEARCH_LEDGER_SCHEMA:
        raise ValueError("正式研究账本协议不匹配")
    rows = raw.get("snapshots")
    if not isinstance(rows, list):
        raise ValueError("正式研究账本快照必须是列表")
    snapshots = tuple(selection_research_snapshot_from_document(row) for row in rows)
    normalized = _validate_selection_research_ledger(snapshots)
    if selection_research_ledger_document(normalized) != raw:
        raise ValueError("正式研究账本不是规范格式")
    return normalized


def _validate_selection_research_ledger(
    snapshots: tuple[SelectionResearchSnapshot, ...],
) -> tuple[SelectionResearchSnapshot, ...]:
    if type(snapshots) is not tuple:
        raise TypeError("正式研究账本必须使用元组")
    identities = tuple(snapshot.snapshot_id for snapshot in snapshots)
    if len(identities) != len(set(identities)):
        raise ValueError("正式研究快照身份必须唯一")
    order = tuple(
        (
            snapshot.symbol,
            snapshot.effective_at,
            snapshot.known_at,
            snapshot.snapshot_id,
        )
        for snapshot in snapshots
    )
    if order != tuple(sorted(order)):
        raise ValueError("正式研究快照必须按标的与时间排序")
    return snapshots


def selection_research_by_symbol(
    snapshots: tuple[SelectionResearchSnapshot, ...],
) -> dict[str, tuple[SelectionResearchSnapshot, ...]]:
    """把规范研究账本转换为历史回放所需的按标的索引。"""

    output: dict[str, list[SelectionResearchSnapshot]] = {}
    for snapshot in _validate_selection_research_ledger(snapshots):
        output.setdefault(snapshot.symbol, []).append(snapshot)
    return {symbol: tuple(values) for symbol, values in output.items()}


def visible_selection_research(
    snapshots: tuple[SelectionResearchSnapshot, ...],
    *,
    symbol: str,
    selection_path: SelectionPath,
    decision_time: datetime,
) -> SelectionResearchSnapshot | None:
    """返回指定时点唯一应生效的最新正式研究快照。"""

    visible = tuple(
        snapshot
        for snapshot in _validate_selection_research_ledger(snapshots)
        if snapshot.symbol == symbol
        and snapshot.path == selection_path
        and snapshot.visible_at(decision_time)
    )
    return (
        None
        if not visible
        else max(
            visible,
            key=lambda snapshot: (
                snapshot.effective_at,
                snapshot.known_at,
                snapshot.snapshot_id,
            ),
        )
    )


@dataclass(frozen=True, slots=True)
class FormalSelectionGate:
    """正式候选资格；它不改变任何技术买卖点，只约束新仓准入。"""

    selection_path: SelectionPath
    research_status: FormalSelectionStatus
    status: FormalSelectionStatus
    accepted: bool
    sector_trigger_required: bool
    sector_triggered: bool
    research_snapshot_id: str | None
    official_evidence_ids: tuple[str, ...]
    research_reason_codes: tuple[str, ...]
    reason_codes: tuple[str, ...]

    def __post_init__(self) -> None:
        for field in (
            "official_evidence_ids",
            "research_reason_codes",
            "reason_codes",
        ):
            values = tuple(getattr(self, field))
            object.__setattr__(self, field, values)
            if len(values) != len(set(values)):
                raise ValueError(f"{field} 必须唯一")
        expected_accepted = bool(
            self.research_status == "PASS"
            and (not self.sector_trigger_required or self.sector_triggered)
            and not self.reason_codes
        )
        if self.accepted is not expected_accepted:
            raise ValueError("正式候选资格与证据不一致")
        expected_status: FormalSelectionStatus = (
            "PASS"
            if expected_accepted
            else "REJECT"
            if self.research_status == "REJECT"
            else "UNRESOLVED"
        )
        if self.status != expected_status:
            raise ValueError("正式候选状态与研究状态不一致")

    def document(self) -> dict[str, object]:
        return {
            **asdict(self),
            "official_evidence_ids": list(self.official_evidence_ids),
            "research_reason_codes": list(self.research_reason_codes),
            "reason_codes": list(self.reason_codes),
        }


def evaluate_formal_selection_gate(
    snapshot: SelectionResearchSnapshot | None,
    *,
    symbol: str,
    decision_time: datetime,
    selection_path: SelectionPath,
    sector_triggered: bool,
) -> FormalSelectionGate:
    """用同一组点时研究规则评估实时、回放和选股的正式候选资格。"""

    decision = normalize_datetime(decision_time, "decision_time")
    research_status: FormalSelectionStatus = "UNRESOLVED"
    research_reasons: list[str] = []
    snapshot_id: str | None = None
    evidence_ids: tuple[str, ...] = ()
    if snapshot is None:
        research_reasons.append("SIGNED_SELECTION_RESEARCH_REQUIRED")
    else:
        snapshot_id = snapshot.snapshot_id
        evidence_ids = snapshot.official_evidence_ids
        if snapshot.symbol != symbol:
            research_status = "REJECT"
            research_reasons.append("SELECTION_RESEARCH_SYMBOL_MISMATCH")
        if snapshot.path != selection_path:
            research_status = "REJECT"
            research_reasons.append("SELECTION_RESEARCH_PATH_MISMATCH")
        if not snapshot.visible_at(decision):
            research_reasons.append("SELECTION_RESEARCH_NOT_VISIBLE_OR_EXPIRED")

        if not research_reasons:
            if selection_path == "INDIVIDUAL_THREE_PROGRAM":
                rejected = bool(
                    snapshot.industry_opportunity_status == "REJECT"
                    or snapshot.fundamental_role == "REJECT"
                    or snapshot.relative_value_status == "OVERVALUED"
                )
                unresolved = bool(
                    snapshot.industry_opportunity_status != "PASS"
                    or snapshot.fundamental_role
                    not in {"LEADER", "GROWTH_CHALLENGER"}
                    or snapshot.relative_value_status not in {"UNDERVALUED", "FAIR"}
                )
                if rejected:
                    research_status = "REJECT"
                    research_reasons.append("INDIVIDUAL_THREE_PROGRAM_REJECTED")
                elif unresolved:
                    research_reasons.append("INDIVIDUAL_THREE_PROGRAM_UNRESOLVED")
                else:
                    research_status = "PASS"
            else:
                valid_proxy = bool(
                    snapshot.industry_opportunity_status == "NOT_APPLICABLE"
                    and snapshot.fundamental_role == "ETF_PROXY"
                    and snapshot.relative_value_status == "ETF_PROXY"
                    and snapshot.basket_mapping_id
                )
                if valid_proxy:
                    research_status = "PASS"
                else:
                    research_status = "REJECT"
                    research_reasons.append("ETF_PROXY_RESEARCH_REJECTED")

    sector_required = selection_path == "INDIVIDUAL_THREE_PROGRAM"
    reasons = list(research_reasons)
    if sector_required and not sector_triggered:
        reasons.append("QMT_SECTOR_TRIGGER_REQUIRED")
    reason_codes = tuple(dict.fromkeys(reasons))
    accepted = research_status == "PASS" and not reason_codes
    status: FormalSelectionStatus = (
        "PASS"
        if accepted
        else "REJECT"
        if research_status == "REJECT"
        else "UNRESOLVED"
    )
    return FormalSelectionGate(
        selection_path=selection_path,
        research_status=research_status,
        status=status,
        accepted=accepted,
        sector_trigger_required=sector_required,
        sector_triggered=sector_triggered,
        research_snapshot_id=snapshot_id,
        official_evidence_ids=evidence_ids,
        research_reason_codes=tuple(dict.fromkeys(research_reasons)),
        reason_codes=reason_codes,
    )


def higher_timeframe_risk_gate(
    *,
    states: tuple[RiskState, RiskState, RiskState],
    completed_ma5_available: bool,
    mapping_unique: bool,
) -> RiskGate:
    """为决策器和校验器推导同一个冻结月周日门槛。"""

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
    # 已知且不唯一的活动顶映射由上方明确的 FORMED_UNRESOLVED 状态表示；若映射标志
    # 为假且没有此类事件，则表示适配器本身未能解析结构事实。
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
        if self.anchor_session > self.observed_at.date():
            raise ValueError("sector strength anchor cannot be in the future")
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
        else:
            if not isinstance(self.strength, Decimal) or not self.strength.is_finite():
                raise ValueError("resolved sector strength must be finite")
            if not Decimal("1") <= self.strength <= Decimal("9"):
                raise ValueError("resolved sector strength must be in [1, 9]")
            if type(self.rank) is not int or self.rank <= 0:
                raise ValueError("resolved sector rank must be a positive integer")

    @property
    def resolved(self) -> bool:
        return not self.unresolved_reasons


@dataclass(frozen=True, slots=True)
class CompletedDailyClose:
    session: date
    close: Decimal
    known_at: datetime
    completed: bool = True

    def __post_init__(self) -> None:
        object.__setattr__(self, "known_at", normalize_datetime(self.known_at, "known_at"))
        if type(self.completed) is not bool:
            raise TypeError("daily close completed flag must be a bool")
        if not isinstance(self.close, Decimal) or not self.close.is_finite():
            raise ValueError("daily close must be a finite Decimal")
        if self.close <= 0:
            raise ValueError("daily close must be positive")
        if self.completed and self.session > self.known_at.date():
            raise ValueError("completed daily close cannot be known before its session")


def completed_ma5_at(
    rows: tuple[CompletedDailyClose, ...],
    *,
    decision_time: datetime,
) -> Decimal | None:
    """根据时点可见的最近五个已完成收盘价返回五日均线。"""

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
    if anchor_session > decision.date():
        raise ValueError("sector strength anchor cannot be in the future")
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
    if anchor_session > decision.date():
        raise ValueError("sector strength anchor cannot be in the future")
    if rank is not None and (type(rank) is not int or rank <= 0):
        raise ValueError("sector strength rank must be a positive integer")
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
    "CompletedDailyClose",
    "FormalSelectionGate",
    "FormalSelectionStatus",
    "HIGHER_TIMEFRAME_RISK_STATES",
    "HigherTimeframeRiskSnapshot",
    "SELECTION_RESEARCH_LEDGER_SCHEMA",
    "SectorMemberHistory",
    "SectorStrengthSnapshot",
    "SelectionResearchSnapshot",
    "TopRiskTransition",
    "advance_top_risk_state",
    "build_sector_strength_snapshot",
    "completed_sma",
    "completed_ma5_at",
    "evaluate_formal_selection_gate",
    "higher_timeframe_risk_gate",
    "member_ma_strength_category",
    "selection_research_snapshot_from_document",
    "selection_research_by_symbol",
    "selection_research_ledger_document",
    "selection_research_ledger_from_document",
    "visible_selection_research",
]
