from __future__ import annotations

from datetime import timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from chanlun.db_models.decision_support import (
    TableByDecisionEvent,
    TableByDecisionTransition,
    TableByRiskSnapshot,
    TableByUserDecision,
)
from chanlun.decision_support.api_read_model import (
    DecisionSupportReadModel,
    ReadModelConflict,
    ReadModelNotFound,
)
from chanlun.decision_support.event_store import (
    DecisionEventStore,
    TransitionSpec,
)
from chanlun.decision_support.event_factory import bind_strategy_run_provenance
from chanlun.decision_support.models import EventState
from chanlun.decision_support.risk import RiskPolicy, evaluate_entry
from chanlun.decision_support.risk_snapshot import RiskSnapshot


@pytest.fixture
def read_model_bundle(
    make_bound_decision_event,
    make_risk_context,
):
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    TableByDecisionEvent.__table__.create(engine)
    TableByDecisionTransition.__table__.create(engine)
    TableByUserDecision.__table__.create(engine)
    TableByRiskSnapshot.__table__.create(engine)
    store = DecisionEventStore(
        sessionmaker(bind=engine, expire_on_commit=False)
    )
    trend = make_bound_decision_event(code="SH.600519")
    reversal = make_bound_decision_event(
        code="SZ.000001",
        track="bottom_reversal",
        bs_type="1buy_nest",
        live_divergence=True,
        divergence_kind="qs",
        confirmation_bs_type="2buy",
    )
    for event in (trend, reversal):
        store.append_event(event)
    context = make_risk_context(
        quote_code=trend.code,
        asof=trend.observed_at,
    )
    decision = evaluate_entry(trend, context, RiskPolicy.conservative())
    store.append_risk_snapshot(
        RiskSnapshot.capture(
            event=trend,
            evaluation_input_fingerprint=trend.data_fingerprint,
            decision=decision,
            observed_at=context.asof,
            expires_at=context.asof + timedelta(minutes=5),
        )
    )
    model = DecisionSupportReadModel(
        store,
        clock=lambda: trend.observed_at,
    )
    try:
        yield model, store, trend, reversal
    finally:
        engine.dispose()


def test_candidates_page_is_stable_and_keeps_tracks_separate(
    read_model_bundle,
) -> None:
    model, _store, trend, reversal = read_model_bundle

    first = model.candidates(None, 1)
    second = model.candidates(first["next_cursor"], 1)
    cards = [
        *first["trend"],
        *first["reversal"],
        *second["trend"],
        *second["reversal"],
    ]

    assert {card["event_id"] for card in cards} == {
        trend.event_id,
        reversal.event_id,
    }
    assert {card["strategy_track"] for card in first["trend"]} <= {
        "trend_continuation"
    }
    assert {card["strategy_track"] for card in first["reversal"]} <= {
        "bottom_reversal"
    }
    assert second["next_cursor"] is None


def test_event_detail_closes_over_risk_and_user_audit(read_model_bundle) -> None:
    model, _store, trend, _reversal = read_model_bundle

    decision = model.record_user_decision(
        trend.event_id,
        "operator-1",
        {
            "action": "ignored",
            "event_data_fingerprint": trend.data_fingerprint,
            "idempotency_key": "manual-request-001",
            "note": "人工忽略",
        },
    )
    detail = model.event(trend.event_id)

    assert decision["event_id"] == trend.event_id
    assert detail["event"]["event_id"] == trend.event_id
    assert detail["event_id"] == trend.event_id
    assert detail["event_data_fingerprint"] == trend.data_fingerprint
    assert detail["code"] == trend.code
    assert detail["strategy_track"] == "trend_continuation"
    assert detail["state"] == "detected"
    assert detail["plan"]["direction"] == "buy"
    assert detail["plan"]["entry_price"] == detail["risk_snapshots"][0][
        "decision"
    ]["entry_reference"]
    assert detail["plan"]["stop_price"] == str(
        trend.signal.structural_stop_below
    )
    assert detail["plan"]["position_size"] == detail["risk_snapshots"][0][
        "decision"
    ]["shares"]
    assert detail["plan"]["target_price"] is None
    assert detail["plan"]["exit_rules"][0] == "hard_risk_full_exit"
    assert detail["freshness"] == "fresh"
    assert len(detail["risk_snapshots"]) == 1
    assert detail["risk_snapshots"][0]["event_data_fingerprint"] == (
        trend.data_fingerprint
    )
    assert detail["user_decisions"][0]["action"] == "ignored"


@pytest.mark.parametrize("action", ("accepted", "executed_externally"))
def test_executable_user_actions_require_confirmed_event(
    read_model_bundle,
    action,
) -> None:
    model, store, trend, _reversal = read_model_bundle

    with pytest.raises(ReadModelConflict, match="event_not_confirmed"):
        model.record_user_decision(
            trend.event_id,
            "operator-1",
            {
                "action": action,
                "event_data_fingerprint": trend.data_fingerprint,
                "idempotency_key": f"blocked-{action}",
                "note": "不得制造可执行审计",
            },
        )

    assert store.list_user_decisions(trend.event_id) == ()


def test_confirmed_event_with_fresh_bound_risk_accepts_manual_action(
    read_model_bundle,
) -> None:
    model, store, trend, _reversal = read_model_bundle
    states = (
        EventState.DETECTED,
        EventState.RISK_CHECKED,
        EventState.REVIEW_PENDING,
        EventState.CONFIRMED,
    )
    store.append_transition_chain(
        trend.event_id,
        tuple(
            TransitionSpec(
                from_state=source,
                to_state=target,
                occurred_at=trend.observed_at,
                reason="test-confirmed-lifecycle",
                actor="test",
            )
            for source, target in zip(states, states[1:])
        ),
    )

    stored = model.record_user_decision(
        trend.event_id,
        "operator-1",
        {
            "action": "accepted",
            "event_data_fingerprint": trend.data_fingerprint,
            "idempotency_key": "confirmed-action-001",
            "note": "仅接受决策建议，不自动下单",
        },
    )

    assert stored["action"] == "accepted"


def test_risk_status_reports_fresh_and_missing_snapshots(
    read_model_bundle,
) -> None:
    model, _store, _trend, _reversal = read_model_bundle

    status = model.risk_status()

    assert status["available"] is True
    assert status["event_count"] == 2
    assert status["fresh_snapshot_count"] == 1
    assert status["missing_snapshot_count"] == 1
    assert status["daily_loss_locked"] is False
    assert status["drawdown_locked"] is False


def test_read_model_rejects_unknown_event_and_tampered_cursor(
    read_model_bundle,
) -> None:
    model, _store, _trend, _reversal = read_model_bundle

    with pytest.raises(ReadModelNotFound, match="event_not_found"):
        model.event("missing")
    with pytest.raises(ValueError, match="cursor"):
        model.candidates("tampered", 1)


def test_strategy_scoped_read_model_excludes_other_epochs_and_blocks_actions(
    read_model_bundle,
) -> None:
    _model, store, trend, reversal = read_model_bundle
    current = bind_strategy_run_provenance(
        trend,
        strategy_run_id="paper-run-" + "a" * 64,
        strategy_run_epoch=7,
        strategy_run_fingerprint="sha256:" + "b" * 64,
    )
    other = bind_strategy_run_provenance(
        reversal,
        strategy_run_id="paper-run-" + "c" * 64,
        strategy_run_epoch=8,
        strategy_run_fingerprint="sha256:" + "d" * 64,
    )
    store.append_event(current)
    store.append_event(other)
    scoped = DecisionSupportReadModel(
        store,
        clock=lambda: current.observed_at,
        strategy_run=SimpleNamespace(
            run_id=current.strategy_run_id,
            epoch=current.strategy_run_epoch,
            strategy_run_fingerprint=current.strategy_run_fingerprint,
        ),
    )

    page = scoped.candidates(None, 100)
    cards = [*page["trend"], *page["reversal"]]
    assert [card["event_id"] for card in cards] == [current.event_id]
    assert scoped.risk_status()["event_count"] == 1
    assert scoped.event(current.event_id)["operational_actions_allowed"] is True
    other_detail = scoped.event(other.event_id)
    assert other_detail["current_strategy_run_match"] is False
    assert other_detail["operational_actions_allowed"] is False

    with pytest.raises(ReadModelConflict, match="outside_current_strategy_run"):
        scoped.record_user_decision(
            other.event_id,
            "operator-1",
            {
                "action": "ignored",
                "event_data_fingerprint": other.data_fingerprint,
                "idempotency_key": "wrong-epoch-action",
                "note": "must remain audit-only",
            },
        )
    assert store.list_user_decisions(other.event_id) == ()
