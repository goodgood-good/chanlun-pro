"""统一决策核心内部使用的技术信号评估阶段。

本模块只保存结构包、评估结果和私有技术评估器；生产调用方必须通过
``HumanAssistedDecisionCore``，从而保证正式研究、板块触发与技术结构使用同一入口。
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timedelta

from chanlun.decision_support.fingerprints import normalize_datetime
from chanlun.decision_support.trading_system.conflicts import resolve_conflict
from chanlun.decision_support.trading_system.context import classify_context
from chanlun.decision_support.trading_system.execution_policy import (
    evaluate_entry_policy,
    evaluate_exit_policy,
)
from chanlun.decision_support.trading_system.lifecycle import (
    advance_lifecycle,
    build_setup,
    match_one_minute_trigger,
)
from chanlun.decision_support.trading_system.higher_timeframe_gate import (
    HigherTimeframeGateBundle,
    HigherTimeframePeriodDiagnostic,
)
from chanlun.decision_support.trading_system.models import (
    ConflictDecision,
    ContextDirection,
    EntryExecutionBoundary,
    EntryDecision,
    ExitDecision,
    SectorAssessment,
    SignalLifecycle,
    StructuralPoint,
    StructureTower,
    TimeframeContext,
    TradeSetup,
    TradingPolicy,
)
from chanlun.decision_support.trading_system.provisional import ProvisionalCandidate
from chanlun.decision_support.trading_system.parameters import SelectionPath
from chanlun.decision_support.trading_system.screening_warmup import (
    SCREENING_WARMUP_DIFFERENCE_CODES,
    SCREENING_WARMUP_FREQUENCIES,
)
from chanlun.decision_support.trading_system.selection import (
    FormalSelectionGate,
    SelectionResearchSnapshot,
)


@dataclass(frozen=True, slots=True)
class SymbolStructureBundle:
    code: str
    as_of: datetime
    sector: SectorAssessment
    thirty_direction: ContextDirection
    thirty_points: tuple[StructuralPoint, ...]
    five_points: tuple[StructuralPoint | ProvisionalCandidate, ...]
    one_points: tuple[StructuralPoint, ...]
    opposite_points: tuple[StructuralPoint, ...]
    daily_direction: ContextDirection = "neutral"
    daily_points: tuple[StructuralPoint, ...] = ()
    held_tower: StructureTower | None = None
    held_level: int | None = None
    higher_timeframe_gates: HigherTimeframeGateBundle | None = None
    enforce_higher_timeframe_entry_gate: bool = False
    warmup_converged: bool = True
    warmup_reason_codes: tuple[str, ...] = ()
    warmup_by_frequency: tuple[tuple[str, bool, int, int], ...] = ()
    warmup_difference_codes_by_frequency: tuple[
        tuple[str, tuple[str, ...]], ...
    ] = ()
    enforce_warmup_entry_gate: bool = False
    physical_timeframe_recursive: bool = False
    entry_execution_boundaries: tuple[EntryExecutionBoundary, ...] = ()
    selection_sources: tuple[str, ...] = ()
    selection_path: SelectionPath = "INDIVIDUAL_THREE_PROGRAM"
    selection_research: SelectionResearchSnapshot | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "as_of", normalize_datetime(self.as_of, "as_of"))
        if not isinstance(self.code, str) or not self.code.strip():
            raise ValueError("结构包标的不能为空")
        frequency_points = (
            ("日线", "d", self.daily_points),
            ("30 分钟", "30m", self.thirty_points),
            ("5 分钟", "5m", self.five_points),
            ("1 分钟", "1m", self.one_points),
        )
        all_point_groups = (
            *((name, points) for name, _frequency, points in frequency_points),
            ("反向证据", self.opposite_points),
        )
        for name, points in all_point_groups:
            if any(point.code != self.code for point in points):
                raise ValueError(f"{name}买卖点标的与结构包标的不一致")
            identities = tuple(
                point.candidate_id
                if isinstance(point, ProvisionalCandidate)
                else point.point_id
                for point in points
            )
            if len(identities) != len(set(identities)):
                raise ValueError(f"{name}买卖点身份不能重复")
            if any(_point_time(point) > self.as_of for point in points):
                raise ValueError(f"{name}买卖点证据不能晚于结构包决策时点")
            if any(
                isinstance(point, StructuralPoint) and not point.confirmed
                for point in points
            ):
                raise ValueError(f"{name}正式买卖点必须已经确认")
        if any(
            point.source_frequency != frequency
            for _name, frequency, points in frequency_points
            for point in points
        ):
            raise ValueError("各周期只能接收本周期产生的买卖点")
        if self.held_level is not None and self.held_level < 0:
            raise ValueError("held_level cannot be negative")
        if len(self.warmup_reason_codes) != len(set(self.warmup_reason_codes)):
            raise ValueError("warmup reason codes must be unique")
        diagnostic_frequencies = tuple(
            frequency
            for frequency, _codes in self.warmup_difference_codes_by_frequency
        )
        if (
            len(diagnostic_frequencies) != len(set(diagnostic_frequencies))
            or diagnostic_frequencies
            != tuple(
                frequency
                for frequency in SCREENING_WARMUP_FREQUENCIES
                if frequency in diagnostic_frequencies
            )
        ):
            raise ValueError("warmup diagnostic frequencies are invalid")
        for _frequency, codes in self.warmup_difference_codes_by_frequency:
            if (
                type(codes) is not tuple
                or len(codes) != len(set(codes))
                or not set(codes).issubset(SCREENING_WARMUP_DIFFERENCE_CODES)
            ):
                raise ValueError("warmup difference codes are invalid")
        if len({value.point_id for value in self.entry_execution_boundaries}) != len(
            self.entry_execution_boundaries
        ):
            raise ValueError("entry execution boundary point ids must be unique")
        if (
            len(self.selection_sources) != len(set(self.selection_sources))
            or any(not isinstance(value, str) or not value for value in self.selection_sources)
        ):
            raise ValueError("selection sources must be unique non-empty strings")
        if self.selection_research is not None and (
            self.selection_research.symbol != self.code
            or self.selection_research.path != self.selection_path
        ):
            raise ValueError("正式研究快照与结构包不一致")
        if any(
            value.symbol != self.code
            for value in self.entry_execution_boundaries
        ):
            raise ValueError("入场执行边界标的与结构包标的不一致")
        one_minute_buy_points = {
            point.point_id: point
            for point in self.one_points
            if point.side == "buy"
        }
        if any(
            boundary.point_id not in one_minute_buy_points
            or boundary.confirmation_bar_closed_at
            != one_minute_buy_points[boundary.point_id].available_at
            for boundary in self.entry_execution_boundaries
        ):
            raise ValueError("入场执行边界没有对应同标的已确认 1 分钟买点")
        if self.higher_timeframe_gates is not None:
            gates = self.higher_timeframe_gates
            if gates.symbol.subject != self.code:
                raise ValueError("个股高周期风险证据与结构包标的不一致")
            if gates.sector.subject != self.sector.sector_id:
                raise ValueError("板块高周期风险证据与结构包板块不一致")
            if any(
                gate.observed_at > self.as_of
                for gate in (gates.market, gates.sector, gates.symbol)
            ):
                raise ValueError("高周期风险证据不能晚于结构包决策时点")


@dataclass(frozen=True, slots=True)
class EvaluatedSignal:
    setup: TradeSetup
    trigger: StructuralPoint | None
    lifecycle: SignalLifecycle
    conflict: ConflictDecision
    entry: EntryDecision | None
    exit: ExitDecision | None
    technical_entry_allowed: bool = False
    market_risk_gate: str = "UNRESOLVED"
    sector_risk_gate: str = "UNRESOLVED"
    symbol_risk_gate: str = "UNRESOLVED"
    higher_timeframe_reason_codes: tuple[str, ...] = ()
    market_higher_timeframe_reason_codes: tuple[str, ...] = ()
    sector_higher_timeframe_reason_codes: tuple[str, ...] = ()
    symbol_higher_timeframe_reason_codes: tuple[str, ...] = ()
    market_higher_timeframe_states: tuple[tuple[str, str], ...] = ()
    sector_higher_timeframe_states: tuple[tuple[str, str], ...] = ()
    symbol_higher_timeframe_states: tuple[tuple[str, str], ...] = ()
    market_higher_timeframe_diagnostics: tuple[
        HigherTimeframePeriodDiagnostic, ...
    ] = ()
    sector_higher_timeframe_diagnostics: tuple[
        HigherTimeframePeriodDiagnostic, ...
    ] = ()
    symbol_higher_timeframe_diagnostics: tuple[
        HigherTimeframePeriodDiagnostic, ...
    ] = ()
    warmup_converged: bool = True
    warmup_reason_codes: tuple[str, ...] = ()
    warmup_by_frequency: tuple[tuple[str, bool, int, int], ...] = ()
    warmup_difference_codes_by_frequency: tuple[
        tuple[str, tuple[str, ...]], ...
    ] = ()
    daily_context: TimeframeContext | None = None
    physical_timeframe_recursive: bool = False
    entry_execution_boundary: EntryExecutionBoundary | None = None
    formal_selection: FormalSelectionGate | None = None
    selection_research: SelectionResearchSnapshot | None = None


def _point_time(point: StructuralPoint | ProvisionalCandidate) -> datetime:
    if isinstance(point, ProvisionalCandidate):
        return point.observed_at
    return point.available_at


def _current_five_minute_points(
    points: tuple[StructuralPoint | ProvisionalCandidate, ...],
    *,
    as_of: datetime,
    policy: TradingPolicy,
) -> tuple[StructuralPoint | ProvisionalCandidate, ...]:
    cutoff = as_of - timedelta(
        seconds=policy.max_five_minute_setup_age_seconds
    )
    current: dict[
        tuple[str, StructureTower, int],
        tuple[datetime, list[StructuralPoint | ProvisionalCandidate]],
    ] = {}
    for point in points:
        if point.source_frequency != "5m":
            raise ValueError("trade setup requires a 5m point")
        if isinstance(point, StructuralPoint) and not point.confirmed:
            continue
        observed_at = _point_time(point)
        if observed_at > as_of:
            raise ValueError("five-minute point cannot be after as_of")
        if observed_at < cutoff:
            continue
        lane = (point.point_type, point.tower, point.recursive_level)
        previous = current.get(lane)
        if previous is None or observed_at > previous[0]:
            current[lane] = (observed_at, [point])
        elif observed_at == previous[0]:
            previous[1].append(point)
    return tuple(
        point
        for _observed_at, lane_points in current.values()
        for point in lane_points
    )


class _TechnicalSignalEvaluator:
    def __init__(
        self,
        trading_policy: TradingPolicy = TradingPolicy(),
    ) -> None:
        self._policy = trading_policy

    def evaluate_symbol(
        self,
        bundle: SymbolStructureBundle,
    ) -> tuple[EvaluatedSignal, ...]:
        context = classify_context(
            frequency="30m",
            current_direction=bundle.thirty_direction,
            points=bundle.thirty_points,
            as_of=bundle.as_of,
        )
        daily_context = classify_context(
            frequency="d",
            current_direction=bundle.daily_direction,
            points=bundle.daily_points,
            as_of=bundle.as_of,
        )
        gate_bundle = bundle.higher_timeframe_gates
        market_gate = "UNRESOLVED" if gate_bundle is None else gate_bundle.market.gate
        sector_gate = (
            "UNRESOLVED"
            if gate_bundle is None
            else gate_bundle.sector.gate
        )
        symbol_gate = "UNRESOLVED" if gate_bundle is None else gate_bundle.symbol.gate
        market_risk_reasons = (
            ("HIGHER_TIMEFRAME_GATE_NOT_ATTACHED",)
            if gate_bundle is None
            else gate_bundle.market.reason_codes
        )
        symbol_risk_reasons = (
            ("HIGHER_TIMEFRAME_GATE_NOT_ATTACHED",)
            if gate_bundle is None
            else gate_bundle.symbol.reason_codes
        )
        sector_risk_reasons = (
            ("HIGHER_TIMEFRAME_SECTOR_GATE_NOT_ATTACHED",)
            if gate_bundle is None
            else gate_bundle.sector.reason_codes
        )
        risk_reasons = (
            tuple(
                dict.fromkeys(
                    (
                        *market_risk_reasons,
                        *sector_risk_reasons,
                        *symbol_risk_reasons,
                    )
                )
            )
        )
        market_states = (
            (
                ("M", "UNRESOLVED"),
                ("W", "UNRESOLVED"),
                ("D", "UNRESOLVED"),
            )
            if gate_bundle is None
            else (
                ("M", gate_bundle.market.monthly),
                ("W", gate_bundle.market.weekly),
                ("D", gate_bundle.market.daily),
            )
        )
        symbol_states = (
            (
                ("M", "UNRESOLVED"),
                ("W", "UNRESOLVED"),
                ("D", "UNRESOLVED"),
            )
            if gate_bundle is None
            else (
                ("M", gate_bundle.symbol.monthly),
                ("W", gate_bundle.symbol.weekly),
                ("D", gate_bundle.symbol.daily),
            )
        )
        sector_states = (
            (
                ("M", "UNRESOLVED"),
                ("W", "UNRESOLVED"),
                ("D", "UNRESOLVED"),
            )
            if gate_bundle is None
            else (
                ("M", gate_bundle.sector.monthly),
                ("W", gate_bundle.sector.weekly),
                ("D", gate_bundle.sector.daily),
            )
        )
        market_diagnostics = (
            () if gate_bundle is None else gate_bundle.market.period_diagnostics
        )
        symbol_diagnostics = (
            () if gate_bundle is None else gate_bundle.symbol.period_diagnostics
        )
        sector_diagnostics = (
            ()
            if gate_bundle is None
            else gate_bundle.sector.period_diagnostics
        )
        entry_boundaries = {
            value.point_id: value for value in bundle.entry_execution_boundaries
        }
        output: list[EvaluatedSignal] = []
        ordered_points = sorted(
            _current_five_minute_points(
                bundle.five_points,
                as_of=bundle.as_of,
                policy=self._policy,
            ),
            key=lambda point: (
                (
                    point.available_at
                    if isinstance(point, StructuralPoint)
                    else point.observed_at
                ),
                point.tower,
                point.recursive_level,
                point.point_type,
                (
                    point.point_id
                    if isinstance(point, StructuralPoint)
                    else point.candidate_id
                ),
            ),
        )
        for point in ordered_points:
            setup = build_setup(point, context, bundle.sector)
            trigger = match_one_minute_trigger(
                setup,
                bundle.one_points,
                as_of=bundle.as_of,
            )
            lifecycle = advance_lifecycle(
                None,
                setup,
                trigger,
                as_of=bundle.as_of,
            )
            if point.side == "sell":
                output.append(
                    EvaluatedSignal(
                        setup=setup,
                        trigger=trigger,
                        lifecycle=lifecycle,
                        conflict=ConflictDecision(False, (), (), ()),
                        entry=None,
                        exit=evaluate_exit_policy(
                            lifecycle,
                            setup,
                            trigger,
                            held_tower=bundle.held_tower,
                            held_level=bundle.held_level,
                            policy=self._policy,
                        ),
                        technical_entry_allowed=False,
                        market_risk_gate=market_gate,
                        sector_risk_gate=sector_gate,
                        symbol_risk_gate=symbol_gate,
                        higher_timeframe_reason_codes=risk_reasons,
                        market_higher_timeframe_reason_codes=market_risk_reasons,
                        sector_higher_timeframe_reason_codes=sector_risk_reasons,
                        symbol_higher_timeframe_reason_codes=symbol_risk_reasons,
                        market_higher_timeframe_states=market_states,
                        sector_higher_timeframe_states=sector_states,
                        symbol_higher_timeframe_states=symbol_states,
                        market_higher_timeframe_diagnostics=market_diagnostics,
                        sector_higher_timeframe_diagnostics=sector_diagnostics,
                        symbol_higher_timeframe_diagnostics=symbol_diagnostics,
                        warmup_converged=bundle.warmup_converged,
                        warmup_reason_codes=bundle.warmup_reason_codes,
                        warmup_by_frequency=bundle.warmup_by_frequency,
                        warmup_difference_codes_by_frequency=(
                            bundle.warmup_difference_codes_by_frequency
                        ),
                        daily_context=daily_context,
                        physical_timeframe_recursive=(
                            bundle.physical_timeframe_recursive
                        ),
                        entry_execution_boundary=(
                            None
                            if trigger is None
                            else entry_boundaries.get(trigger.point_id)
                        ),
                    )
                )
                continue
            conflict = resolve_conflict(
                setup,
                bundle.opposite_points,
                physical_timeframes=bundle.physical_timeframe_recursive,
            )
            entry = evaluate_entry_policy(
                lifecycle,
                setup,
                trigger,
                conflict,
                self._policy,
            )
            if entry.allowed and daily_context.hard_block:
                entry = replace(
                    entry,
                    allowed=False,
                    risk_multiplier=entry.risk_multiplier * 0,
                    reason_codes=tuple(
                        dict.fromkeys(
                            (*entry.reason_codes, "daily_structure_hostile")
                        )
                    ),
                )
            technical_entry_allowed = entry.allowed
            higher_timeframe_blocked = (
                bundle.enforce_higher_timeframe_entry_gate
                and (gate_bundle is None or not gate_bundle.allows_new_entry)
            )
            warmup_blocked = (
                bundle.enforce_warmup_entry_gate and not bundle.warmup_converged
            )
            if entry.allowed and (higher_timeframe_blocked or warmup_blocked):
                reasons = tuple(
                    dict.fromkeys(
                        (
                            *entry.reason_codes,
                            *(
                                (
                                    "HIGHER_TIMEFRAME_GATE_NOT_GREEN",
                                    f"MARKET_GATE_{market_gate}",
                                    f"SECTOR_GATE_{sector_gate}",
                                    f"SYMBOL_GATE_{symbol_gate}",
                                    *risk_reasons,
                                )
                                if higher_timeframe_blocked
                                else ()
                            ),
                            *(
                                (
                                    "WARMUP_CONVERGENCE_GATE_FAILED",
                                    *bundle.warmup_reason_codes,
                                )
                                if warmup_blocked
                                else ()
                            ),
                        )
                    )
                )
                entry = replace(
                    entry,
                    allowed=False,
                    risk_multiplier=entry.risk_multiplier * 0,
                    reason_codes=reasons,
                )
            output.append(
                EvaluatedSignal(
                    setup,
                    trigger,
                    lifecycle,
                    conflict,
                    entry,
                    None,
                    technical_entry_allowed=technical_entry_allowed,
                    market_risk_gate=market_gate,
                    sector_risk_gate=sector_gate,
                    symbol_risk_gate=symbol_gate,
                    higher_timeframe_reason_codes=risk_reasons,
                    market_higher_timeframe_reason_codes=market_risk_reasons,
                    sector_higher_timeframe_reason_codes=sector_risk_reasons,
                    symbol_higher_timeframe_reason_codes=symbol_risk_reasons,
                    market_higher_timeframe_states=market_states,
                    sector_higher_timeframe_states=sector_states,
                    symbol_higher_timeframe_states=symbol_states,
                    market_higher_timeframe_diagnostics=market_diagnostics,
                    sector_higher_timeframe_diagnostics=sector_diagnostics,
                    symbol_higher_timeframe_diagnostics=symbol_diagnostics,
                    warmup_converged=bundle.warmup_converged,
                    warmup_reason_codes=bundle.warmup_reason_codes,
                    warmup_by_frequency=bundle.warmup_by_frequency,
                    warmup_difference_codes_by_frequency=(
                        bundle.warmup_difference_codes_by_frequency
                    ),
                    daily_context=daily_context,
                    physical_timeframe_recursive=(
                        bundle.physical_timeframe_recursive
                    ),
                    entry_execution_boundary=(
                        None
                        if trigger is None
                        else entry_boundaries.get(trigger.point_id)
                    ),
                )
            )
        return tuple(output)
__all__ = [
    "EvaluatedSignal",
    "SymbolStructureBundle",
]
