"""统一决策核心内部使用的技术信号评估阶段。

本模块只保存结构包、评估结果和私有技术评估器；生产调用方必须通过
``HumanAssistedDecisionCore``，从而保证正式研究、板块触发与技术结构使用同一入口。
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime
from math import isfinite

from chanlun.decision_support.fingerprints import normalize_datetime
from chanlun.decision_support.trading_system.conflicts import resolve_conflict
from chanlun.decision_support.trading_system.a_share_minute_grid import (
    a_share_optional_entry_valid_until,
)
from chanlun.decision_support.trading_system.context import classify_context
from chanlun.decision_support.trading_system.context_evidence import (
    SamePeriodTechnicalContext,
    SignalContextAssessment,
    assess_signal_context,
)
from chanlun.decision_support.trading_system.execution_policy import (
    evaluate_entry_policy,
    evaluate_exit_policy,
)
from chanlun.decision_support.trading_system.lifecycle import (
    advance_lifecycle,
    build_setup,
    current_five_minute_setup_points,
    five_minute_setup_is_executable,
    match_one_minute_nesting_witness,
    structural_point_occurrence_id,
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
from chanlun.decision_support.trading_system.operation_level import (
    is_five_minute_trade_level,
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
    latest_price: float | None = None
    daily_technical_context: SamePeriodTechnicalContext | None = None
    thirty_technical_context: SamePeriodTechnicalContext | None = None
    previous_lifecycles: tuple[SignalLifecycle, ...] = ()
    previous_trigger_points: tuple[StructuralPoint, ...] = ()
    analysis_closed_at_by_frequency: tuple[tuple[str, datetime], ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "as_of", normalize_datetime(self.as_of, "as_of"))
        normalized_analysis_closed_at = tuple(
            (
                frequency,
                normalize_datetime(closed_at, f"{frequency} analysis closed_at"),
            )
            for frequency, closed_at in self.analysis_closed_at_by_frequency
        )
        object.__setattr__(
            self,
            "analysis_closed_at_by_frequency",
            normalized_analysis_closed_at,
        )
        analysis_frequencies = tuple(
            frequency for frequency, _closed_at in normalized_analysis_closed_at
        )
        if (
            len(analysis_frequencies) != len(set(analysis_frequencies))
            or analysis_frequencies
            != tuple(
                frequency
                for frequency in SCREENING_WARMUP_FREQUENCIES
                if frequency in analysis_frequencies
            )
            or any(
                closed_at > self.as_of
                for _frequency, closed_at in normalized_analysis_closed_at
            )
        ):
            raise ValueError("analysis closed_at frequencies are invalid")
        if not isinstance(self.code, str) or not self.code.strip():
            raise ValueError("结构包标的不能为空")
        if self.latest_price is not None and (
            not isinstance(self.latest_price, (int, float))
            or isinstance(self.latest_price, bool)
            or not isfinite(float(self.latest_price))
            or self.latest_price <= 0
        ):
            raise ValueError("最新价格必须为正数")
        for expected, value in (
            ("d", self.daily_technical_context),
            ("30m", self.thirty_technical_context),
        ):
            if value is not None and (
                value.frequency != expected or value.observed_at > self.as_of
            ):
                raise ValueError("同周期技术上下文与结构包不一致")
        if len({item.signal_id for item in self.previous_lifecycles}) != len(
            self.previous_lifecycles
        ) or len({item.setup_id for item in self.previous_lifecycles}) != len(
            self.previous_lifecycles
        ):
            raise ValueError("previous lifecycles must be unique")
        if any(item.observed_at > self.as_of for item in self.previous_lifecycles):
            raise ValueError("previous lifecycle cannot be after bundle as_of")
        if len({item.point_id for item in self.previous_trigger_points}) != len(
            self.previous_trigger_points
        ) or any(
            item.code != self.code or item.source_frequency != "1m"
            for item in self.previous_trigger_points
        ):
            raise ValueError("previous trigger points are invalid")
        previous_trigger_ids = {
            item.point_id for item in self.previous_trigger_points
        }
        # 旧快照可能携带 1 分钟定位点；新 5 分钟正式信号不再要求它。
        if any(
            item.trigger_point_id is not None
            and item.trigger_point_id not in previous_trigger_ids
            for item in self.previous_lifecycles
        ):
            raise ValueError("previous lifecycle segment evidence is incomplete")
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
        if any(
            not is_five_minute_trade_level(
                point.source_frequency,
                point.recursive_level,
            )
            for point in self.five_points
        ):
            raise ValueError("5 分钟交易通道只能接收物理 5m/L0 买卖点")
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
        boundary_pair_ids = tuple(
            (value.setup_occurrence_id, value.point_id)
            for value in self.entry_execution_boundaries
        )
        if len(boundary_pair_ids) != len(set(boundary_pair_ids)):
            raise ValueError("entry execution boundary setup/witness pairs must be unique")
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
        five_minute_buy_occurrences = {
            structural_point_occurrence_id(point)
            for point in self.five_points
            if isinstance(point, StructuralPoint) and point.side == "buy"
        }
        if any(
            boundary.setup_occurrence_id not in five_minute_buy_occurrences
            or boundary.point_id not in one_minute_buy_points
            or boundary.confirmation_bar_closed_at
            < one_minute_buy_points[boundary.point_id].available_at
            for boundary in self.entry_execution_boundaries
        ):
            raise ValueError("入场执行边界不能早于 1 分钟区间套见证")
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
    higher_timeframe_data_integrity_reason_codes: tuple[str, ...] = ()
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
    daily_technical_context: SamePeriodTechnicalContext | None = None
    thirty_technical_context: SamePeriodTechnicalContext | None = None
    context_assessment: SignalContextAssessment | None = None
    advisory_reason_codes: tuple[str, ...] = ()


def _point_time(point: StructuralPoint | ProvisionalCandidate) -> datetime:
    if isinstance(point, ProvisionalCandidate):
        return point.available_at
    return point.available_at


def _trade_level_warmup_converged(bundle: SymbolStructureBundle) -> bool:
    """Return the physical 5m warmup result used by the entry gate.

    Production bundles always carry all four rows.  Falling back to the legacy
    aggregate keeps hand-built/older callers fail-closed when the 5m row is
    absent instead of silently authorizing a new entry.
    """

    for frequency, converged, _full_count, _suffix_count in bundle.warmup_by_frequency:
        if frequency == "5m":
            return converged
    return bundle.warmup_converged


def _trade_level_warmup_failure_reasons(
    bundle: SymbolStructureBundle,
) -> tuple[str, ...]:
    """Keep only the 5m cause in the hard-block reason list."""

    return tuple(
        reason
        for reason in bundle.warmup_reason_codes
        if reason.startswith("5M:")
    )


def _context_warmup_advisory_reasons(
    bundle: SymbolStructureBundle,
) -> tuple[str, ...]:
    """Expose non-trade-period divergence as review context, never a veto."""

    diagnostic_failures = {
        "WARMUP_HISTORY_INSUFFICIENT",
        "WARMUP_TAIL_DIVERGED",
    }
    return tuple(
        reason
        for reason in bundle.warmup_reason_codes
        if reason.split(":", 1)[0] in {"D", "30M", "1M"}
        and reason.split(":", 1)[-1] in diagnostic_failures
    )


def _current_five_minute_points(
    points: tuple[StructuralPoint | ProvisionalCandidate, ...],
    *,
    as_of: datetime,
    policy: TradingPolicy,
) -> tuple[StructuralPoint | ProvisionalCandidate, ...]:
    """兼容旧内部入口；唯一通道裁剪规则位于生命周期模块。"""

    current = current_five_minute_setup_points(
        points,
        as_of=as_of,
        max_setup_age_seconds=policy.max_five_minute_setup_age_seconds,
    )
    return tuple(
        point
        for point in current
        if five_minute_setup_is_executable(
            point,
            as_of=as_of,
            max_setup_age_seconds=policy.max_five_minute_setup_age_seconds,
        )
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
        higher_timeframe_data_integrity_reasons = (
            ()
            if gate_bundle is None
            or not bundle.enforce_higher_timeframe_entry_gate
            else gate_bundle.hard_data_integrity_reason_codes
        )
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
            (value.setup_occurrence_id, value.point_id): value
            for value in bundle.entry_execution_boundaries
        }
        previous_lifecycles = {
            item.setup_id: item for item in bundle.previous_lifecycles
        }
        previous_triggers = {
            item.point_id: item for item in bundle.previous_trigger_points
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
                    else point.available_at
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
        sector_required = (
            bundle.selection_path == "INDIVIDUAL_THREE_PROGRAM"
        )
        for point in ordered_points:
            setup = build_setup(
                point,
                context,
                bundle.sector,
                sector_required=sector_required,
            )
            previous_lifecycle = previous_lifecycles.get(setup.setup_id)
            trigger = match_one_minute_nesting_witness(
                setup,
                bundle.one_points,
                as_of=bundle.as_of,
                minimum_tick=self._policy.minimum_tick,
            )
            if (
                previous_lifecycle is not None
                and previous_lifecycle.trigger_point_id is not None
            ):
                previous_trigger = previous_triggers.get(
                    previous_lifecycle.trigger_point_id
                )
                if previous_trigger is not None:
                    persisted_match = match_one_minute_nesting_witness(
                        setup,
                        (previous_trigger,),
                        as_of=bundle.as_of,
                        minimum_tick=self._policy.minimum_tick,
                    )
                    # The first exact nesting witness is immutable for this
                    # setup.  Persist it across a 5m-only refresh, but never
                    # replace it with a later witness and move/reopen entry.
                    if persisted_match is not None:
                        trigger = persisted_match
            lifecycle = advance_lifecycle(
                previous_lifecycle,
                setup,
                trigger,
                as_of=bundle.as_of,
                current_price=bundle.latest_price,
                minimum_tick=self._policy.minimum_tick,
            )
            entry_boundary = (
                None
                if trigger is None
                else entry_boundaries.get(
                    (structural_point_occurrence_id(setup.point), trigger.point_id)
                )
            )
            if entry_boundary is not None:
                jointly_known_at = max(
                    normalize_datetime(setup.point.available_at, "setup available_at"),
                    normalize_datetime(trigger.available_at, "witness available_at"),
                )
                if (
                    normalize_datetime(
                        entry_boundary.confirmation_bar_closed_at,
                        "entry boundary confirmation_bar_closed_at",
                    )
                    != jointly_known_at
                ):
                    # A pair-scoped boundary must also be frozen at the first
                    # timestamp at which the setup and nesting witness were
                    # jointly known.
                    entry_boundary = None
            context_assessment = assess_signal_context(
                side=point.side,
                point_type=point.point_type,
                daily_evidence=bundle.daily_technical_context,
                thirty_minute_evidence=bundle.thirty_technical_context,
                daily_structure=daily_context,
                thirty_minute_structure=context,
            )
            higher_timeframe_adverse = (
                bundle.enforce_higher_timeframe_entry_gate
                and (
                    gate_bundle is None
                    or not gate_bundle.allows_new_entry_for(
                        bundle.selection_path
                    )
                )
            )
            advisory_reasons: list[str] = []
            if setup.context.hard_block:
                advisory_reasons.append("thirty_minute_hostile")
            if daily_context.hard_block:
                advisory_reasons.append("daily_structure_hostile")
            if setup.sector_required and setup.sector.hard_block:
                advisory_reasons.append("sector_hostile")
            if point.side == "buy" and higher_timeframe_adverse:
                advisory_reasons.extend(
                    (
                        "HIGHER_TIMEFRAME_CONTEXT_NOT_GREEN",
                        f"MARKET_GATE_{market_gate}",
                        *(
                            (f"SECTOR_GATE_{sector_gate}",)
                            if sector_required
                            else ()
                        ),
                        f"SYMBOL_GATE_{symbol_gate}",
                        *risk_reasons,
                    )
                )
            if context_assessment.grade != "A":
                advisory_reasons.append(
                    f"SAME_PERIOD_CONTEXT_GRADE_{context_assessment.grade}"
                )
            if point.side == "buy":
                # 日线、30 分钟只负责环境分级；1 分钟区间套负责精确执行定位。
                # 暖机差异不能否定物理 5 分钟正式点，但 1 分钟未确认时不得
                # 生成当前买入资格。
                advisory_reasons.extend(
                    _context_warmup_advisory_reasons(bundle)
                )
            advisory_reason_codes = tuple(dict.fromkeys(advisory_reasons))
            entry_boundary_reason: str | None = None
            if (
                point.side == "buy"
                and point.code.startswith(("SH.", "SZ.", "BJ."))
                and trigger is not None
                and lifecycle.stage in {"triggered", "executable", "active"}
                and bundle.physical_timeframe_recursive
            ):
                if entry_boundary is None:
                    entry_boundary_reason = (
                        "ONE_MINUTE_SEGMENT_BOUNDARY_EXPIRED"
                        if a_share_optional_entry_valid_until(
                            trigger.available_at
                        )
                        <= bundle.as_of
                        else "ONE_MINUTE_SEGMENT_BOUNDARY_MISSING"
                    )
                elif (
                    entry_boundary is not None
                    and entry_boundary.entry_valid_until <= bundle.as_of
                ):
                    entry_boundary_reason = "ONE_MINUTE_SEGMENT_BOUNDARY_EXPIRED"
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
                        higher_timeframe_data_integrity_reason_codes=(
                            higher_timeframe_data_integrity_reasons
                        ),
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
                            entry_boundary
                        ),
                        daily_technical_context=bundle.daily_technical_context,
                        thirty_technical_context=bundle.thirty_technical_context,
                        context_assessment=context_assessment,
                        advisory_reason_codes=advisory_reason_codes,
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
            # 该标志只回答物理 5 分钟买点本身是否成立。1 分钟区间套及其
            # 瞬时边界决定精确执行资格，但不能反向抹掉 5 分钟结构信号。
            technical_entry_allowed = not any(
                reason
                not in {
                    "one_minute_not_confirmed",
                    "ONE_MINUTE_SEGMENT_BOUNDARY_MISSING",
                    "ONE_MINUTE_SEGMENT_BOUNDARY_EXPIRED",
                }
                for reason in entry.reason_codes
            )
            if entry_boundary_reason is not None:
                entry = replace(
                    entry,
                    allowed=False,
                    reason_codes=tuple(
                        dict.fromkeys((*entry.reason_codes, entry_boundary_reason))
                    ),
                )
            warmup_blocked = bundle.enforce_warmup_entry_gate and not (
                _trade_level_warmup_converged(bundle)
            )
            outer_hard_reasons = tuple(
                dict.fromkeys(
                    (
                        *(
                            (
                                "HIGHER_TIMEFRAME_DATA_INTEGRITY_GATE_FAILED",
                                *higher_timeframe_data_integrity_reasons,
                            )
                            if higher_timeframe_data_integrity_reasons
                            else ()
                        ),
                        *(
                            (
                                "WARMUP_CONVERGENCE_GATE_FAILED",
                                *_trade_level_warmup_failure_reasons(bundle),
                            )
                            if warmup_blocked
                            else ()
                        ),
                    )
                )
            )
            if outer_hard_reasons:
                entry = replace(
                    entry,
                    allowed=False,
                    risk_multiplier=entry.risk_multiplier * 0,
                    reason_codes=tuple(
                        dict.fromkeys((*entry.reason_codes, *outer_hard_reasons))
                    ),
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
                    higher_timeframe_data_integrity_reason_codes=(
                        higher_timeframe_data_integrity_reasons
                    ),
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
                        entry_boundary
                    ),
                    daily_technical_context=bundle.daily_technical_context,
                    thirty_technical_context=bundle.thirty_technical_context,
                    context_assessment=context_assessment,
                    advisory_reason_codes=advisory_reason_codes,
                )
            )
        return tuple(output)
__all__ = [
    "EvaluatedSignal",
    "SymbolStructureBundle",
]
