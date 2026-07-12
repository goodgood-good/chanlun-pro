from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field, replace
from datetime import datetime
import re
from threading import Lock
from types import MappingProxyType
from typing import Callable, Mapping

from chanlun.recursive_bt.engine.engine import Signal

from .event_factory import event_from_signal
from .event_service import DecisionEventService
from .fingerprints import normalize_datetime
from .models import DecisionEvent, StrategyTrack
from .rule_cards import EvaluationVerdict, RuleEvaluation
from .rule_context import LevelEvaluationFacts, RuleRuntimeFacts
from .risk import RiskContext
from .strategies import (
    CandidateDecision,
    REVERSAL_OBSERVATIONS,
    TREND_BUYS,
    evaluate_bottom_reversal,
    evaluate_trend_continuation,
    reversal_rank_key,
    trend_rank_key,
)
from .universe import (
    EligibleSecurity,
    SecuritySnapshot,
    UniverseExclusion,
    UniversePolicy,
    filter_universe,
)


_FINGERPRINT_RE = re.compile(r"sha256:[0-9a-f]{64}")
SIGNAL_OBSERVATION_STATES = frozenset(
    {
        "trusted_first_seen",
        "baseline_not_fresh",
        "quarantined_unknown",
    }
)


@dataclass(frozen=True, slots=True)
class UniverseSnapshot:
    observed_at: datetime
    securities: tuple[SecuritySnapshot, ...]

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "observed_at",
            normalize_datetime(self.observed_at, "observed_at"),
        )
        object.__setattr__(self, "securities", tuple(self.securities))
        if not all(
            isinstance(item, SecuritySnapshot) for item in self.securities
        ):
            raise TypeError("securities must contain SecuritySnapshot values")


@dataclass(frozen=True, slots=True)
class SymbolStructureSnapshot:
    frequency: str
    cd: object
    signals: tuple[Signal, ...]
    first_visible_bar: int
    completed_bars: tuple[Mapping[str, object], ...]
    config: Mapping[str, object]
    operation_bar_closed: bool
    fund_ok: bool
    comparison_ok: bool
    invalidations: tuple[InvalidationNotice, ...] = ()
    current_cycle_id: str | None = None
    signals_first_observed_at: Mapping[str, datetime] = field(
        default_factory=dict
    )
    signal_observation_states: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.frequency, str) or not self.frequency:
            raise ValueError("frequency must be a non-empty string")
        object.__setattr__(self, "signals", tuple(self.signals))
        object.__setattr__(self, "completed_bars", tuple(self.completed_bars))
        object.__setattr__(self, "invalidations", tuple(self.invalidations))
        if not all(isinstance(item, Signal) for item in self.signals):
            raise TypeError("signals must contain Signal values")
        if not all(
            isinstance(item, InvalidationNotice)
            for item in self.invalidations
        ):
            raise TypeError(
                "invalidations must contain InvalidationNotice values"
            )
        if (
            isinstance(self.first_visible_bar, bool)
            or not isinstance(self.first_visible_bar, int)
            or self.first_visible_bar < 0
        ):
            raise ValueError("first_visible_bar must be non-negative")
        if not isinstance(self.config, Mapping):
            raise TypeError("config must be a mapping")
        if self.current_cycle_id is not None and (
            not isinstance(self.current_cycle_id, str)
            or _FINGERPRINT_RE.fullmatch(self.current_cycle_id) is None
        ):
            raise ValueError(
                "current_cycle_id must use sha256:<64 lowercase hex>"
            )
        if not isinstance(self.signals_first_observed_at, Mapping):
            raise TypeError("signals_first_observed_at must be a mapping")
        normalized_observations: dict[str, datetime] = {}
        for signal_fingerprint, first_observed_at in (
            self.signals_first_observed_at.items()
        ):
            if (
                not isinstance(signal_fingerprint, str)
                or _FINGERPRINT_RE.fullmatch(signal_fingerprint) is None
            ):
                raise ValueError(
                    "signals_first_observed_at keys must be fingerprints"
                )
            normalized_observations[signal_fingerprint] = normalize_datetime(
                first_observed_at,
                "signals_first_observed_at value",
            )
        object.__setattr__(
            self,
            "signals_first_observed_at",
            MappingProxyType(normalized_observations),
        )
        if not isinstance(self.signal_observation_states, Mapping):
            raise TypeError("signal_observation_states must be a mapping")
        normalized_states = dict(self.signal_observation_states)
        if not normalized_states and normalized_observations:
            normalized_states = {
                signal_fingerprint: "trusted_first_seen"
                for signal_fingerprint in normalized_observations
            }
        if (
            any(
                not isinstance(signal_fingerprint, str)
                or _FINGERPRINT_RE.fullmatch(signal_fingerprint) is None
                for signal_fingerprint in normalized_states
            )
            or any(
                state not in SIGNAL_OBSERVATION_STATES
                for state in normalized_states.values()
            )
            or not set(normalized_observations).issubset(normalized_states)
            or any(
                (
                    state == "quarantined_unknown"
                    and signal_fingerprint in normalized_observations
                )
                or (
                    state != "quarantined_unknown"
                    and signal_fingerprint not in normalized_observations
                )
                for signal_fingerprint, state in normalized_states.items()
            )
        ):
            raise ValueError("signal observation state bindings are invalid")
        object.__setattr__(
            self,
            "signal_observation_states",
            MappingProxyType(normalized_states),
        )
        for field_name in (
            "operation_bar_closed",
            "fund_ok",
            "comparison_ok",
        ):
            if type(getattr(self, field_name)) is not bool:
                raise ValueError(f"{field_name} must be boolean")


@dataclass(frozen=True, slots=True)
class SymbolScanFailure:
    code: str
    reason: str
    detail: str


@dataclass(frozen=True, slots=True)
class InvalidationNotice:
    event_id: str
    reason: str

    def __post_init__(self) -> None:
        for field_name in ("event_id", "reason"):
            value = getattr(self, field_name)
            if not isinstance(value, str) or not value:
                raise ValueError(f"{field_name} must be a non-empty string")


@dataclass(frozen=True, slots=True)
class ScanResult:
    bar_closed_at: datetime
    code: str
    created_count: int
    trend_candidates: tuple[CandidateDecision, ...]
    reversal_candidates: tuple[CandidateDecision, ...]
    exclusions: tuple[UniverseExclusion, ...]
    failures: tuple[SymbolScanFailure, ...]
    invalidated_count: int = 0


UniverseProvider = Callable[[datetime], UniverseSnapshot]
StructureProvider = Callable[
    [EligibleSecurity, datetime],
    SymbolStructureSnapshot,
]
RiskContextProvider = Callable[
    [EligibleSecurity, DecisionEvent, datetime],
    RiskContext,
]
EventStrategyRunBinder = Callable[[DecisionEvent], DecisionEvent]


class DecisionScanner:
    def __init__(
        self,
        *,
        universe_provider: UniverseProvider,
        structure_provider: StructureProvider,
        risk_context_provider: RiskContextProvider,
        event_service: DecisionEventService,
        rule_engine: object,
        manual_check_workflow: object | None = None,
        event_strategy_run_binder: EventStrategyRunBinder | None = None,
        universe_policy: UniversePolicy | None = None,
        max_market_age_seconds: int = 300,
        processed_bar_limit: int = 2_048,
    ) -> None:
        for value, field_name in (
            (universe_provider, "universe_provider"),
            (structure_provider, "structure_provider"),
            (risk_context_provider, "risk_context_provider"),
        ):
            if not callable(value):
                raise TypeError(f"{field_name} must be callable")
        if not isinstance(event_service, DecisionEventService):
            raise TypeError("event_service must be DecisionEventService")
        if not callable(getattr(rule_engine, "evaluate", None)):
            raise TypeError("rule_engine must provide callable evaluate")
        if manual_check_workflow is not None and not callable(
            getattr(manual_check_workflow, "capture_candidate", None)
        ):
            raise TypeError(
                "manual_check_workflow must provide callable capture_candidate"
            )
        if event_strategy_run_binder is not None and not callable(
            event_strategy_run_binder
        ):
            raise TypeError("event_strategy_run_binder must be callable")
        if universe_policy is not None and not isinstance(
            universe_policy,
            UniversePolicy,
        ):
            raise TypeError("universe_policy must be UniversePolicy")
        if (
            isinstance(max_market_age_seconds, bool)
            or not isinstance(max_market_age_seconds, int)
            or max_market_age_seconds <= 0
        ):
            raise ValueError("max_market_age_seconds must be positive")
        if (
            isinstance(processed_bar_limit, bool)
            or not isinstance(processed_bar_limit, int)
            or processed_bar_limit <= 0
        ):
            raise ValueError("processed_bar_limit must be positive")
        self._universe_provider = universe_provider
        self._structure_provider = structure_provider
        self._risk_context_provider = risk_context_provider
        self._event_service = event_service
        self._rule_engine = rule_engine
        self._manual_check_workflow = manual_check_workflow
        self._event_strategy_run_binder = event_strategy_run_binder
        self._universe_policy = (
            universe_policy or UniversePolicy.a_share_short_term()
        )
        self._max_market_age_seconds = max_market_age_seconds
        self._processed_bar_limit = processed_bar_limit
        self._processed: set[datetime] = set()
        self._processed_order: deque[datetime] = deque()
        self._inflight: set[datetime] = set()
        self._lock = Lock()

    @property
    def event_service(self) -> DecisionEventService:
        return self._event_service

    def scan_closed_bar(self, bar_closed_at: datetime) -> ScanResult:
        bar_closed_at = normalize_datetime(bar_closed_at, "bar_closed_at")
        if (
            bar_closed_at.minute % 5 != 0
            or bar_closed_at.second != 0
            or bar_closed_at.microsecond != 0
        ):
            raise ValueError("bar_closed_at must identify a closed 5-minute bar")
        with self._lock:
            if (
                bar_closed_at in self._processed
                or bar_closed_at in self._inflight
            ):
                return self._empty_result(bar_closed_at, "duplicate_bar")
            self._inflight.add(bar_closed_at)

        completed = False
        try:
            result = self._scan(bar_closed_at)
            completed = result.code in {"ok", "no_eligible_symbols"}
            return result
        finally:
            with self._lock:
                self._inflight.discard(bar_closed_at)
                if completed:
                    if bar_closed_at not in self._processed:
                        self._processed.add(bar_closed_at)
                        self._processed_order.append(bar_closed_at)
                    while len(self._processed_order) > self._processed_bar_limit:
                        expired = self._processed_order.popleft()
                        self._processed.discard(expired)

    def _scan(self, bar_closed_at: datetime) -> ScanResult:
        market_snapshot = self._universe_provider(bar_closed_at)
        if not isinstance(market_snapshot, UniverseSnapshot):
            raise TypeError("universe_provider must return UniverseSnapshot")
        age_seconds = (
            bar_closed_at - market_snapshot.observed_at
        ).total_seconds()
        if age_seconds < 0:
            return self._empty_result(bar_closed_at, "future_market_data")
        if age_seconds > self._max_market_age_seconds:
            return self._empty_result(bar_closed_at, "stale_market_data")

        universe = filter_universe(
            market_snapshot.securities,
            bar_closed_at,
            self._universe_policy,
        )
        if not universe.included:
            return ScanResult(
                bar_closed_at,
                "no_eligible_symbols",
                0,
                (),
                (),
                universe.excluded,
                (),
            )

        trend: list[CandidateDecision] = []
        reversal: list[CandidateDecision] = []
        failures: list[SymbolScanFailure] = []
        created_count = 0
        invalidated_count = 0
        for security in universe.included:
            try:
                structure = self._structure_provider(
                    security,
                    bar_closed_at,
                )
                if not isinstance(structure, SymbolStructureSnapshot):
                    raise TypeError(
                        "structure_provider must return SymbolStructureSnapshot"
                    )
                for notice in structure.invalidations:
                    before = self._event_service.get(notice.event_id)
                    if before.event.code != security.code:
                        raise ValueError(
                            "invalidation event does not belong to symbol"
                        )
                    after = self._event_service.invalidate(
                        notice.event_id,
                        notice.reason,
                        occurred_at=bar_closed_at,
                    )
                    if (
                        before.state is not after.state
                        and after.state.value == "invalidated"
                    ):
                        invalidated_count += 1
                for signal in structure.signals:
                    if signal.bs_type in TREND_BUYS:
                        created, decision = self._process_trend(
                            security,
                            structure,
                            signal,
                            bar_closed_at,
                        )
                        created_count += created
                        if decision is not None:
                            trend.append(decision)
                    elif signal.bs_type in REVERSAL_OBSERVATIONS:
                        created, decision = self._process_reversal(
                            security,
                            structure,
                            signal,
                            bar_closed_at,
                        )
                        created_count += created
                        if decision is not None:
                            reversal.append(decision)
            except Exception as exc:
                failures.append(
                    SymbolScanFailure(
                        security.code,
                        type(exc).__name__,
                        str(exc),
                    )
                )
        trend.sort(key=trend_rank_key)
        reversal.sort(key=reversal_rank_key)
        return ScanResult(
            bar_closed_at,
            "partial_failure" if failures else "ok",
            created_count,
            tuple(trend),
            tuple(reversal),
            universe.excluded,
            tuple(failures),
            invalidated_count,
        )

    def _build_event(
        self,
        security: EligibleSecurity,
        structure: SymbolStructureSnapshot,
        signal: Signal,
        bar_closed_at: datetime,
        track: StrategyTrack,
    ) -> DecisionEvent | None:
        return event_from_signal(
            market=security.market,
            code=security.code,
            name=security.name,
            frequency=structure.frequency,
            signal=signal,
            first_visible_bar=structure.first_visible_bar,
            observed_at=bar_closed_at,
            bar_closed_at=bar_closed_at,
            operation_bar_closed=structure.operation_bar_closed,
            cd=structure.cd,
            market_constraints=security.market_constraints(),
            completed_bars=structure.completed_bars,
            config=structure.config,
            strategy_track=track,
        )

    def _process_trend(
        self,
        security: EligibleSecurity,
        structure: SymbolStructureSnapshot,
        signal: Signal,
        bar_closed_at: datetime,
    ) -> tuple[int, CandidateDecision | None]:
        event = self._build_event(
            security,
            structure,
            signal,
            bar_closed_at,
            StrategyTrack.TREND_CONTINUATION,
        )
        if event is None:
            return 0, None
        decision = evaluate_trend_continuation(
            event,
            fund_ok=structure.fund_ok,
            comparison_ok=structure.comparison_ok,
        )
        if not decision.accepted:
            return 0, None
        event, evaluation, runtime_facts = self._evaluate_rule(
            security,
            structure,
            event,
        )
        if evaluation.verdict is EvaluationVerdict.REJECT:
            return 0, None
        decision = self._bind_candidate(decision, event, evaluation)
        created = self._event_service.store.count_events(event.event_id) == 0
        risk_context = self._risk_context_provider(
            security,
            event,
            bar_closed_at,
        )
        self._event_service.register(
            event,
            risk_context,
            rule_evaluation=evaluation,
        )
        self._capture_manual_check(event, runtime_facts, evaluation)
        return int(created), decision

    def _process_reversal(
        self,
        security: EligibleSecurity,
        structure: SymbolStructureSnapshot,
        signal: Signal,
        bar_closed_at: datetime,
    ) -> tuple[int, CandidateDecision | None]:
        event = self._build_event(
            security,
            structure,
            signal,
            bar_closed_at,
            StrategyTrack.BOTTOM_REVERSAL,
        )
        if event is None:
            return 0, None
        decision = evaluate_bottom_reversal(event)
        if not decision.accepted and not decision.observation:
            return 0, None
        event, evaluation, runtime_facts = self._evaluate_rule(
            security,
            structure,
            event,
        )
        if evaluation.verdict is EvaluationVerdict.REJECT:
            return 0, None
        decision = self._bind_candidate(decision, event, evaluation)
        created = self._event_service.store.count_events(event.event_id) == 0
        risk_context = self._risk_context_provider(
            security,
            event,
            bar_closed_at,
        )
        self._event_service.register(
            event,
            risk_context,
            rule_evaluation=evaluation,
        )
        self._capture_manual_check(event, runtime_facts, evaluation)
        return int(created), decision

    def _evaluate_rule(
        self,
        security: EligibleSecurity,
        structure: SymbolStructureSnapshot,
        event: DecisionEvent,
    ) -> tuple[DecisionEvent, RuleEvaluation, RuleRuntimeFacts]:
        latest_price: object = event.signal.price
        if structure.completed_bars:
            latest_price = structure.completed_bars[-1].get(
                "close",
                latest_price,
            )
        runtime_facts = RuleRuntimeFacts(
            fundamental_ok=structure.fund_ok,
            comparison_ok=structure.comparison_ok,
            market_liquid=(
                security.security.avg_turnover_20d is not None
                and security.security.avg_turnover_20d
                >= self._universe_policy.min_avg_turnover_20d
            ),
            risk_allowed=None,
            latest_price=latest_price,
            level_facts=(
                LevelEvaluationFacts(
                    frequency=event.signal_frequency,
                    level=event.signal.level,
                    completed_bar_count=len(structure.completed_bars),
                    latest_bar_closed=structure.operation_bar_closed,
                ),
            ),
        )
        bound_event, evaluation = self._rule_engine.evaluate(
            event,
            runtime_facts,
        )
        if not isinstance(bound_event, DecisionEvent):
            raise TypeError("rule engine must return a DecisionEvent")
        if not isinstance(evaluation, RuleEvaluation):
            raise TypeError("rule engine must return a RuleEvaluation")
        if bound_event.rule_binding_status != "bound":
            raise ValueError("rule engine returned an unbound event")
        if self._event_strategy_run_binder is not None:
            strategy_bound_event = self._event_strategy_run_binder(
                bound_event
            )
            if not isinstance(strategy_bound_event, DecisionEvent):
                raise TypeError(
                    "event_strategy_run_binder must return a DecisionEvent"
                )
            unchanged_fields = set(bound_event.__dataclass_fields__).difference(
                {
                    "event_id",
                    "strategy_run_id",
                    "strategy_run_epoch",
                    "strategy_run_fingerprint",
                }
            )
            if any(
                getattr(strategy_bound_event, field_name)
                != getattr(bound_event, field_name)
                for field_name in unchanged_fields
            ):
                raise ValueError(
                    "event_strategy_run_binder changed immutable event facts"
                )
            if strategy_bound_event.strategy_run_binding_status != "bound":
                raise ValueError(
                    "event_strategy_run_binder returned an unbound event"
                )
            bound_event = strategy_bound_event
        return bound_event, evaluation, runtime_facts

    def _capture_manual_check(
        self,
        event: DecisionEvent,
        runtime_facts: RuleRuntimeFacts,
        evaluation: RuleEvaluation,
    ) -> None:
        if (
            self._manual_check_workflow is None
            or evaluation.verdict is not EvaluationVerdict.WATCH
            or evaluation.safe_to_proceed
        ):
            return
        self._manual_check_workflow.capture_candidate(
            event=event,
            runtime_facts=runtime_facts,
            evaluation=evaluation,
        )

    @staticmethod
    def _bind_candidate(
        decision: CandidateDecision,
        event: DecisionEvent,
        evaluation: RuleEvaluation,
    ) -> CandidateDecision:
        if evaluation.verdict is EvaluationVerdict.WATCH:
            return replace(
                decision,
                event=event,
                accepted=False,
                observation=True,
                reasons=decision.reasons + ("rule_watch",),
                rule_evaluation=evaluation,
            )
        return replace(
            decision,
            event=event,
            rule_evaluation=evaluation,
        )

    @staticmethod
    def _empty_result(bar_closed_at: datetime, code: str) -> ScanResult:
        return ScanResult(bar_closed_at, code, 0, (), (), (), ())
