from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import replace
from datetime import timedelta
from threading import Event

import pytest
from sqlalchemy import create_engine, event as sqlalchemy_event
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from chanlun.db_models.decision_support import (
    TableByDecisionEvent,
    TableByDecisionReview,
    TableByDecisionTransition,
    TableByRiskSnapshot,
)
from chanlun.decision_support.event_service import (
    DecisionEventService,
    ReviewApplication,
    StaleReviewError,
)
from chanlun.decision_support.event_factory import (
    bind_rule_evaluation,
    bind_strategy_run_provenance,
)
from chanlun.decision_support.event_store import (
    DecisionEventStore,
    InvalidEventTransition,
    ReviewConflictError,
)
from chanlun.decision_support.fingerprints import sha256_json
from chanlun.decision_support.models import DecisionEvent, EventState
from chanlun.decision_support.rule_cards import EvaluationVerdict, RuleEvaluation


class _ClosedBarClock:
    def __init__(self) -> None:
        self.closed_at = ()
        self.on_count = None

    def count_closed_bars(self, event, asof) -> int:
        if self.on_count is not None:
            self.on_count(event)
        return sum(
            event.observed_at < closed_at <= asof
            for closed_at in self.closed_at
        )


class _StrategyRunCapability:
    run_id = "paper-run-" + "a" * 64
    epoch = 7
    strategy_run_fingerprint = "sha256:" + "b" * 64

    def __init__(self) -> None:
        self._held = False

    @contextmanager
    def mutation_lease(self, _operation):
        assert self._held is False
        self._held = True
        try:
            yield object()
        finally:
            self._held = False

    def require_current_mutation_lease(self) -> None:
        if not self._held:
            raise RuntimeError("test mutation lease missing")


def _matching_evaluation(event: DecisionEvent) -> RuleEvaluation:
    return RuleEvaluation(
        rule_id=event.rule_id,
        rule_card_version=event.rule_card_version,
        rule_card_fingerprint=event.rule_card_fingerprint,
        rule_set_fingerprint=event.rule_set_fingerprint,
        corpus_manifest_fingerprint=event.corpus_manifest_fingerprint,
        algorithm_fingerprint=event.algorithm_fingerprint,
        evaluation_input_fingerprint=event.data_fingerprint,
        strategy_track=event.strategy_track,
        level=event.signal.level,
        verdict=EvaluationVerdict.CONFIRM,
        candidate_satisfied=True,
        confirmation_satisfied=True,
        invalidation_triggered=False,
        conflict_triggered=False,
        critical_indeterminate=False,
        safe_to_proceed=True,
        reasons=(),
        evidence_ids=("lesson-20-main", "lesson-20-counter"),
        supporting_evidence_ids=("lesson-20-main",),
        counterevidence_ids=("lesson-20-counter",),
    )


def _legacy_event(bound: DecisionEvent) -> DecisionEvent:
    payload = bound.to_dict()
    for field_name in (
        "rule_id",
        "rule_card_version",
        "rule_card_fingerprint",
        "rule_set_fingerprint",
        "corpus_manifest_fingerprint",
        "algorithm_fingerprint",
    ):
        payload.pop(field_name)
    payload["schema_version"] = 2
    payload["event_id"] = payload["event_id"].rsplit(":P", 1)[0]
    return DecisionEvent.from_dict(payload)


class _TestDecisionEventService(DecisionEventService):
    def register(self, event, risk_context, *, rule_evaluation=None):
        return super().register(
            event,
            risk_context,
            rule_evaluation=rule_evaluation or _matching_evaluation(event),
        )


@pytest.fixture(name="make_decision_event")
def _make_bound_decision_event(make_bound_decision_event):
    return make_bound_decision_event


@pytest.fixture
def service_bundle():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    TableByDecisionEvent.__table__.create(engine)
    TableByDecisionTransition.__table__.create(engine)
    TableByDecisionReview.__table__.create(engine)
    TableByRiskSnapshot.__table__.create(engine)
    store = DecisionEventStore(
        sessionmaker(bind=engine, expire_on_commit=False)
    )
    clock = _ClosedBarClock()
    try:
        yield _TestDecisionEventService(store, clock), clock
    finally:
        engine.dispose()


def _review(event, *, verdict: str = "CONFIRM") -> ReviewApplication:
    return ReviewApplication(
        review_id="review-1",
        reviewed_event_id=event.event_id,
        reviewed_data_fingerprint=event.data_fingerprint,
        verdict=verdict,
        reviewed_at=event.observed_at + timedelta(minutes=1),
    )


def test_register_risk_allowed_event_reaches_review_pending(
    service_bundle,
    make_decision_event,
    make_risk_context,
) -> None:
    service, _ = service_bundle
    event = make_decision_event()

    result = service.register(
        event,
        make_risk_context(asof=event.observed_at),
    )

    assert result.risk is not None
    assert result.risk.allowed is True
    assert result.view.state is EventState.REVIEW_PENDING
    assert tuple(
        item.to_state for item in result.view.transitions
    ) == (EventState.RISK_CHECKED, EventState.REVIEW_PENDING)


def test_strategy_bound_service_rejects_unbound_event_before_any_write(
    service_bundle,
    make_decision_event,
    make_risk_context,
) -> None:
    service, _ = service_bundle
    active = _StrategyRunCapability()
    service.bind_strategy_run(active)
    event = make_decision_event()

    with pytest.raises(InvalidEventTransition, match="strategy run"):
        service.register(
            event,
            make_risk_context(asof=event.observed_at),
        )
    assert service.store.count_events(event.event_id) == 0

    bound = bind_strategy_run_provenance(
        event,
        strategy_run_id=active.run_id,
        strategy_run_epoch=active.epoch,
        strategy_run_fingerprint=active.strategy_run_fingerprint,
    )
    result = service.register(
        bound,
        make_risk_context(asof=bound.observed_at),
    )

    assert result.view.event == bound
    assert result.view.state is EventState.REVIEW_PENDING


def test_register_rejects_legacy_unbound_event_before_write(
    service_bundle,
    make_decision_event,
    make_risk_context,
) -> None:
    service, _ = service_bundle
    event = _legacy_event(make_decision_event())

    with pytest.raises(ValueError, match="legacy-unbound"):
        service.register(
            event,
            make_risk_context(asof=event.observed_at),
            rule_evaluation=None,
        )

    assert service.store.count_events(event.event_id) == 0


def test_register_requires_matching_safe_rule_evaluation(
    service_bundle,
    make_decision_event,
    make_rule_evaluation,
    make_risk_context,
) -> None:
    service, _ = service_bundle
    legacy = make_decision_event()
    evaluation = make_rule_evaluation(legacy)
    event = bind_rule_evaluation(legacy, evaluation)

    result = service.register(
        event,
        make_risk_context(asof=event.observed_at),
        rule_evaluation=evaluation,
    )

    assert result.view.event == event
    assert result.view.state is EventState.REVIEW_PENDING


def test_register_persists_fresh_event_bound_risk_snapshot(
    service_bundle,
    make_decision_event,
    make_rule_evaluation,
    make_risk_context,
) -> None:
    service, _ = service_bundle
    legacy = make_decision_event()
    evaluation = make_rule_evaluation(legacy)
    event = bind_rule_evaluation(legacy, evaluation)
    context = make_risk_context(asof=event.observed_at)

    result = service.register(
        event,
        context,
        rule_evaluation=evaluation,
    )
    snapshots = service.store.list_risk_snapshots(event.event_id)

    assert result.risk is not None
    assert len(snapshots) == 1
    snapshot = snapshots[0]
    assert snapshot.event_id == event.event_id
    assert snapshot.event_data_fingerprint == event.data_fingerprint
    assert snapshot.evaluation_input_fingerprint == event.data_fingerprint
    assert snapshot.decision == result.risk
    assert snapshot.expires_at == context.asof + timedelta(seconds=300)
    assert snapshot.validate_for_review(event, as_of=context.asof).usable is True


def test_register_rejects_mismatched_evaluation_input_before_write(
    service_bundle,
    make_decision_event,
    make_rule_evaluation,
    make_risk_context,
) -> None:
    service, _ = service_bundle
    legacy = make_decision_event()
    evaluation = make_rule_evaluation(legacy)
    event = bind_rule_evaluation(legacy, evaluation)
    mismatched = replace(
        evaluation,
        evaluation_input_fingerprint="sha256:" + "f" * 64,
    )

    with pytest.raises(ValueError, match="binding mismatch"):
        service.register(
            event,
            make_risk_context(asof=event.observed_at),
            rule_evaluation=mismatched,
        )

    assert service.store.count_events(event.event_id) == 0


def test_watch_evaluation_cannot_enter_review_pending(
    service_bundle,
    make_decision_event,
    make_rule_evaluation,
    make_risk_context,
) -> None:
    service, _ = service_bundle
    legacy = make_decision_event()
    evaluation = make_rule_evaluation(
        legacy,
        verdict=EvaluationVerdict.WATCH,
        safe_to_proceed=False,
    )
    event = bind_rule_evaluation(legacy, evaluation)

    result = service.register(
        event,
        make_risk_context(asof=event.observed_at),
        rule_evaluation=evaluation,
    )

    assert result.risk is not None and result.risk.allowed is True
    assert result.view.state is EventState.RISK_CHECKED
    assert EventState.REVIEW_PENDING not in {
        transition.to_state for transition in result.view.transitions
    }


def test_risk_rejected_event_never_reaches_review_pending(
    service_bundle,
    make_decision_event,
    make_risk_context,
) -> None:
    service, _ = service_bundle
    event = make_decision_event(stop_below=None)

    result = service.register(
        event,
        make_risk_context(asof=event.observed_at),
    )

    assert result.risk is not None
    assert result.risk.allowed is False
    assert result.view.state is EventState.RISK_CHECKED
    assert EventState.REVIEW_PENDING not in {
        item.to_state for item in result.view.transitions
    }


def test_risk_rejected_event_is_not_reevaluated_on_reregistration(
    service_bundle,
    make_decision_event,
    make_risk_context,
) -> None:
    service, _ = service_bundle
    event = make_decision_event()
    first = service.register(
        event,
        make_risk_context(
            available_cash="0",
            asof=event.observed_at,
        ),
    )

    second = service.register(
        event,
        make_risk_context(
            asof=event.observed_at + timedelta(minutes=1),
        ),
    )

    assert first.risk is not None
    assert first.risk.allowed is False
    assert first.risk.reasons == ("zero_shares",)
    assert second.risk is None
    assert second.view.state is EventState.RISK_CHECKED
    assert tuple(item.to_state for item in second.view.transitions) == (
        EventState.RISK_CHECKED,
    )
    assert second.view.transitions[0].reason == "risk_rejected:zero_shares"


def test_legacy_risk_allowed_transition_recovers_without_reevaluation(
    service_bundle,
    make_decision_event,
    make_risk_context,
) -> None:
    service, _ = service_bundle
    event = make_decision_event()
    service.store.append_event(event)
    service.store.append_transition(
        event.event_id,
        EventState.DETECTED,
        EventState.RISK_CHECKED,
        occurred_at=event.observed_at,
        reason="risk_allowed",
        actor="risk_engine",
    )

    result = service.register(
        event,
        make_risk_context(
            available_cash="0",
            asof=event.observed_at + timedelta(minutes=1),
        ),
    )

    assert result.risk is None
    assert result.view.state is EventState.REVIEW_PENDING
    assert tuple(item.to_state for item in result.view.transitions) == (
        EventState.RISK_CHECKED,
        EventState.REVIEW_PENDING,
    )


def test_event_expires_after_three_closed_five_minute_bars(
    service_bundle,
    make_decision_event,
    make_risk_context,
) -> None:
    service, clock = service_bundle
    event = make_decision_event()
    service.register(event, make_risk_context(asof=event.observed_at))
    clock.closed_at = tuple(
        event.observed_at + timedelta(minutes=offset)
        for offset in (5, 10, 15)
    )

    expired = service.expire_stale(
        event.observed_at + timedelta(minutes=15)
    )

    assert tuple(item.event_id for item in expired) == (event.event_id,)
    assert service.get(event.event_id).state is EventState.EXPIRED


def test_two_closed_bars_do_not_expire_event(
    service_bundle,
    make_decision_event,
    make_risk_context,
) -> None:
    service, clock = service_bundle
    event = make_decision_event()
    service.register(event, make_risk_context(asof=event.observed_at))
    clock.closed_at = tuple(
        event.observed_at + timedelta(minutes=offset)
        for offset in (5, 10)
    )

    assert service.expire_stale(
        event.observed_at + timedelta(minutes=30)
    ) == ()
    assert service.get(event.event_id).state is EventState.REVIEW_PENDING


def test_invalidation_appends_transition_without_deleting_event(
    service_bundle,
    make_decision_event,
    make_risk_context,
) -> None:
    service, _ = service_bundle
    event = make_decision_event()
    service.register(event, make_risk_context(asof=event.observed_at))

    view = service.invalidate(
        event.event_id,
        "structural_stop_broken",
        occurred_at=event.observed_at + timedelta(minutes=1),
    )

    assert view.event == event
    assert view.state is EventState.INVALIDATED
    assert service.get(event.event_id).event == event


def test_matching_review_confirms_once_and_duplicate_is_idempotent(
    service_bundle,
    make_decision_event,
    make_risk_context,
) -> None:
    service, _ = service_bundle
    event = make_decision_event()
    service.register(event, make_risk_context(asof=event.observed_at))
    review = _review(event)

    first = service.apply_review(review)
    second = service.apply_review(review)

    assert first.applied is True
    assert second == first
    assert service.get(event.event_id).state is EventState.CONFIRMED
    review_transitions = [
        item
        for item in service.get(event.event_id).transitions
        if item.from_state is EventState.REVIEW_PENDING
    ]
    assert len(review_transitions) == 1
    assert service.store.count_reviews(event.event_id) == 1


def test_review_after_trusted_clock_is_rejected_before_any_write(
    service_bundle,
    make_decision_event,
    make_risk_context,
) -> None:
    original_service, bar_clock = service_bundle
    event = make_decision_event()
    trusted_now = event.observed_at + timedelta(minutes=1)
    service = _TestDecisionEventService(
        original_service.store,
        bar_clock,
        clock=lambda: trusted_now,
    )
    service.register(event, make_risk_context(asof=event.observed_at))
    review = ReviewApplication(
        review_id="future-review",
        reviewed_event_id=event.event_id,
        reviewed_data_fingerprint=event.data_fingerprint,
        verdict="CONFIRM",
        reviewed_at=trusted_now + timedelta(microseconds=1),
    )

    with pytest.raises(ValueError, match="later than trusted clock"):
        service.apply_review(review)

    assert service.store.count_reviews(event.event_id) == 0
    assert service.get(event.event_id).state is EventState.REVIEW_PENDING
    assert not any(
        item.from_state is EventState.REVIEW_PENDING
        for item in service.store.list_transitions(event.event_id)
    )


def test_stale_review_fingerprint_cannot_transition_event(
    service_bundle,
    make_decision_event,
    make_risk_context,
) -> None:
    service, _ = service_bundle
    event = make_decision_event()
    service.register(event, make_risk_context(asof=event.observed_at))
    review = ReviewApplication(
        review_id="stale-review",
        reviewed_event_id=event.event_id,
        reviewed_data_fingerprint=sha256_json({"stale": True}),
        verdict="CONFIRM",
        reviewed_at=event.observed_at + timedelta(minutes=1),
    )

    with pytest.raises(StaleReviewError, match="fingerprint"):
        service.apply_review(review)

    assert service.get(event.event_id).state is EventState.REVIEW_PENDING
    reviews = service.store.list_reviews(event.event_id)
    assert len(reviews) == 1
    assert reviews[0].applied is False
    assert reviews[0].reason == "stale_data_fingerprint"


def test_review_after_expiry_is_audit_only_and_cannot_confirm(
    service_bundle,
    make_decision_event,
    make_risk_context,
) -> None:
    service, clock = service_bundle
    event = make_decision_event()
    service.register(event, make_risk_context(asof=event.observed_at))
    clock.closed_at = tuple(
        event.observed_at + timedelta(minutes=offset)
        for offset in (5, 10, 15)
    )
    service.expire_stale(event.observed_at + timedelta(minutes=15))

    result = service.apply_review(_review(event))

    assert result.applied is False
    assert result.state is EventState.EXPIRED
    assert result.reason == "event_not_review_pending"
    assert service.get(event.event_id).state is EventState.EXPIRED
    reviews = service.store.list_reviews(event.event_id)
    assert len(reviews) == 1
    assert reviews[0].review_id == result.review_id
    assert reviews[0].applied is False
    assert reviews[0].state is EventState.EXPIRED


def test_review_at_freshness_boundary_expires_and_is_audit_only(
    service_bundle,
    make_decision_event,
    make_risk_context,
) -> None:
    service, clock = service_bundle
    event = make_decision_event()
    service.register(event, make_risk_context(asof=event.observed_at))
    reviewed_at = event.observed_at + timedelta(minutes=15)
    clock.closed_at = tuple(
        event.observed_at + timedelta(minutes=offset)
        for offset in (5, 10, 15)
    )
    review = ReviewApplication(
        review_id="boundary-review",
        reviewed_event_id=event.event_id,
        reviewed_data_fingerprint=event.data_fingerprint,
        verdict="CONFIRM",
        reviewed_at=reviewed_at,
    )

    result = service.apply_review(review)

    assert result.applied is False
    assert result.state is EventState.EXPIRED
    reviews = service.store.list_reviews(event.event_id)
    assert len(reviews) == 1
    assert reviews[0].review_id == review.review_id
    lifecycle = [
        item
        for item in service.get(event.event_id).transitions
        if item.from_state is EventState.REVIEW_PENDING
    ]
    assert len(lifecycle) == 1
    assert lifecycle[0].to_state is EventState.EXPIRED


def test_boundary_review_and_expiry_roll_back_together_on_flush_failure(
    service_bundle,
    make_decision_event,
    make_risk_context,
) -> None:
    service, clock = service_bundle
    event = make_decision_event()
    service.register(event, make_risk_context(asof=event.observed_at))
    reviewed_at = event.observed_at + timedelta(minutes=15)
    clock.closed_at = tuple(
        event.observed_at + timedelta(minutes=offset)
        for offset in (5, 10, 15)
    )
    review = ReviewApplication(
        review_id="rollback-boundary-review",
        reviewed_event_id=event.event_id,
        reviewed_data_fingerprint=event.data_fingerprint,
        verdict="REJECT",
        reviewed_at=reviewed_at,
    )
    session_class = service.store._session_factory.class_

    def fail_review_flush(session, flush_context, instances):
        has_review = any(
            isinstance(row, TableByDecisionReview) for row in session.new
        )
        has_transition = any(
            isinstance(row, TableByDecisionTransition) for row in session.new
        )
        if has_review and has_transition:
            raise RuntimeError("injected atomic review failure")

    sqlalchemy_event.listen(session_class, "before_flush", fail_review_flush)
    try:
        with pytest.raises(RuntimeError, match="injected atomic review failure"):
            service.apply_review(review)
    finally:
        sqlalchemy_event.remove(
            session_class,
            "before_flush",
            fail_review_flush,
        )

    assert service.get(event.event_id).state is EventState.REVIEW_PENDING
    assert service.store.count_reviews(event.event_id) == 0
    assert not any(
        item.from_state is EventState.REVIEW_PENDING
        for item in service.store.list_transitions(event.event_id)
    )

    result = service.apply_review(review)

    assert result.applied is False
    assert result.state is EventState.EXPIRED
    assert service.store.count_reviews(event.event_id) == 1
    lifecycle = [
        item
        for item in service.store.list_transitions(event.event_id)
        if item.from_state is EventState.REVIEW_PENDING
    ]
    assert len(lifecycle) == 1
    assert lifecycle[0].to_state is EventState.EXPIRED


def test_duplicate_review_remains_idempotent_after_event_is_acted(
    service_bundle,
    make_decision_event,
    make_risk_context,
) -> None:
    service, _ = service_bundle
    event = make_decision_event()
    service.register(event, make_risk_context(asof=event.observed_at))
    review = _review(event)
    first = service.apply_review(review)
    service.store.append_transition(
        event.event_id,
        EventState.CONFIRMED,
        EventState.ACTED,
        occurred_at=event.observed_at + timedelta(minutes=2),
        reason="user_confirmed",
        actor="user:test",
    )

    duplicate = service.apply_review(review)

    assert duplicate == first
    assert service.get(event.event_id).state is EventState.ACTED
    assert service.store.count_reviews(event.event_id) == 1


def test_same_review_id_with_different_payload_is_conflict(
    service_bundle,
    make_decision_event,
    make_risk_context,
) -> None:
    service, _ = service_bundle
    event = make_decision_event()
    service.register(event, make_risk_context(asof=event.observed_at))
    service.apply_review(_review(event))
    conflicting = ReviewApplication(
        review_id="review-1",
        reviewed_event_id=event.event_id,
        reviewed_data_fingerprint=event.data_fingerprint,
        verdict="REJECT",
        reviewed_at=event.observed_at + timedelta(minutes=1),
    )

    with pytest.raises(ReviewConflictError, match="review_id"):
        service.apply_review(conflicting)

    assert service.store.count_reviews(event.event_id) == 1


def test_competing_opposite_reviews_have_deterministic_conflict_result(
    service_bundle,
    make_decision_event,
    make_risk_context,
) -> None:
    service, _ = service_bundle

    for index in range(5):
        code = f"SH.6001{index:02d}"
        event = make_decision_event(code=code)
        service.register(
            event,
            make_risk_context(
                quote_code=code,
                asof=event.observed_at,
            ),
        )
        winner = ReviewApplication(
            review_id=f"competing-{index}-winner",
            reviewed_event_id=event.event_id,
            reviewed_data_fingerprint=event.data_fingerprint,
            verdict="CONFIRM",
            reviewed_at=event.observed_at + timedelta(minutes=1),
        )
        loser = ReviewApplication(
            review_id=f"competing-{index}-loser",
            reviewed_event_id=event.event_id,
            reviewed_data_fingerprint=event.data_fingerprint,
            verdict="REJECT",
            reviewed_at=event.observed_at + timedelta(minutes=1),
        )
        original_append = service.store.append_review_application
        loser_reached_store = Event()
        winner_committed = Event()

        def ordered_append(**kwargs):
            if kwargs["review_id"] == loser.review_id:
                loser_reached_store.set()
                assert winner_committed.wait(timeout=5)
                return original_append(**kwargs)
            assert loser_reached_store.wait(timeout=5)
            stored = original_append(**kwargs)
            winner_committed.set()
            return stored

        service.store.append_review_application = ordered_append
        try:
            with ThreadPoolExecutor(max_workers=2) as executor:
                loser_future = executor.submit(service.apply_review, loser)
                winner_future = executor.submit(service.apply_review, winner)
                results = (winner_future.result(), loser_future.result())
        finally:
            service.store.append_review_application = original_append

        assert sum(result.applied for result in results) == 1
        conflict = next(result for result in results if not result.applied)
        assert conflict.reason == "review_transition_conflict"
        assert service.store.count_reviews(event.event_id) == 2
        review_transitions = [
            item
            for item in service.get(event.event_id).transitions
            if item.from_state is EventState.REVIEW_PENDING
        ]
        assert len(review_transitions) == 1


def test_expiry_cas_race_does_not_abort_remaining_events(
    service_bundle,
    make_decision_event,
    make_risk_context,
) -> None:
    service, clock = service_bundle
    first = make_decision_event(code="SH.600001")
    second = make_decision_event(code="SH.600002")
    service.register(
        first,
        make_risk_context(
            quote_code=first.code,
            asof=first.observed_at,
        ),
    )
    service.register(
        second,
        make_risk_context(
            quote_code=second.code,
            asof=second.observed_at,
        ),
    )
    clock.closed_at = tuple(
        first.observed_at + timedelta(minutes=offset)
        for offset in (5, 10, 15)
    )
    raced = False

    def confirm_first_during_count(event):
        nonlocal raced
        if event.event_id == first.event_id and not raced:
            raced = True
            service.apply_review(_review(first))

    clock.on_count = confirm_first_during_count

    expired = service.expire_stale(
        first.observed_at + timedelta(minutes=15)
    )

    assert tuple(item.event_id for item in expired) == (second.event_id,)
    assert service.get(first.event_id).state is EventState.CONFIRMED
    assert service.get(second.event_id).state is EventState.EXPIRED


def test_expiry_propagates_transition_timestamp_regression(
    service_bundle,
    make_decision_event,
    make_risk_context,
) -> None:
    service, clock = service_bundle
    event = make_decision_event()
    risk_checked_at = event.observed_at + timedelta(minutes=10)
    service.register(
        event,
        make_risk_context(asof=risk_checked_at),
    )
    clock.closed_at = tuple(
        event.observed_at + timedelta(minutes=offset)
        for offset in (1, 2, 3)
    )

    with pytest.raises(
        InvalidEventTransition,
        match="transition time cannot move backwards",
    ):
        service.expire_stale(event.observed_at + timedelta(minutes=5))

    assert service.get(event.event_id).state is EventState.REVIEW_PENDING


def test_service_get_uses_atomic_store_snapshot(
    service_bundle,
    make_decision_event,
) -> None:
    service, _ = service_bundle
    event = make_decision_event()
    service.store.append_event(event)

    def forbidden_split_read(*args, **kwargs):
        raise AssertionError("split read is not allowed")

    service.store.get_event = forbidden_split_read
    service.store.current_state = forbidden_split_read
    service.store.list_transitions = forbidden_split_read

    view = service.get(event.event_id)

    assert view.event == event
    assert view.state is EventState.DETECTED
