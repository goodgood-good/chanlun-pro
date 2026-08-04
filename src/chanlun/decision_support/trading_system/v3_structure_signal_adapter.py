from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime
from decimal import Decimal
from typing import Literal, Mapping, Sequence

from chanlun.core.strict_structure.models import TrendType
from chanlun.decision_support.fingerprints import normalize_datetime, sha256_json
from chanlun.decision_support.trading_system.models import StructuralPoint
from chanlun.decision_support.trading_system.v3_decision import SystemHealthFacts


STRUCTURE_SIGNAL_LEDGER_SCHEMA = (
    "chanlun-v3-frozen-structure-signal-ledger/v1"
)
_ALLOWED_PHASES = frozenset(
    {
        "BUILDING",
        "OSCILLATION",
        "UPMOVE",
        "DOWNMOVE",
        "EXPANSION_RECLASSIFYING",
    }
)
V3_REQUIRED_STRUCTURE_RULES = (
    "L0_THIRD_SELL",
    "FIRST_UP_LEG_FAILED",
    "SECOND_SELL_CONFIRM",
    "L0_UPMOVE_DIVERGENCE",
    "L1_THIRD_BUY_PROTECTION",
    "L1_THIRD_SELL",
    "THIRD_SELL_RECOVERY",
    "ORDINARY_TACTICAL_SELL",
    "ORDINARY_TACTICAL_BUYBACK",
)


def _valid_sha256(value: str) -> bool:
    if len(value) != 71 or not value.startswith("sha256:"):
        return False
    try:
        int(value[7:], 16)
    except ValueError:
        return False
    return True


@dataclass(frozen=True, slots=True)
class SignalAdapterDiagnostic:
    severity: Literal["INFO", "UNRESOLVED", "ERROR"]
    code: str
    detail: str
    point_id: str | None = None


@dataclass(frozen=True, slots=True)
class FrozenCompletedTrendFact:
    """Read-only copy of one completed physical or recursive trend."""

    trend_id: str
    source_frequency: Literal["30m", "5m", "1m"]
    recursive_level: int
    price_basis_revision: str
    direction: Literal["up", "down"]
    market_start: datetime
    market_end: datetime
    confirmed_at: datetime
    available_at: datetime
    source_fact_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        for field in (
            "market_start",
            "market_end",
            "confirmed_at",
            "available_at",
        ):
            object.__setattr__(
                self,
                field,
                normalize_datetime(getattr(self, field), field),
            )
        object.__setattr__(self, "source_fact_ids", tuple(self.source_fact_ids))
        if not self.trend_id or not self.price_basis_revision:
            raise ValueError("completed trend audit identity is required")
        if self.recursive_level < 0:
            raise ValueError("completed trend recursive level must be non-negative")
        if not (
            self.market_start
            <= self.market_end
            <= self.confirmed_at
            <= self.available_at
        ):
            raise ValueError("completed trend times are not causal")
        if not self.source_fact_ids or len(self.source_fact_ids) != len(
            set(self.source_fact_ids)
        ):
            raise ValueError("completed trend source facts must be nonempty and unique")


def frozen_completed_trend_fact(
    trend: TrendType,
    *,
    source_frequency: Literal["30m", "5m", "1m"],
) -> FrozenCompletedTrendFact:
    """Copy immutable facts without changing or wrapping the frozen core object."""

    if not trend.complete:
        raise ValueError("only completed trends apply")
    if trend.confirmed_at is None:
        raise ValueError("completed trend must carry confirmation time")
    return FrozenCompletedTrendFact(
        trend_id=trend.trend_id,
        source_frequency=source_frequency,
        recursive_level=trend.structural_level,
        price_basis_revision=trend.price_basis_revision,
        direction=trend.direction,
        market_start=trend.market_start,
        market_end=trend.market_end,
        confirmed_at=trend.confirmed_at,
        available_at=trend.available_at,
        source_fact_ids=tuple(
            dict.fromkeys(
                (
                    trend.trend_id,
                    *(center.center_id for center in trend.centers),
                    *(unit.unit_id for unit in trend.constituent_units),
                )
            )
        ),
    )


@dataclass(frozen=True, slots=True)
class FrozenCenterPhaseFact:
    """Point-in-time L1 phase copied from one frozen causal center snapshot."""

    center_id: str
    source_frequency: str
    recursive_level: int
    phase: Literal[
        "BUILDING",
        "OSCILLATION",
        "UPMOVE",
        "DOWNMOVE",
        "EXPANSION_RECLASSIFYING",
    ]
    available_at: datetime
    structure_snapshot_id: str
    source_fact_ids: tuple[str, ...]
    complete: bool = True

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "available_at",
            normalize_datetime(self.available_at, "available_at"),
        )
        object.__setattr__(self, "source_fact_ids", tuple(self.source_fact_ids))
        valid_level_identity = (
            (self.source_frequency == "5m" and self.recursive_level == 0)
            or (self.source_frequency == "1m" and self.recursive_level == 1)
        )
        if (
            not self.center_id
            or not self.structure_snapshot_id
            or not valid_level_identity
            or self.phase not in _ALLOWED_PHASES
        ):
            raise ValueError("L1 center phase identity is invalid")
        if not self.source_fact_ids or len(self.source_fact_ids) != len(
            set(self.source_fact_ids)
        ):
            raise ValueError("center phase source facts must be nonempty and unique")


Qualification = Literal["PASS", "FAIL", "UNRESOLVED"]


@dataclass(frozen=True, slots=True)
class FrozenSignalExecutionFact:
    """Explicit non-structural facts needed to promote one structural signal.

    The adapter never derives these values from prices or later account state.
    A missing envelope leaves the structural observation in the audit ledger but
    makes it ineligible for a replay order event.
    """

    signal_point_id: str
    known_at: datetime
    account_snapshot_id: str
    health: SystemHealthFacts
    price_cap_or_floor: Decimal
    boundary_fact_id: str
    boundary_point_id: str
    locator_point_id: str | None = None
    risk_fact_ids: tuple[str, ...] = ()
    source_fact_ids: tuple[str, ...] = ()
    q_liquidity_cap: int = 0
    broker_sellable_tactical_qty: int = 0
    cash_affordable_buyback_qty: int = 0
    l2_reached_required_half: bool | None = None
    zn_at_or_above_a: bool | None = None
    higher_timeframe_allows_ordinary_buyback: bool | None = None
    higher_timeframe_allows_third_sell_recovery: bool | None = None
    tactical_adaptation: Qualification = "UNRESOLVED"
    every_partial_prefix_edge: Qualification = "UNRESOLVED"

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "known_at",
            normalize_datetime(self.known_at, "known_at"),
        )
        object.__setattr__(self, "risk_fact_ids", tuple(self.risk_fact_ids))
        object.__setattr__(self, "source_fact_ids", tuple(self.source_fact_ids))
        if (
            not self.signal_point_id
            or not self.account_snapshot_id
            or not self.boundary_fact_id
            or not self.boundary_point_id
            or self.price_cap_or_floor <= 0
        ):
            raise ValueError("signal execution fact identity/boundary is invalid")
        if any(
            value < 0
            for value in (
                self.q_liquidity_cap,
                self.broker_sellable_tactical_qty,
                self.cash_affordable_buyback_qty,
            )
        ):
            raise ValueError("signal execution quantities cannot be negative")
        if len(self.risk_fact_ids) != len(set(self.risk_fact_ids)) or len(
            self.source_fact_ids
        ) != len(set(self.source_fact_ids)):
            raise ValueError("signal execution source IDs must be unique")


@dataclass(frozen=True, slots=True)
class V3StructureSignalLedger:
    symbol: str
    coverage: Mapping[str, object]
    structure_signal_facts: tuple[Mapping[str, object], ...]
    diagnostics: tuple[SignalAdapterDiagnostic, ...]
    rule_coverage: Mapping[str, str]
    source_ledger_sha256: str

    def document(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "schema": STRUCTURE_SIGNAL_LEDGER_SCHEMA,
            "symbol": self.symbol,
            "source_ledger_sha256": self.source_ledger_sha256,
            "structure_coverage": dict(self.coverage),
            "rule_coverage": dict(self.rule_coverage),
            "structure_signal_facts": tuple(
                dict(value) for value in self.structure_signal_facts
            ),
            "diagnostics": tuple(asdict(value) for value in self.diagnostics),
            "highest_status": "RESEARCH_ONLY",
            "live_status": "LIVE_DISABLED",
        }
        payload["content_sha256"] = sha256_json(payload)
        return payload


def _validate_points(
    symbol: str,
    source_frequency: str,
    recursive_level: int,
    points: Sequence[StructuralPoint],
) -> tuple[StructuralPoint, ...]:
    values = tuple(
        sorted(points, key=lambda value: (value.available_at, value.point_id))
    )
    if len({value.point_id for value in values}) != len(values):
        raise ValueError("structural point IDs must be unique")
    for point in values:
        if (
            point.code != symbol
            or point.source_frequency != source_frequency
            or point.recursive_level != recursive_level
            or point.tower != "formal"
            or not point.confirmed
            or point.confirmed_at is None
        ):
            raise ValueError("signal adapter received an incompatible point")
    return values


def _latest_phase(
    phases: tuple[FrozenCenterPhaseFact, ...],
    observed_at: datetime,
) -> FrozenCenterPhaseFact | None:
    eligible = tuple(
        value
        for value in phases
        if value.complete and value.available_at <= observed_at
    )
    if not eligible:
        return None
    latest_at = max(value.available_at for value in eligible)
    latest = tuple(value for value in eligible if value.available_at == latest_at)
    if len(latest) != 1:
        return None
    return latest[0]


def _matching_trend(
    point: StructuralPoint,
    trends: tuple[FrozenCompletedTrendFact, ...],
) -> tuple[FrozenCompletedTrendFact | None, str | None]:
    expected = "up" if point.side == "sell" else "down"
    matching = tuple(
        value
        for value in trends
        if value.source_frequency == point.source_frequency
        and value.recursive_level == point.recursive_level
        and value.price_basis_revision == point.price_basis_revision
        and value.direction == expected
        and value.market_end == point.anchor_at
        and value.confirmed_at <= point.available_at
        and value.available_at <= point.available_at
    )
    if not matching:
        return None, "UNRESOLVED_COMPLETED_TREND_LINK_MISSING"
    if len(matching) != 1:
        return None, "UNRESOLVED_COMPLETED_TREND_LINK_NONUNIQUE"
    return matching[0], None


def _point_proof(
    point: StructuralPoint,
    *,
    points_by_id: Mapping[str, StructuralPoint],
    trends: tuple[FrozenCompletedTrendFact, ...],
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    if point.point_type in {"1buy", "1sell"}:
        trend, reason = _matching_trend(point, trends)
        return (
            () if trend is None else trend.source_fact_ids,
            () if reason is None else (reason,),
        )
    if point.point_type in {"2buy", "2sell"}:
        parent = (
            None
            if point.parent_point_id is None
            else points_by_id.get(point.parent_point_id)
        )
        expected_parent = "1buy" if point.side == "buy" else "1sell"
        reasons: list[str] = []
        if (
            parent is None
            or parent.point_type != expected_parent
            or parent.available_at > point.available_at
        ):
            reasons.append("UNRESOLVED_SECOND_POINT_PARENT_LINK")
        required_codes = {
            "complete_adjacent_rebound",
            "complete_first_pullback",
        }
        if not required_codes.issubset(set(point.evidence_codes)):
            reasons.append("UNRESOLVED_SECOND_POINT_COMPLETION_EVIDENCE")
        return (
            () if parent is None else (parent.point_id,),
            tuple(reasons),
        )
    return (), ()


def _locator(
    point: StructuralPoint,
    execution: FrozenSignalExecutionFact | None,
    l2_by_id: Mapping[str, StructuralPoint],
) -> tuple[StructuralPoint | None, tuple[str, ...]]:
    if execution is None or execution.locator_point_id is None:
        return None, ("UNRESOLVED_L1_SIGNAL_L2_LOCATOR_MISSING",)
    locator = l2_by_id.get(execution.locator_point_id)
    expected_side = point.side
    if (
        locator is None
        or locator.side != expected_side
        or locator.point_type not in {"1buy", "2buy", "1sell", "2sell"}
        or locator.available_at > point.available_at
        or execution.boundary_point_id != locator.point_id
    ):
        return None, ("UNRESOLVED_L1_SIGNAL_L2_LOCATOR_INVALID",)
    return locator, ()


def build_v3_structure_signal_ledger(
    *,
    symbol: str,
    l0_points: Sequence[StructuralPoint],
    l1_points: Sequence[StructuralPoint],
    l2_points: Sequence[StructuralPoint],
    completed_trends: Sequence[FrozenCompletedTrendFact],
    l1_center_phases: Sequence[FrozenCenterPhaseFact],
    execution_facts: Sequence[FrozenSignalExecutionFact],
    coverage_start: datetime,
    coverage_end: datetime,
    source_ledger_sha256: str,
    level_relation_mode: Literal[
        "DIRECT_RECURSIVE",
        "USER_OVERRIDE_INDEPENDENT_TIMEFRAMES",
    ] = "USER_OVERRIDE_INDEPENDENT_TIMEFRAMES",
) -> V3StructureSignalLedger:
    """Map frozen facts to V3 signals without deriving a second signal system."""

    start = normalize_datetime(coverage_start, "coverage_start")
    end = normalize_datetime(coverage_end, "coverage_end")
    if start > end:
        raise ValueError("structure signal coverage is reversed")
    if not symbol or not _valid_sha256(source_ledger_sha256):
        raise ValueError("structure signal ledger identity is invalid")
    if level_relation_mode == "DIRECT_RECURSIVE":
        identities = (("1m", 2), ("1m", 1), ("1m", 0))
    elif level_relation_mode == "USER_OVERRIDE_INDEPENDENT_TIMEFRAMES":
        identities = (("30m", 0), ("5m", 0), ("1m", 0))
    else:
        raise ValueError("unsupported structure signal level relation mode")
    l0 = _validate_points(symbol, *identities[0], l0_points)
    l1 = _validate_points(symbol, *identities[1], l1_points)
    l2 = _validate_points(symbol, *identities[2], l2_points)
    all_points = (*l0, *l1, *l2)
    if any(not start <= value.available_at <= end for value in all_points):
        raise ValueError("structural point lies outside certified coverage")
    point_by_id = {value.point_id: value for value in all_points}
    if len(point_by_id) != len(all_points):
        raise ValueError("structural point identity crosses frequencies")

    trends = tuple(
        sorted(
            completed_trends,
            key=lambda value: (
                value.available_at,
                value.source_frequency,
                value.trend_id,
            ),
        )
    )
    if len({(value.source_frequency, value.trend_id) for value in trends}) != len(
        trends
    ):
        raise ValueError("completed trend identities must be unique")
    if any(not start <= value.available_at <= end for value in trends):
        raise ValueError("completed trend lies outside certified coverage")
    expected_trend_identities = set(identities)
    if any(
        (value.source_frequency, value.recursive_level)
        not in expected_trend_identities
        for value in trends
    ):
        raise ValueError("completed trend crossed the level relation contract")
    phases = tuple(
        sorted(
            l1_center_phases,
            key=lambda value: (value.available_at, value.structure_snapshot_id),
        )
    )
    if len({value.structure_snapshot_id for value in phases}) != len(phases):
        raise ValueError("center phase snapshot identities must be unique")
    execution_by_point = {value.signal_point_id: value for value in execution_facts}
    if len(execution_by_point) != len(tuple(execution_facts)):
        raise ValueError("signal execution facts must be unique by point")
    unknown_execution = set(execution_by_point) - set(point_by_id)
    if unknown_execution:
        raise ValueError("signal execution fact references an unknown point")

    diagnostics: list[SignalAdapterDiagnostic] = [
        SignalAdapterDiagnostic(
            "UNRESOLVED",
            "UNRESOLVED_FIRST_UP_LEG_FAILED_CYCLE_RELATIVE_FACTS",
            "a completed trend alone cannot prove the entry-relative departure-high comparison",
        ),
        SignalAdapterDiagnostic(
            "UNRESOLVED",
            "UNRESOLVED_SECOND_SELL_CONFIRM_HALF_CYCLE_FACTS",
            "the half-position down/up monitor and non-new-high comparison are absent",
        ),
        SignalAdapterDiagnostic(
            "UNRESOLVED",
            "UNRESOLVED_L0_UPMOVE_DIVERGENCE_CYCLE_STATE_BINDING",
            "a generic sell point cannot be promoted to the V3 full-position reduce rule",
        ),
    ]
    rule_coverage = {name: "COMPLETE" for name in V3_REQUIRED_STRUCTURE_RULES}
    rule_coverage.update(
        {
            "FIRST_UP_LEG_FAILED": "UNRESOLVED",
            "SECOND_SELL_CONFIRM": "UNRESOLVED",
            "L0_UPMOVE_DIVERGENCE": "UNRESOLVED",
        }
    )
    signal_rows: list[dict[str, object]] = []
    l2_by_id = {value.point_id: value for value in l2}
    last_l1_third_sell: StructuralPoint | None = None

    def append_row(
        point: StructuralPoint,
        *,
        signal_kind: str,
        strategic: Mapping[str, object] | None = None,
        tactical: Mapping[str, object] | None = None,
        persistence: Literal["OPTIONAL", "PERSISTENT_EXIT", "NONE"],
        source_frequencies: tuple[str, ...],
        proof_ids: tuple[str, ...] = (),
        reasons: tuple[str, ...] = (),
        emit_to_replay: bool = True,
        locator: StructuralPoint | None = None,
    ) -> None:
        execution = execution_by_point.get(point.point_id)
        local_reasons = list(reasons)
        if execution is None:
            local_reasons.append("UNRESOLVED_SIGNAL_EXECUTION_ENVELOPE_MISSING")
        elif execution.known_at > point.available_at:
            local_reasons.append("UNRESOLVED_SIGNAL_EXECUTION_FACT_NOT_YET_KNOWN")
        elif execution.boundary_point_id != (
            point.point_id if locator is None else locator.point_id
        ):
            local_reasons.append("UNRESOLVED_SIGNAL_BOUNDARY_POINT_BINDING")
        fact_ids = tuple(
            dict.fromkeys(
                (
                    point.point_id,
                    *point.evidence_codes,
                    *proof_ids,
                    *(() if locator is None else (locator.point_id,)),
                    *(
                        ()
                        if execution is None
                        else (
                            execution.boundary_fact_id,
                            *execution.source_fact_ids,
                        )
                    ),
                )
            )
        )
        snapshot_id = sha256_json(
            {
                "schema": STRUCTURE_SIGNAL_LEDGER_SCHEMA,
                "signal_kind": signal_kind,
                "point_id": point.point_id,
                "decision_time": point.available_at,
                "facts": fact_ids,
            }
        )
        resolved = not local_reasons
        event_id = sha256_json(
            {
                "schema": "chanlun-v3-frozen-structure-signal-event/v1",
                "symbol": symbol,
                "signal_kind": signal_kind,
                "point_id": point.point_id,
                "decision_time": point.available_at,
            }
        )
        row: dict[str, object] = {
            "event_id": event_id,
            "signal_kind": signal_kind,
            "symbol": symbol,
            "decision_time": point.available_at.isoformat(),
            "confirmation_time": point.confirmed_at.isoformat(),
            "structure_snapshot_id": snapshot_id,
            "account_snapshot_id": (
                "UNRESOLVED"
                if execution is None
                else execution.account_snapshot_id
            ),
            "completed": True,
            "recursive_level": 0,
            "source_frequencies": source_frequencies,
            "strategic": dict(strategic or {}),
            "tactical": dict(tactical or {}),
            "health": (
                asdict(
                    SystemHealthFacts(
                        data_complete=False,
                        broker_healthy=False,
                        reconciliation_passed=False,
                        timestamps_monotonic=False,
                        account_transfer_registered=False,
                    )
                )
                if execution is None
                else asdict(execution.health)
            ),
            "price_cap_or_floor": (
                None
                if execution is None
                else format(execution.price_cap_or_floor, "f")
            ),
            "frozen_structure_fact_ids": fact_ids,
            "risk_fact_ids": (
                () if execution is None else execution.risk_fact_ids
            ),
            "execution_persistence": persistence,
            "source_ledger_sha256": source_ledger_sha256,
            "all_required_facts_resolved": resolved,
            "unresolved_reason_codes": tuple(dict.fromkeys(local_reasons)),
            "emit_to_replay": emit_to_replay and resolved,
        }
        signal_rows.append(row)
        diagnostics.extend(
            SignalAdapterDiagnostic(
                "UNRESOLVED",
                reason,
                signal_kind,
                point.point_id,
            )
            for reason in dict.fromkeys(local_reasons)
        )

    for point in l0:
        if point.point_type == "3sell":
            append_row(
                point,
                signal_kind="L0_THIRD_SELL",
                strategic={"l0_third_sell": True},
                persistence="PERSISTENT_EXIT",
                source_frequencies=("30m",),
            )
        elif point.point_type in {"1sell", "2sell"}:
            append_row(
                point,
                signal_kind="UNRESOLVED_L0_SELL_POINT_CLASSIFICATION",
                persistence="NONE",
                source_frequencies=("30m",),
                reasons=(
                    "UNRESOLVED_L0_SELL_POINT_REQUIRES_STRATEGIC_CYCLE_CONTEXT",
                ),
                emit_to_replay=False,
            )

    for point in l1:
        execution = execution_by_point.get(point.point_id)
        if point.point_type in {"3buy", "3sell"}:
            locator, locator_reasons = _locator(point, execution, l2_by_id)
            is_buy = point.point_type == "3buy"
            tactical = {
                "l1_phase": "UPMOVE" if is_buy else "DOWNMOVE",
                "l1_third_buy": is_buy,
                "l1_third_sell": not is_buy,
                "q_liquidity_cap": (
                    0 if execution is None else execution.q_liquidity_cap
                ),
                "broker_sellable_tactical_qty": (
                    0
                    if execution is None
                    else execution.broker_sellable_tactical_qty
                ),
                "cash_affordable_buyback_qty": (
                    0
                    if execution is None
                    else execution.cash_affordable_buyback_qty
                ),
            }
            reasons = list(locator_reasons)
            if is_buy and (execution is None or not execution.risk_fact_ids):
                reasons.append("UNRESOLVED_POINT_IN_TIME_RISK_FACTS")
            append_row(
                point,
                signal_kind=(
                    "L1_THIRD_BUY_PROTECTION" if is_buy else "L1_THIRD_SELL"
                ),
                tactical=tactical,
                persistence="OPTIONAL" if is_buy else "PERSISTENT_EXIT",
                source_frequencies=("5m", "1m"),
                reasons=tuple(reasons),
                locator=locator,
            )
            if not is_buy:
                last_l1_third_sell = point
            continue
        if point.point_type not in {"1buy", "2buy"}:
            continue
        proof, proof_reasons = _point_proof(
            point,
            points_by_id=point_by_id,
            trends=trends,
        )
        locator, locator_reasons = _locator(point, execution, l2_by_id)
        if last_l1_third_sell is None or last_l1_third_sell.available_at >= point.available_at:
            append_row(
                point,
                signal_kind="L1_BUY_WITHOUT_PRIOR_THIRD_SELL",
                persistence="NONE",
                source_frequencies=("5m",),
                proof_ids=proof,
                reasons=proof_reasons,
                emit_to_replay=False,
            )
            continue
        reasons = [*proof_reasons, *locator_reasons]
        if execution is None or execution.higher_timeframe_allows_third_sell_recovery is None:
            reasons.append("UNRESOLVED_HIGHER_TIMEFRAME_RECOVERY_POLICY")
        if execution is None or not execution.risk_fact_ids:
            reasons.append("UNRESOLVED_POINT_IN_TIME_RISK_FACTS")
        append_row(
            point,
            signal_kind="THIRD_SELL_RECOVERY",
            tactical={
                "l1_phase": "DOWNMOVE",
                "third_sell_recovery_first_or_second_buy": True,
                "higher_timeframe_allows_third_sell_recovery": (
                    False
                    if execution is None
                    or execution.higher_timeframe_allows_third_sell_recovery
                    is None
                    else execution.higher_timeframe_allows_third_sell_recovery
                ),
                "q_liquidity_cap": (
                    0 if execution is None else execution.q_liquidity_cap
                ),
                "cash_affordable_buyback_qty": (
                    0
                    if execution is None
                    else execution.cash_affordable_buyback_qty
                ),
            },
            persistence="OPTIONAL",
            source_frequencies=("5m", "1m"),
            proof_ids=(last_l1_third_sell.point_id, *proof),
            reasons=tuple(reasons),
            locator=locator,
        )

    for point in l2:
        if point.point_type not in {"1buy", "2buy", "1sell", "2sell"}:
            continue
        execution = execution_by_point.get(point.point_id)
        proof, proof_reasons = _point_proof(
            point,
            points_by_id=point_by_id,
            trends=trends,
        )
        phase = _latest_phase(phases, point.available_at)
        if phase is None:
            phase_reasons = ("UNRESOLVED_L1_CENTER_PHASE_AT_SIGNAL",)
            phase_name = "BUILDING"
            emit = True
            phase_ids: tuple[str, ...] = ()
        elif phase.phase != "OSCILLATION":
            phase_reasons = ()
            phase_name = phase.phase
            emit = False
            phase_ids = (phase.structure_snapshot_id, *phase.source_fact_ids)
        else:
            phase_reasons = ()
            phase_name = phase.phase
            emit = True
            phase_ids = (phase.structure_snapshot_id, *phase.source_fact_ids)
        reasons = [*proof_reasons, *phase_reasons]
        is_sell = point.side == "sell"
        if execution is None or execution.l2_reached_required_half is None:
            reasons.append("UNRESOLVED_L2_REQUIRED_HALF_GEOMETRY")
        if execution is None or not execution.risk_fact_ids:
            reasons.append("UNRESOLVED_POINT_IN_TIME_RISK_FACTS")
        if is_sell:
            if execution is None or execution.tactical_adaptation == "UNRESOLVED":
                reasons.append("UNRESOLVED_TACTICAL_ADAPTATION_20_PAIR_FACTS")
            tactical = {
                "l1_phase": phase_name,
                "ordinary_sell_signal": True,
                "l2_signal_confirmed": True,
                "l2_reached_required_half": (
                    False
                    if execution is None
                    or execution.l2_reached_required_half is None
                    else execution.l2_reached_required_half
                ),
                "tactical_adaptation_passed": (
                    False
                    if execution is None
                    else execution.tactical_adaptation == "PASS"
                ),
                "broker_sellable_tactical_qty": (
                    0
                    if execution is None
                    else execution.broker_sellable_tactical_qty
                ),
                "q_liquidity_cap": (
                    0 if execution is None else execution.q_liquidity_cap
                ),
            }
            kind = "ORDINARY_TACTICAL_SELL"
        else:
            if execution is None or execution.zn_at_or_above_a is None:
                reasons.append("UNRESOLVED_ZN_AT_OR_ABOVE_A")
            if (
                execution is None
                or execution.higher_timeframe_allows_ordinary_buyback is None
            ):
                reasons.append("UNRESOLVED_HIGHER_TIMEFRAME_BUYBACK_POLICY")
            if (
                execution is None
                or execution.every_partial_prefix_edge == "UNRESOLVED"
            ):
                reasons.append("UNRESOLVED_EVERY_PARTIAL_PREFIX_EDGE_FACTS")
            tactical = {
                "l1_phase": phase_name,
                "ordinary_buyback_signal": True,
                "l2_signal_confirmed": True,
                "l2_reached_required_half": (
                    False
                    if execution is None
                    or execution.l2_reached_required_half is None
                    else execution.l2_reached_required_half
                ),
                "zn_at_or_above_a": (
                    False
                    if execution is None or execution.zn_at_or_above_a is None
                    else execution.zn_at_or_above_a
                ),
                "higher_timeframe_allows_ordinary_buyback": (
                    False
                    if execution is None
                    or execution.higher_timeframe_allows_ordinary_buyback is None
                    else execution.higher_timeframe_allows_ordinary_buyback
                ),
                "every_partial_prefix_edge_passed": (
                    False
                    if execution is None
                    else execution.every_partial_prefix_edge == "PASS"
                ),
                "q_liquidity_cap": (
                    0 if execution is None else execution.q_liquidity_cap
                ),
                "cash_affordable_buyback_qty": (
                    0
                    if execution is None
                    else execution.cash_affordable_buyback_qty
                ),
            }
            kind = "ORDINARY_TACTICAL_BUYBACK"
        append_row(
            point,
            signal_kind=kind,
            tactical=tactical,
            persistence="OPTIONAL",
            source_frequencies=("1m",),
            proof_ids=(*proof, *phase_ids),
            reasons=tuple(reasons),
            emit_to_replay=emit,
            locator=point,
        )

    signal_rows.sort(
        key=lambda value: (
            str(value["decision_time"]),
            str(value["event_id"]),
        )
    )
    rule_complete = all(value == "COMPLETE" for value in rule_coverage.values())
    coverage = {
        "symbol": symbol,
        "start_at": start.isoformat(),
        "end_at": end.isoformat(),
        "frequencies": ("30m", "5m", "1m"),
        "raw_recursive_levels": (
            {"L0": 2, "L1": 1, "L2": 0}
            if level_relation_mode == "DIRECT_RECURSIVE"
            else {"L0": 0, "L1": 0, "L2": 0}
        ),
        "level_relation_mode": level_relation_mode,
        "complete": rule_complete,
        "source_ledger_sha256": source_ledger_sha256,
        "rule_coverage": dict(rule_coverage),
        "missing_data_was_inferred": False,
    }
    return V3StructureSignalLedger(
        symbol=symbol,
        coverage=coverage,
        structure_signal_facts=tuple(signal_rows),
        diagnostics=tuple(diagnostics),
        rule_coverage=rule_coverage,
        source_ledger_sha256=source_ledger_sha256,
    )


__all__ = [
    "FrozenCenterPhaseFact",
    "FrozenCompletedTrendFact",
    "FrozenSignalExecutionFact",
    "STRUCTURE_SIGNAL_LEDGER_SCHEMA",
    "SignalAdapterDiagnostic",
    "V3StructureSignalLedger",
    "V3_REQUIRED_STRUCTURE_RULES",
    "build_v3_structure_signal_ledger",
    "frozen_completed_trend_fact",
]
