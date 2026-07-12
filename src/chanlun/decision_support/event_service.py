from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import re
from typing import Callable, Protocol

from .event_store import (
    DecisionEventStore,
    EventStateConflictError,
    InvalidEventTransition,
    StoredTransition,
    TransitionSpec,
)
from .fingerprints import normalize_datetime
from .manual_check_binding import (
    MANUAL_CHECK_TRANSITION_ACTOR,
    manual_check_transition_reason,
)
from .models import DecisionEvent, EventState
from .mutation_fence import MutationLeaseGuard, mutation_fenced
from .risk import (
    RiskContext,
    RiskDecision,
    RiskPolicy,
    evaluate_entry,
)
from .risk_snapshot import RiskSnapshot
from .rule_cards import EvaluationVerdict, RuleEvaluation


_FINGERPRINT_RE = re.compile(r"sha256:[0-9a-f]{64}")
_REVIEW_VERDICTS = frozenset({"CONFIRM", "WATCH", "REJECT", "ABSTAIN"})
_REVIEW_TARGETS = {
    "CONFIRM": EventState.CONFIRMED,
    "WATCH": EventState.ABSTAINED,
    "REJECT": EventState.REJECTED,
    "ABSTAIN": EventState.ABSTAINED,
}
_EXPIRABLE_STATES = frozenset(
    {
        EventState.DETECTED,
        EventState.RISK_CHECKED,
        EventState.REVIEW_PENDING,
        EventState.CONFIRMED,
    }
)
_AUDIT_ONLY_STATES = frozenset(
    {
        EventState.EXPIRED,
        EventState.INVALIDATED,
        EventState.ACTED,
    }
)


def _required_text(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{field_name} must be a non-empty string")
    return value


def _real_clock() -> datetime:
    return datetime.now(timezone.utc)


class ClosedBarClock(Protocol):
    def count_closed_bars(
        self,
        event: DecisionEvent,
        asof: datetime,
    ) -> int: ...


class StaleReviewError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class EventView:
    event: DecisionEvent
    state: EventState
    transitions: tuple[StoredTransition, ...]

    @property
    def event_id(self) -> str:
        return self.event.event_id


@dataclass(frozen=True, slots=True)
class RegistrationResult:
    view: EventView
    risk: RiskDecision | None


@dataclass(frozen=True, slots=True)
class ReviewApplication:
    review_id: str
    reviewed_event_id: str
    reviewed_data_fingerprint: str
    verdict: str
    reviewed_at: datetime

    def __post_init__(self) -> None:
        _required_text(self.review_id, "review_id")
        _required_text(self.reviewed_event_id, "reviewed_event_id")
        if (
            not isinstance(self.reviewed_data_fingerprint, str)
            or _FINGERPRINT_RE.fullmatch(
                self.reviewed_data_fingerprint
            )
            is None
        ):
            raise ValueError(
                "reviewed_data_fingerprint must use sha256:<64 lowercase hex>"
            )
        if self.verdict not in _REVIEW_VERDICTS:
            raise ValueError("verdict is invalid")
        object.__setattr__(
            self,
            "reviewed_at",
            normalize_datetime(self.reviewed_at, "reviewed_at"),
        )


@dataclass(frozen=True, slots=True)
class ReviewApplicationResult:
    review_id: str
    event_id: str
    state: EventState
    applied: bool
    reason: str


class DecisionEventService:
    def __init__(
        self,
        store: DecisionEventStore,
        bar_clock: ClosedBarClock,
        *,
        risk_policy: RiskPolicy | None = None,
        freshness_bars: int = 3,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if not isinstance(store, DecisionEventStore):
            raise TypeError("store must be DecisionEventStore")
        if not callable(getattr(bar_clock, "count_closed_bars", None)):
            raise TypeError("bar_clock must count closed bars")
        if (
            isinstance(freshness_bars, bool)
            or not isinstance(freshness_bars, int)
            or freshness_bars <= 0
        ):
            raise ValueError("freshness_bars must be a positive integer")
        if risk_policy is not None and not isinstance(risk_policy, RiskPolicy):
            raise TypeError("risk_policy must be RiskPolicy")
        if clock is not None and not callable(clock):
            raise TypeError("clock must be callable")
        self.store = store
        self._bar_clock = bar_clock
        self._risk_policy = risk_policy or RiskPolicy.conservative()
        self._freshness_bars = freshness_bars
        self._clock = clock or _real_clock
        self._mutation_fence = MutationLeaseGuard()
        self._strategy_run_binding: tuple[str, int, str] | None = None

    def bind_strategy_run(self, strategy_run: object) -> None:
        self._mutation_fence.bind(strategy_run)
        run_id = getattr(strategy_run, "run_id", None)
        epoch = getattr(strategy_run, "epoch", None)
        fingerprint = getattr(
            strategy_run,
            "strategy_run_fingerprint",
            None,
        )
        _required_text(run_id, "strategy_run_id")
        if isinstance(epoch, bool) or not isinstance(epoch, int) or epoch <= 0:
            raise ValueError("strategy_run_epoch must be a positive integer")
        if (
            not isinstance(fingerprint, str)
            or _FINGERPRINT_RE.fullmatch(fingerprint) is None
        ):
            raise ValueError("strategy_run_fingerprint is invalid")
        binding = (run_id, epoch, fingerprint)
        if self._strategy_run_binding not in (None, binding):
            raise ValueError("strategy run binding cannot change")
        self._strategy_run_binding = binding

    def _event_matches_strategy_run(self, event: DecisionEvent) -> bool:
        binding = self._strategy_run_binding
        return binding is None or (
            event.strategy_run_id,
            event.strategy_run_epoch,
            event.strategy_run_fingerprint,
        ) == binding

    def _require_current_strategy_event(self, event: DecisionEvent) -> None:
        if not self._event_matches_strategy_run(event):
            raise InvalidEventTransition(
                "event is outside the current strategy run"
            )

    def get(self, event_id: str) -> EventView:
        snapshot = self.store.get_snapshot(event_id)
        return EventView(
            event=snapshot.event,
            state=snapshot.state,
            transitions=snapshot.transitions,
        )

    @mutation_fenced("decision_event_service.register")
    def register(
        self,
        event: DecisionEvent,
        risk_context: RiskContext,
        *,
        rule_evaluation: RuleEvaluation | None,
    ) -> RegistrationResult:
        self._mutation_fence.require()
        self._require_current_strategy_event(event)
        if event.rule_binding_status == "legacy_unbound":
            raise ValueError("legacy-unbound events are read-only")
        if not isinstance(rule_evaluation, RuleEvaluation):
            raise TypeError("rule_evaluation must be RuleEvaluation")
        binding_fields = (
            "rule_id",
            "rule_card_version",
            "rule_card_fingerprint",
            "rule_set_fingerprint",
            "corpus_manifest_fingerprint",
            "algorithm_fingerprint",
        )
        if (
            event.strategy_track is not rule_evaluation.strategy_track
            or event.signal.level != rule_evaluation.level
            or event.data_fingerprint
            != rule_evaluation.evaluation_input_fingerprint
            or any(
                getattr(event, field_name)
                != getattr(rule_evaluation, field_name)
                for field_name in binding_fields
            )
        ):
            raise ValueError("event and rule evaluation binding mismatch")
        if rule_evaluation.safe_to_proceed is not (
            rule_evaluation.verdict is EvaluationVerdict.CONFIRM
        ):
            raise ValueError("rule evaluation safety state is inconsistent")
        if not isinstance(risk_context, RiskContext):
            raise TypeError("risk_context must be RiskContext")
        self.store.append_event(event)
        snapshot = self.store.get_snapshot(event.event_id)
        risk: RiskDecision | None = None
        if snapshot.state is EventState.DETECTED:
            risk = evaluate_entry(event, risk_context, self._risk_policy)
            if risk_context.asof >= event.observed_at:
                self.store.append_risk_snapshot(
                    RiskSnapshot.capture(
                        event=event,
                        evaluation_input_fingerprint=(
                            rule_evaluation.evaluation_input_fingerprint
                        ),
                        decision=risk,
                        observed_at=risk_context.asof,
                        expires_at=risk_context.asof
                        + timedelta(
                            seconds=self._risk_policy.max_quote_age_seconds
                        ),
                    )
                )
            risk_reason = (
                "risk_allowed"
                if risk.allowed
                else "risk_rejected:" + ",".join(risk.reasons)
            )
            chain = [
                TransitionSpec(
                    EventState.DETECTED,
                    EventState.RISK_CHECKED,
                    risk_context.asof,
                    risk_reason,
                    "risk_engine",
                )
            ]
            if risk.allowed and rule_evaluation.safe_to_proceed:
                chain.append(
                    TransitionSpec(
                        EventState.RISK_CHECKED,
                        EventState.REVIEW_PENDING,
                        risk_context.asof,
                        "risk_allowed",
                        "event_service",
                    )
                )
            self.store.append_transition_chain(event.event_id, tuple(chain))
        elif (
            snapshot.state is EventState.RISK_CHECKED
            and rule_evaluation.safe_to_proceed
            and snapshot.transitions[-1].from_state is EventState.DETECTED
            and snapshot.transitions[-1].reason == "risk_allowed"
        ):
            self.store.append_transition(
                event.event_id,
                EventState.RISK_CHECKED,
                EventState.REVIEW_PENDING,
                occurred_at=risk_context.asof,
                reason="risk_allowed",
                actor="event_service",
            )
        return RegistrationResult(self.get(event.event_id), risk)

    @mutation_fenced("decision_event_service.mark_review_pending")
    def mark_review_pending(
        self,
        event_id: str,
        risk: RiskDecision,
        *,
        occurred_at: datetime,
        manual_check_pending_id: str | None = None,
        manual_check_payload_fingerprint: str | None = None,
    ) -> EventView:
        self._mutation_fence.require()
        if not isinstance(risk, RiskDecision):
            raise TypeError("risk must be RiskDecision")
        if not risk.allowed:
            raise InvalidEventTransition(
                "risk-rejected event cannot enter review pending"
            )
        if (manual_check_pending_id is None) != (
            manual_check_payload_fingerprint is None
        ):
            raise ValueError("manual check transition binding is incomplete")
        reason = "risk_allowed"
        actor = "event_service"
        if manual_check_pending_id is not None:
            reason = manual_check_transition_reason(
                manual_check_pending_id,
                manual_check_payload_fingerprint,
            )
            actor = MANUAL_CHECK_TRANSITION_ACTOR
        self._require_current_strategy_event(self.get(event_id).event)
        self.store.append_transition(
            event_id,
            EventState.RISK_CHECKED,
            EventState.REVIEW_PENDING,
            occurred_at=occurred_at,
            reason=reason,
            actor=actor,
        )
        return self.get(event_id)

    @mutation_fenced("decision_event_service.invalidate")
    def invalidate(
        self,
        event_id: str,
        reason: str,
        *,
        occurred_at: datetime,
    ) -> EventView:
        self._mutation_fence.require()
        reason = _required_text(reason, "reason")
        view = self.get(event_id)
        self._require_current_strategy_event(view.event)
        if view.state is EventState.INVALIDATED:
            return view
        self.store.append_transition(
            event_id,
            view.state,
            EventState.INVALIDATED,
            occurred_at=occurred_at,
            reason=reason,
            actor="event_service",
        )
        return self.get(event_id)

    @mutation_fenced("decision_event_service.expire_stale")
    def expire_stale(self, asof: datetime) -> tuple[EventView, ...]:
        self._mutation_fence.require()
        asof = normalize_datetime(asof, "asof")
        expired: list[EventView] = []
        for event in self.store.list_current_strategy_events():
            if not self._event_matches_strategy_run(event):
                continue
            state = self.store.current_state(event.event_id)
            if state not in _EXPIRABLE_STATES:
                continue
            count = self._bar_clock.count_closed_bars(event, asof)
            if (
                isinstance(count, bool)
                or not isinstance(count, int)
                or count < 0
            ):
                raise ValueError(
                    "bar_clock must return a non-negative integer"
                )
            if count < self._freshness_bars:
                continue
            try:
                self.store.append_transition(
                    event.event_id,
                    state,
                    EventState.EXPIRED,
                    occurred_at=asof,
                    reason="freshness_window_exceeded",
                    actor="event_service",
                )
            except EventStateConflictError:
                continue
            expired.append(self.get(event.event_id))
        return tuple(expired)

    @mutation_fenced("decision_event_service.apply_review")
    def apply_review(
        self,
        review: ReviewApplication,
    ) -> ReviewApplicationResult:
        self._mutation_fence.require()
        if not isinstance(review, ReviewApplication):
            raise TypeError("review must be ReviewApplication")
        trusted_now = normalize_datetime(self._clock(), "clock")
        if review.reviewed_at > trusted_now:
            raise ValueError("reviewed_at cannot be later than trusted clock")
        target = _REVIEW_TARGETS[review.verdict]
        reason = f"review_verdict:{review.verdict}"
        actor = f"review:{review.review_id}"
        snapshot = self.store.get_snapshot(review.reviewed_event_id)
        self._require_current_strategy_event(snapshot.event)
        expire_if_pending = False
        if snapshot.state is EventState.REVIEW_PENDING:
            closed_bars = self._bar_clock.count_closed_bars(
                snapshot.event,
                review.reviewed_at,
            )
            if (
                isinstance(closed_bars, bool)
                or not isinstance(closed_bars, int)
                or closed_bars < 0
            ):
                raise ValueError(
                    "bar_clock must return a non-negative integer"
                )
            expire_if_pending = closed_bars >= self._freshness_bars
        stored = self.store.append_review_application(
            review_id=review.review_id,
            event_id=review.reviewed_event_id,
            reviewed_data_fingerprint=review.reviewed_data_fingerprint,
            verdict=review.verdict,
            reviewed_at=review.reviewed_at,
            target_state=target,
            transition_reason=reason,
            actor=actor,
            expire_if_pending=expire_if_pending,
        )
        if stored.reason == "stale_data_fingerprint":
            raise StaleReviewError(
                "review data fingerprint does not match event"
            )
        if stored.reason == "review_time_before_state":
            raise InvalidEventTransition(
                "review time cannot move lifecycle backwards"
            )
        if (
            stored.reason == "event_not_review_pending"
            and stored.state not in _AUDIT_ONLY_STATES
        ):
            raise InvalidEventTransition(
                f"current state is {stored.state.value}, not review_pending"
            )
        return ReviewApplicationResult(
            review_id=stored.review_id,
            event_id=stored.event_id,
            state=stored.state,
            applied=stored.applied,
            reason=stored.reason,
        )
