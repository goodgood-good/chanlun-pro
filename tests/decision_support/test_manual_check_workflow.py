from __future__ import annotations

from contextlib import contextmanager
from dataclasses import replace
from datetime import timedelta

import json
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from chanlun.db_models.decision_support import (
    TableByDecisionEvent,
    TableByDecisionReview,
    TableByDecisionTransition,
    TableByRiskSnapshot,
)
from chanlun.decision_support.event_service import DecisionEventService
from chanlun.decision_support.event_store import (
    DecisionEventStore,
    InvalidEventTransition,
)
from chanlun.decision_support.manual_check_workflow import (
    FileManualCheckStore,
    ManualCheckWorkflow,
    manual_check_snapshot_from_dict,
)
from chanlun.decision_support.manual_checks import ManualCheckSnapshot
from chanlun.decision_support.models import EventState, StrategyTrack
from chanlun.decision_support.rule_cards import (
    AutomationBoundary,
    CompletedBarRequirement,
    DataRequirements,
    EvidenceReference,
    EvaluationVerdict,
    Predicate,
    PredicateMode,
    PredicateOperator,
    RuleCard,
    RuleSet,
)
from chanlun.decision_support.rule_context import (
    LevelEvaluationFacts,
    RuleRuntimeFacts,
)
from chanlun.decision_support.rule_engine import RuleEngine


_CHECK_ID = "chart.third_buy_structure_confirmed"
_EVIDENCE_IDS = ("evidence:lesson-20-third-buy-chart",)


class _Clock:
    def count_closed_bars(self, event, asof) -> int:
        return 0


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


def _rule_engine() -> RuleEngine:
    evidence = EvidenceReference(
        evidence_id=_EVIDENCE_IDS[0],
        lesson=20,
        pdf_pages=(100,),
        lesson_chart_ids=("lesson-20-third-buy-chart",),
    )
    manual_predicate = Predicate(
        predicate_id="predicate.third_buy_chart",
        mode=PredicateMode.MANUAL,
        evidence_ids=_EVIDENCE_IDS,
        manual_check_id=_CHECK_ID,
        prompt="核对次级别回抽不重新跌回中枢。",
    )
    candidate = Predicate(
        predicate_id="predicate.third_buy_signal",
        mode=PredicateMode.MACHINE,
        evidence_ids=_EVIDENCE_IDS,
        field="signal.bs_type",
        operator=PredicateOperator.EQ,
        expected="3buy",
    )
    invalidation = Predicate(
        predicate_id="predicate.stop_breached",
        mode=PredicateMode.MACHINE,
        evidence_ids=_EVIDENCE_IDS,
        field="risk.stop_breached",
        operator=PredicateOperator.IS_TRUE,
    )
    conflict = Predicate(
        predicate_id="predicate.live_divergence_conflict",
        mode=PredicateMode.MACHINE,
        evidence_ids=_EVIDENCE_IDS,
        field="signal.live_divergence",
        operator=PredicateOperator.IS_TRUE,
    )
    project_fields = (
        "signal.level",
        "signal.bs_type",
        "risk.stop_breached",
        "signal.live_divergence",
        "levels.1.completed_bar_count",
        "levels.1.latest_bar_closed",
    )
    card = RuleCard(
        rule_id="fixture.third_buy.manual",
        version=1,
        track=StrategyTrack.TREND_CONTINUATION,
        applicable_levels=(1,),
        algorithm_version="fixture/manual-check/1",
        concepts=("third_buy",),
        evidence=(evidence,),
        counterevidence=(),
        project_fields=project_fields,
        data_requirements=DataRequirements(project_fields, (1,)),
        completed_bar_requirements=(
            CompletedBarRequirement(1, 1, True),
        ),
        candidate_predicates=(candidate,),
        confirmation_predicates=(manual_predicate,),
        invalidation_predicates=(invalidation,),
        conflict_predicates=(conflict,),
        automation_boundary=AutomationBoundary(
            (
                candidate.predicate_id,
                invalidation.predicate_id,
                conflict.predicate_id,
            ),
            (_CHECK_ID,),
        ),
    )
    return RuleEngine(
        RuleSet(
            schema_version=1,
            cards=(card,),
            corpus_manifest_sha256="1" * 64,
            source_pdf_sha256="2" * 64,
        )
    )


def _facts() -> RuleRuntimeFacts:
    return RuleRuntimeFacts(
        fundamental_ok=True,
        comparison_ok=True,
        market_liquid=True,
        risk_allowed=None,
        latest_price=10.0,
        level_facts=(LevelEvaluationFacts("5m", 1, 120, True),),
    )


@pytest.fixture
def workflow_parts(tmp_path, make_decision_event, make_risk_context):
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    for table in (
        TableByDecisionEvent,
        TableByDecisionTransition,
        TableByDecisionReview,
        TableByRiskSnapshot,
    ):
        table.__table__.create(engine)
    event_store = DecisionEventStore(
        sessionmaker(bind=engine, expire_on_commit=False)
    )
    event_service = DecisionEventService(event_store, _Clock())
    rule_engine = _rule_engine()
    event, evaluation = rule_engine.evaluate(make_decision_event(), _facts())
    assert evaluation.verdict is EvaluationVerdict.WATCH
    risk_context = make_risk_context(
        quote_code=event.code,
        quote_time=event.observed_at,
        asof=event.observed_at,
    )
    event_service.register(
        event,
        risk_context,
        rule_evaluation=evaluation,
    )
    clock_now = [event.observed_at + timedelta(seconds=1)]
    store_path = tmp_path / "manual-checks"
    workflow = ManualCheckWorkflow(
        event_service=event_service,
        rule_engine=rule_engine,
        store=FileManualCheckStore(store_path),
        clock=lambda: clock_now[0],
    )
    try:
        yield {
            "clock_now": clock_now,
            "event": event,
            "event_service": event_service,
            "evaluation": evaluation,
            "facts": _facts(),
            "rule_engine": rule_engine,
            "store_path": store_path,
            "workflow": workflow,
        }
    finally:
        engine.dispose()


def _snapshot(parts, **changes) -> ManualCheckSnapshot:
    event = parts["event"]
    values = {
        "manual_check_id": _CHECK_ID,
        "value": True,
        "operator_id": "operator.lc",
        "recorded_at": parts["clock_now"][0],
        "event_id": event.event_id,
        "context_fingerprint": event.data_fingerprint,
        "evidence_ids": _EVIDENCE_IDS,
    }
    return ManualCheckSnapshot(**(values | changes))


def _capture(parts):
    return parts["workflow"].capture_candidate(
        event=parts["event"],
        runtime_facts=parts["facts"],
        evaluation=parts["evaluation"],
    )


def test_first_watch_scan_is_persisted_as_identity_bound_manual_check_pending(
    workflow_parts,
) -> None:
    pending = _capture(workflow_parts)

    assert pending.status == "pending"
    assert pending.event_id == workflow_parts["event"].event_id
    assert pending.context_fingerprint == workflow_parts["event"].data_fingerprint
    assert pending.required_checks[0].manual_check_id == _CHECK_ID
    assert pending.required_checks[0].evidence_ids == _EVIDENCE_IDS
    assert pending.risk_snapshot_id.startswith("risk-snapshot:")

    restarted = FileManualCheckStore(workflow_parts["store_path"])
    assert restarted.get_for_event(pending.event_id) == pending


def test_capture_rejects_old_strategy_run_before_creating_manual_check_record(
    workflow_parts,
) -> None:
    event = workflow_parts["event"]
    workflow_parts["event_service"].bind_strategy_run(_StrategyRunCapability())

    with pytest.raises(InvalidEventTransition, match="strategy run"):
        _capture(workflow_parts)

    assert workflow_parts["workflow"].store.get_for_event(event.event_id) is None
    assert tuple(workflow_parts["store_path"].glob("*.json")) == ()


def test_submit_rejects_old_strategy_run_without_changing_manual_check_record(
    workflow_parts,
) -> None:
    pending = _capture(workflow_parts)
    paths = tuple(workflow_parts["store_path"].glob("*.json"))
    assert len(paths) == 1
    before = paths[0].read_bytes()
    workflow_parts["event_service"].bind_strategy_run(_StrategyRunCapability())

    with pytest.raises(InvalidEventTransition, match="strategy run"):
        workflow_parts["workflow"].submit(
            pending.event_id,
            (_snapshot(workflow_parts),),
        )

    assert paths[0].read_bytes() == before
    restarted = FileManualCheckStore(workflow_parts["store_path"])
    assert restarted.get_for_event(pending.event_id) == pending


def test_all_bound_manual_checks_re_evaluate_same_input_and_advance_review_pending(
    workflow_parts,
) -> None:
    pending = _capture(workflow_parts)

    result = workflow_parts["workflow"].submit(
        pending.event_id,
        (_snapshot(workflow_parts),),
    )

    assert result.accepted is True
    assert result.reasons == ()
    assert result.evaluation is not None
    assert result.evaluation.verdict is EvaluationVerdict.CONFIRM
    assert result.evaluation.evaluation_input_fingerprint == pending.context_fingerprint
    assert result.record.status == "approved"
    assert (
        workflow_parts["event_service"].get(pending.event_id).state
        is EventState.REVIEW_PENDING
    )
    transition = workflow_parts["event_service"].get(pending.event_id).transitions[-1]
    assert transition.actor == "manual_check_workflow"
    assert transition.reason == (
        "manual_check_approved:"
        f"{result.record.pending_id}:{result.record.payload_fingerprint}"
    )


def test_web_boundary_parses_strict_snapshot_and_serializes_submission_result(
    workflow_parts,
) -> None:
    pending = _capture(workflow_parts)
    snapshot = _snapshot(workflow_parts)
    request_payload = {
        "manual_check_id": snapshot.manual_check_id,
        "value": snapshot.value,
        "operator_id": snapshot.operator_id,
        "recorded_at": snapshot.recorded_at.isoformat(),
        "event_id": snapshot.event_id,
        "context_fingerprint": snapshot.context_fingerprint,
        "evidence_ids": list(snapshot.evidence_ids),
    }

    result = workflow_parts["workflow"].submit(
        pending.event_id,
        (manual_check_snapshot_from_dict(request_payload),),
    )
    response_payload = result.to_dict()

    assert response_payload["accepted"] is True
    assert response_payload["record"]["status"] == "approved"
    assert response_payload["evaluation"]["verdict"] == "CONFIRM"
    assert response_payload["record"]["required_checks"][0][
        "manual_check_id"
    ] == _CHECK_ID
    json.dumps(response_payload, ensure_ascii=False, allow_nan=False)


@pytest.mark.parametrize(
    ("changes", "reason"),
    [
        ({"value": False}, "manual_check_failed"),
        ({"event_id": "event:forged"}, "manual_check_event_id_mismatch"),
        (
            {"context_fingerprint": "sha256:" + "f" * 64},
            "manual_check_context_fingerprint_mismatch",
        ),
        (
            {"evidence_ids": ("evidence:unbound",)},
            "manual_check_evidence_ids_mismatch",
        ),
    ],
)
def test_manual_submission_fails_closed_on_false_or_identity_mismatch(
    workflow_parts,
    changes,
    reason,
) -> None:
    pending = _capture(workflow_parts)

    result = workflow_parts["workflow"].submit(
        pending.event_id,
        (_snapshot(workflow_parts, **changes),),
    )

    assert result.accepted is False
    assert reason in result.reasons
    assert result.evaluation is None
    assert result.record.status == "pending"
    assert result.record.attempts[-1].outcome == "rejected"
    assert (
        workflow_parts["event_service"].get(pending.event_id).state
        is EventState.RISK_CHECKED
    )


def test_manual_submission_requires_exact_check_set(workflow_parts) -> None:
    pending = _capture(workflow_parts)

    result = workflow_parts["workflow"].submit(pending.event_id, ())

    assert result.accepted is False
    assert result.reasons == ("manual_check_set_mismatch",)
    assert (
        workflow_parts["event_service"].get(pending.event_id).state
        is EventState.RISK_CHECKED
    )


def test_expired_risk_snapshot_blocks_manual_advance(workflow_parts) -> None:
    pending = _capture(workflow_parts)
    workflow_parts["clock_now"][0] = workflow_parts["event"].observed_at + timedelta(
        minutes=5
    )

    result = workflow_parts["workflow"].submit(
        pending.event_id,
        (_snapshot(workflow_parts),),
    )

    assert result.accepted is False
    assert "risk_snapshot_expired" in result.reasons
    assert (
        workflow_parts["event_service"].get(pending.event_id).state
        is EventState.RISK_CHECKED
    )


def test_risk_is_revalidated_immediately_before_review_transition(
    workflow_parts,
) -> None:
    pending = _capture(workflow_parts)
    times = iter(
        (
            workflow_parts["event"].observed_at + timedelta(seconds=1),
            workflow_parts["event"].observed_at + timedelta(minutes=5),
        )
    )
    workflow_parts["workflow"]._clock = lambda: next(times)

    result = workflow_parts["workflow"].submit(
        pending.event_id,
        (_snapshot(workflow_parts),),
    )

    assert result.accepted is False
    assert "risk_snapshot_expired" in result.reasons
    assert result.record.status == "pending"
    assert [attempt.outcome for attempt in result.record.attempts] == [
        "validated",
        "rejected",
    ]
    assert (
        workflow_parts["event_service"].get(pending.event_id).state
        is EventState.RISK_CHECKED
    )


def test_file_store_detects_persisted_payload_tampering(workflow_parts) -> None:
    pending = _capture(workflow_parts)
    path = workflow_parts["store_path"] / f"{pending.pending_id[15:]}.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["runtime_facts"]["latest_price"] = 999.0
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(
        ValueError,
        match="(pending id|payload fingerprint) mismatch",
    ):
        FileManualCheckStore(workflow_parts["store_path"]).get_for_event(
            pending.event_id
        )


def test_submission_record_survives_restart_before_review_transition(
    workflow_parts,
) -> None:
    pending = _capture(workflow_parts)
    first_store = workflow_parts["workflow"].store
    original_mark = workflow_parts["event_service"].mark_review_pending

    def crash_before_transition(*args, **kwargs):
        raise RuntimeError("simulated process crash")

    workflow_parts["event_service"].mark_review_pending = crash_before_transition
    with pytest.raises(RuntimeError, match="simulated process crash"):
        workflow_parts["workflow"].submit(
            pending.event_id,
            (_snapshot(workflow_parts),),
        )
    validated = first_store.get_for_event(pending.event_id)
    assert validated.attempts[-1].outcome == "validated"
    assert validated.status == "pending"

    workflow_parts["event_service"].mark_review_pending = original_mark
    restarted = ManualCheckWorkflow(
        event_service=workflow_parts["event_service"],
        rule_engine=workflow_parts["rule_engine"],
        store=FileManualCheckStore(workflow_parts["store_path"]),
        clock=lambda: workflow_parts["clock_now"][0],
    )
    result = restarted.submit(pending.event_id, (_snapshot(workflow_parts),))

    assert result.accepted is True
    assert result.record.status == "approved"
    assert (
        workflow_parts["event_service"].get(pending.event_id).state
        is EventState.REVIEW_PENDING
    )


def test_submission_recovers_idempotently_after_transition_before_file_approval(
    workflow_parts,
) -> None:
    pending = _capture(workflow_parts)
    snapshot = _snapshot(workflow_parts)
    store = workflow_parts["workflow"].store
    original_mark = store.mark_advanced

    def crash_after_transition(*args, **kwargs):
        raise RuntimeError("simulated process crash after transition")

    store.mark_advanced = crash_after_transition
    with pytest.raises(RuntimeError, match="after transition"):
        workflow_parts["workflow"].submit(pending.event_id, (snapshot,))

    transition = workflow_parts["event_service"].get(pending.event_id).transitions[-1]
    persisted = FileManualCheckStore(workflow_parts["store_path"]).get_for_event(
        pending.event_id
    )
    assert persisted is not None
    assert persisted.status == "pending"
    assert len(persisted.attempts) == 1

    store.mark_advanced = original_mark
    workflow_parts["clock_now"][0] += timedelta(seconds=1)
    restarted = ManualCheckWorkflow(
        event_service=workflow_parts["event_service"],
        rule_engine=workflow_parts["rule_engine"],
        store=FileManualCheckStore(workflow_parts["store_path"]),
        clock=lambda: workflow_parts["clock_now"][0],
    )
    recovered = restarted.submit(pending.event_id, (snapshot,))

    assert recovered.accepted is True
    assert recovered.record.status == "approved"
    assert len(recovered.record.attempts) == 1
    assert transition.reason == (
        "manual_check_approved:"
        f"{recovered.record.pending_id}:{recovered.record.payload_fingerprint}"
    )


def test_transition_recovery_rejects_a_changed_manual_submission(
    workflow_parts,
) -> None:
    pending = _capture(workflow_parts)
    snapshot = _snapshot(workflow_parts)
    store = workflow_parts["workflow"].store
    original_mark = store.mark_advanced

    def crash_after_transition(*args, **kwargs):
        raise RuntimeError("simulated process crash after transition")

    store.mark_advanced = crash_after_transition
    with pytest.raises(RuntimeError, match="after transition"):
        workflow_parts["workflow"].submit(pending.event_id, (snapshot,))
    store.mark_advanced = original_mark
    workflow_parts["clock_now"][0] += timedelta(seconds=1)
    changed = _snapshot(
        workflow_parts,
        operator_id="operator.forged-retry",
        recorded_at=workflow_parts["clock_now"][0],
    )
    restarted = ManualCheckWorkflow(
        event_service=workflow_parts["event_service"],
        rule_engine=workflow_parts["rule_engine"],
        store=FileManualCheckStore(workflow_parts["store_path"]),
        clock=lambda: workflow_parts["clock_now"][0],
    )

    with pytest.raises(ValueError, match="manual check transition binding mismatch"):
        restarted.submit(pending.event_id, (changed,))

    persisted = restarted.store.get_for_event(pending.event_id)
    assert persisted is not None
    assert persisted.status == "pending"
    assert len(persisted.attempts) == 1


def test_transition_recovery_remains_idempotent_after_review_has_confirmed(
    workflow_parts,
) -> None:
    pending = _capture(workflow_parts)
    snapshot = _snapshot(workflow_parts)
    store = workflow_parts["workflow"].store
    original_mark = store.mark_advanced

    def crash_after_transition(*args, **kwargs):
        raise RuntimeError("simulated process crash after transition")

    store.mark_advanced = crash_after_transition
    with pytest.raises(RuntimeError, match="after transition"):
        workflow_parts["workflow"].submit(pending.event_id, (snapshot,))
    store.mark_advanced = original_mark
    confirmed_at = workflow_parts["clock_now"][0] + timedelta(seconds=1)
    workflow_parts["event_service"].store.append_transition(
        pending.event_id,
        EventState.REVIEW_PENDING,
        EventState.CONFIRMED,
        occurred_at=confirmed_at,
        reason="review_verdict:CONFIRM",
        actor="review:fixture",
    )
    workflow_parts["clock_now"][0] = confirmed_at
    restarted = ManualCheckWorkflow(
        event_service=workflow_parts["event_service"],
        rule_engine=workflow_parts["rule_engine"],
        store=FileManualCheckStore(workflow_parts["store_path"]),
        clock=lambda: workflow_parts["clock_now"][0],
    )

    recovered = restarted.submit(pending.event_id, (snapshot,))
    repeated = restarted.submit(pending.event_id, (snapshot,))

    assert recovered.accepted is True
    assert repeated.accepted is True
    assert repeated.record == recovered.record
    assert len(recovered.record.attempts) == 1
    assert workflow_parts["event_service"].get(pending.event_id).state is EventState.CONFIRMED


def test_approved_submission_is_idempotent_after_risk_snapshot_expires(
    workflow_parts,
) -> None:
    pending = _capture(workflow_parts)
    snapshot = _snapshot(workflow_parts)
    first = workflow_parts["workflow"].submit(pending.event_id, (snapshot,))
    workflow_parts["clock_now"][0] = workflow_parts["event"].observed_at + timedelta(
        hours=1
    )

    repeated = workflow_parts["workflow"].submit(pending.event_id, (snapshot,))

    assert first.accepted is True
    assert repeated.accepted is True
    assert repeated.record == first.record
    assert len(repeated.record.attempts) == 1
    assert (
        workflow_parts["event_service"].get(pending.event_id).state
        is EventState.REVIEW_PENDING
    )


def test_capture_rejects_runtime_facts_that_already_contain_manual_checks(
    workflow_parts,
) -> None:
    snapshot = _snapshot(workflow_parts)

    with pytest.raises(ValueError, match="initial runtime facts"):
        workflow_parts["workflow"].capture_candidate(
            event=workflow_parts["event"],
            runtime_facts=replace(
                workflow_parts["facts"],
                manual_checks=(snapshot,),
            ),
            evaluation=workflow_parts["evaluation"],
        )
