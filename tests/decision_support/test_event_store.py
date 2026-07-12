from __future__ import annotations

from contextlib import nullcontext
from dataclasses import replace
from datetime import timedelta
import json
from threading import Event, Thread

import pytest
from sqlalchemy import (
    Integer,
    create_engine,
    event as sqlalchemy_event,
    inspect,
    select,
    update,
)
from sqlalchemy.dialects import mysql
from sqlalchemy.schema import CreateTable
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from chanlun.db_models.decision_support import (
    TableByDecisionEvent,
    TableByDecisionReview,
    TableByDecisionTransition,
)
from chanlun.decision_support.event_store import (
    DecisionEventStore,
    EventConflictError,
    InvalidEventTransition,
    TransitionSpec,
)
from chanlun.decision_support import event_store as event_store_module
from chanlun.decision_support.event_factory import bind_strategy_run_provenance
from chanlun.decision_support.fingerprints import sha256_json
from chanlun.decision_support.models import EventState


_STRATEGY_RUN_ID = "paper-run:20260715T010203000000Z:00000001"
_STRATEGY_RUN_EPOCH = 1
_STRATEGY_RUN_FINGERPRINT = "sha256:" + "a" * 64


def _strategy_bound_event(
    make_bound_decision_event,
    *,
    strategy_run_id=_STRATEGY_RUN_ID,
    strategy_run_epoch=_STRATEGY_RUN_EPOCH,
    strategy_run_fingerprint=_STRATEGY_RUN_FINGERPRINT,
    **event_changes,
):
    return bind_strategy_run_provenance(
        make_bound_decision_event(**event_changes),
        strategy_run_id=strategy_run_id,
        strategy_run_epoch=strategy_run_epoch,
        strategy_run_fingerprint=strategy_run_fingerprint,
    )


@pytest.fixture
def event_store_engine():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    TableByDecisionEvent.__table__.create(engine)
    TableByDecisionTransition.__table__.create(engine)
    TableByDecisionReview.__table__.create(engine)
    try:
        yield engine
    finally:
        engine.dispose()


@pytest.fixture
def event_store(event_store_engine) -> DecisionEventStore:
    session_factory = sessionmaker(
        bind=event_store_engine,
        expire_on_commit=False,
    )
    return DecisionEventStore(session_factory)


def _append_transition(
    event_store: DecisionEventStore,
    event_id: str,
    from_state: EventState,
    to_state: EventState,
    occurred_at,
):
    return event_store.append_transition(
        event_id,
        from_state,
        to_state,
        occurred_at=occurred_at,
        reason=f"{from_state.value}_to_{to_state.value}",
        actor="pytest",
    )


def test_append_same_event_is_idempotent(
    event_store,
    make_decision_event,
) -> None:
    event = make_decision_event()

    first = event_store.append_event(event)
    second = event_store.append_event(event)

    assert first == event
    assert second == event
    assert event_store.count_events(event.event_id) == 1


def test_append_same_id_with_different_payload_raises(
    event_store,
    make_decision_event,
) -> None:
    event = make_decision_event()
    event_store.append_event(event)
    changed = replace(
        event,
        data_fingerprint=sha256_json({"fixture": "changed"}),
    )

    with pytest.raises(EventConflictError, match="immutable event conflict"):
        event_store.append_event(changed)

    assert event_store.get_event(event.event_id) == event
    assert event_store.count_events(event.event_id) == 1


def test_get_and_list_events_round_trip_canonical_payload(
    event_store,
    make_decision_event,
) -> None:
    first = make_decision_event()
    second_time = first.observed_at + timedelta(minutes=5)
    second = make_decision_event(
        observed_at=second_time,
        quote_time=second_time,
    )
    event_store.append_event(second)
    event_store.append_event(first)

    assert event_store.get_event(first.event_id) == first
    assert event_store.get_event("missing") is None
    assert event_store.list_events(market="a", code=first.code) == (
        first,
        second,
    )


def test_get_and_list_events_round_trip_bound_v3_payload(
    event_store,
    make_bound_decision_event,
) -> None:
    event = make_bound_decision_event()

    event_store.append_event(event)

    assert event.to_dict()["schema_version"] == 3
    assert event_store.get_event(event.event_id) == event
    assert event_store.list_events(market="a", code=event.code) == (event,)


def test_append_v4_writes_strategy_run_columns_and_round_trips(
    event_store,
    event_store_engine,
    make_bound_decision_event,
) -> None:
    event = _strategy_bound_event(make_bound_decision_event)

    event_store.append_event(event)

    with event_store_engine.connect() as connection:
        row = connection.execute(
            select(
                TableByDecisionEvent.strategy_run_id,
                TableByDecisionEvent.strategy_run_epoch,
                TableByDecisionEvent.strategy_run_fingerprint,
            ).where(TableByDecisionEvent.event_id == event.event_id)
        ).one()
    assert tuple(row) == (
        _STRATEGY_RUN_ID,
        _STRATEGY_RUN_EPOCH,
        _STRATEGY_RUN_FINGERPRINT,
    )
    assert event_store.get_event(event.event_id) == event
    assert event_store.list_events(code=event.code) == (event,)


def test_bound_store_rejects_foreign_strategy_run_event_before_insert(
    event_store,
    make_bound_decision_event,
) -> None:
    class ActiveRun:
        run_id = _STRATEGY_RUN_ID
        epoch = _STRATEGY_RUN_EPOCH
        strategy_run_fingerprint = _STRATEGY_RUN_FINGERPRINT

        @staticmethod
        def mutation_lease(_operation):
            return nullcontext()

        @staticmethod
        def require_current_mutation_lease() -> None:
            return None

    event_store.bind_strategy_run(ActiveRun())
    foreign = _strategy_bound_event(
        make_bound_decision_event,
        strategy_run_id="paper-run:20260715T010203000000Z:00000002",
        strategy_run_epoch=2,
        strategy_run_fingerprint="sha256:" + "b" * 64,
    )

    with pytest.raises(EventConflictError, match="outside_current_strategy_run"):
        event_store.append_event(foreign)

    assert event_store.count_events() == 0
    current = _strategy_bound_event(make_bound_decision_event)
    assert event_store.append_event(current) == current


def test_bound_store_rejects_transition_of_preexisting_foreign_run_event(
    event_store,
    make_bound_decision_event,
) -> None:
    foreign = _strategy_bound_event(
        make_bound_decision_event,
        strategy_run_id="paper-run:20260715T010203000000Z:00000002",
        strategy_run_epoch=2,
        strategy_run_fingerprint="sha256:" + "b" * 64,
    )
    event_store.append_event(foreign)

    class ActiveRun:
        run_id = _STRATEGY_RUN_ID
        epoch = _STRATEGY_RUN_EPOCH
        strategy_run_fingerprint = _STRATEGY_RUN_FINGERPRINT

        @staticmethod
        def mutation_lease(_operation):
            return nullcontext()

        @staticmethod
        def require_current_mutation_lease() -> None:
            return None

    event_store.bind_strategy_run(ActiveRun())

    with pytest.raises(EventConflictError, match="outside_current_strategy_run"):
        _append_transition(
            event_store,
            foreign.event_id,
            EventState.DETECTED,
            EventState.RISK_CHECKED,
            foreign.observed_at,
        )

    assert event_store.count_transitions(foreign.event_id) == 0


def test_bound_store_lists_current_strategy_run_with_exact_sql_scope(
    event_store,
    event_store_engine,
    make_bound_decision_event,
) -> None:
    current = _strategy_bound_event(make_bound_decision_event)
    foreign = _strategy_bound_event(
        make_bound_decision_event,
        strategy_run_id="paper-run:20260715T010203000000Z:00000002",
        strategy_run_epoch=2,
        strategy_run_fingerprint="sha256:" + "b" * 64,
    )
    event_store.append_event(current)
    event_store.append_event(foreign)

    class ActiveRun:
        run_id = _STRATEGY_RUN_ID
        epoch = _STRATEGY_RUN_EPOCH
        strategy_run_fingerprint = _STRATEGY_RUN_FINGERPRINT

        @staticmethod
        def mutation_lease(_operation):
            return nullcontext()

        @staticmethod
        def require_current_mutation_lease() -> None:
            return None

    event_store.bind_strategy_run(ActiveRun())
    with event_store_engine.begin() as connection:
        connection.execute(
            update(TableByDecisionEvent)
            .where(TableByDecisionEvent.event_id == foreign.event_id)
            .values(payload_json="{}")
        )

    assert event_store.list_current_strategy_events() == (current,)


def test_store_binding_publishes_guard_and_run_identity_atomically(
    event_store,
    make_bound_decision_event,
    monkeypatch,
) -> None:
    class ActiveRun:
        run_id = _STRATEGY_RUN_ID
        epoch = _STRATEGY_RUN_EPOCH
        strategy_run_fingerprint = _STRATEGY_RUN_FINGERPRINT

        @staticmethod
        def mutation_lease(_operation):
            return nullcontext()

        @staticmethod
        def require_current_mutation_lease() -> None:
            return None

    active = ActiveRun()
    foreign = _strategy_bound_event(
        make_bound_decision_event,
        strategy_run_id="paper-run:20260715T010203000000Z:00000002",
        strategy_run_epoch=2,
        strategy_run_fingerprint="sha256:" + "b" * 64,
    )
    guard_bound = Event()
    allow_binding_return = Event()
    original_bind = event_store._mutation_fence.bind

    def delayed_guard_bind(strategy_run, **kwargs):
        original_bind(strategy_run, **kwargs)
        guard_bound.set()
        assert allow_binding_return.wait(timeout=5)

    monkeypatch.setattr(
        event_store._mutation_fence,
        "bind",
        delayed_guard_bind,
    )
    bind_errors: list[BaseException] = []
    write_errors: list[BaseException] = []
    write_done = Event()

    def bind() -> None:
        try:
            event_store.bind_strategy_run(active)
        except BaseException as exc:
            bind_errors.append(exc)

    def write() -> None:
        try:
            event_store.append_event(foreign)
        except BaseException as exc:
            write_errors.append(exc)
        finally:
            write_done.set()

    binder = Thread(target=bind)
    binder.start()
    assert guard_bound.wait(timeout=5)
    writer = Thread(target=write)
    writer.start()
    write_done.wait(timeout=1)
    allow_binding_return.set()
    binder.join(timeout=5)
    writer.join(timeout=5)

    assert binder.is_alive() is False
    assert writer.is_alive() is False
    assert bind_errors == []
    assert len(write_errors) == 1
    assert isinstance(write_errors[0], EventConflictError)
    assert str(write_errors[0]) == "event_outside_current_strategy_run"
    assert event_store.count_events() == 0


@pytest.mark.parametrize(
    ("column_name", "bad_value"),
    (
        ("strategy_run_id", None),
        ("strategy_run_epoch", _STRATEGY_RUN_EPOCH + 1),
        ("strategy_run_fingerprint", "sha256:" + "b" * 64),
    ),
)
def test_v4_read_rejects_strategy_run_column_disagreement(
    event_store,
    event_store_engine,
    make_bound_decision_event,
    column_name,
    bad_value,
) -> None:
    event = _strategy_bound_event(make_bound_decision_event)
    event_store.append_event(event)
    with event_store_engine.begin() as connection:
        connection.execute(
            update(TableByDecisionEvent)
            .where(TableByDecisionEvent.event_id == event.event_id)
            .values({column_name: bad_value})
        )

    with pytest.raises(
        EventConflictError,
        match="strategy-run columns disagree with payload",
    ):
        event_store.get_event(event.event_id)


@pytest.mark.parametrize("schema_version", (2, 3))
def test_legacy_event_requires_null_strategy_run_columns(
    event_store,
    event_store_engine,
    make_decision_event,
    make_bound_decision_event,
    schema_version,
) -> None:
    event = (
        make_decision_event()
        if schema_version == 2
        else make_bound_decision_event()
    )
    event_store.append_event(event)
    with event_store_engine.connect() as connection:
        row = connection.execute(
            select(
                TableByDecisionEvent.strategy_run_id,
                TableByDecisionEvent.strategy_run_epoch,
                TableByDecisionEvent.strategy_run_fingerprint,
            ).where(TableByDecisionEvent.event_id == event.event_id)
        ).one()
    assert tuple(row) == (None, None, None)

    with event_store_engine.begin() as connection:
        connection.execute(
            update(TableByDecisionEvent)
            .where(TableByDecisionEvent.event_id == event.event_id)
            .values(strategy_run_epoch=1)
        )

    with pytest.raises(
        EventConflictError,
        match="strategy-run columns disagree with payload",
    ):
        event_store.get_event(event.event_id)


def test_list_events_filters_by_exact_strategy_run_triple(
    event_store,
    make_bound_decision_event,
) -> None:
    first = _strategy_bound_event(make_bound_decision_event)
    second = _strategy_bound_event(
        make_bound_decision_event,
        strategy_run_id="paper-run:20260715T010203000000Z:00000002",
        strategy_run_epoch=2,
        strategy_run_fingerprint="sha256:" + "b" * 64,
    )
    event_store.append_event(first)
    event_store.append_event(second)

    assert event_store.list_events(
        strategy_run_id=_STRATEGY_RUN_ID,
        strategy_run_epoch=_STRATEGY_RUN_EPOCH,
        strategy_run_fingerprint=_STRATEGY_RUN_FINGERPRINT,
    ) == (first,)


@pytest.mark.parametrize(
    "filters",
    (
        {"strategy_run_id": _STRATEGY_RUN_ID},
        {"strategy_run_epoch": _STRATEGY_RUN_EPOCH},
        {"strategy_run_fingerprint": _STRATEGY_RUN_FINGERPRINT},
        {
            "strategy_run_id": _STRATEGY_RUN_ID,
            "strategy_run_epoch": _STRATEGY_RUN_EPOCH,
        },
        {
            "strategy_run_id": _STRATEGY_RUN_ID,
            "strategy_run_fingerprint": _STRATEGY_RUN_FINGERPRINT,
        },
        {
            "strategy_run_epoch": _STRATEGY_RUN_EPOCH,
            "strategy_run_fingerprint": _STRATEGY_RUN_FINGERPRINT,
        },
    ),
)
def test_list_events_requires_complete_strategy_run_filter(
    event_store,
    filters,
) -> None:
    with pytest.raises(ValueError, match="must be provided together"):
        event_store.list_events(**filters)


@pytest.mark.parametrize(
    "read_path",
    ("get_event", "get_snapshot", "list_events"),
)
def test_event_read_paths_wrap_partial_v3_binding_as_event_conflict(
    event_store,
    event_store_engine,
    make_bound_decision_event,
    read_path,
) -> None:
    event = make_bound_decision_event()
    event_store.append_event(event)
    payload = event.to_dict()
    del payload["algorithm_fingerprint"]
    with event_store_engine.begin() as connection:
        connection.execute(
            update(TableByDecisionEvent)
            .where(TableByDecisionEvent.event_id == event.event_id)
            .values(payload_json=json.dumps(payload))
        )

    readers = {
        "get_event": lambda: event_store.get_event(event.event_id),
        "get_snapshot": lambda: event_store.get_snapshot(event.event_id),
        "list_events": lambda: event_store.list_events(code=event.code),
    }
    with pytest.raises(
        EventConflictError,
        match="stored event payload is invalid",
    ) as caught:
        readers[read_path]()

    assert isinstance(caught.value.__cause__, ValueError)


def test_event_read_wraps_model_type_error_as_event_conflict(
    event_store,
    make_bound_decision_event,
    monkeypatch,
) -> None:
    event = make_bound_decision_event()
    event_store.append_event(event)

    def reject_payload(cls, payload):
        raise TypeError("injected model type failure")

    monkeypatch.setattr(
        event_store_module.DecisionEvent,
        "from_dict",
        classmethod(reject_payload),
    )

    with pytest.raises(
        EventConflictError,
        match="stored event payload is invalid",
    ) as caught:
        event_store.get_event(event.event_id)

    assert isinstance(caught.value.__cause__, TypeError)


def test_event_tables_have_physical_uniqueness(event_store_engine) -> None:
    inspector = inspect(event_store_engine)

    assert {
        "cl_decision_event",
        "cl_decision_review",
        "cl_decision_transition",
    }.issubset(inspector.get_table_names())
    event_constraints = {
        item["name"]
        for item in inspector.get_unique_constraints("cl_decision_event")
    }
    transition_constraints = {
        item["name"]
        for item in inspector.get_unique_constraints(
            "cl_decision_transition"
        )
    }
    review_constraints = {
        item["name"]
        for item in inspector.get_unique_constraints("cl_decision_review")
    }
    assert "uq_cl_decision_event_event_id" in event_constraints
    assert (
        "uq_cl_decision_transition_event_from_state"
        in transition_constraints
    )
    assert "uq_cl_decision_review_review_id" in review_constraints


def test_decision_event_strategy_run_columns_and_observed_index() -> None:
    table = TableByDecisionEvent.__table__
    strategy_run_id = table.c.strategy_run_id
    strategy_run_epoch = table.c.strategy_run_epoch
    strategy_run_fingerprint = table.c.strategy_run_fingerprint

    mysql_dialect = mysql.dialect()
    strategy_run_id_type = strategy_run_id.type.dialect_impl(mysql_dialect)
    strategy_run_epoch_type = strategy_run_epoch.type.dialect_impl(mysql_dialect)
    strategy_run_fingerprint_type = strategy_run_fingerprint.type.dialect_impl(
        mysql_dialect
    )

    assert strategy_run_id.nullable is True
    assert strategy_run_id_type.length == 80
    assert strategy_run_id_type.collation == "utf8mb4_bin"
    assert strategy_run_epoch.nullable is True
    assert isinstance(strategy_run_epoch_type, Integer)
    assert strategy_run_fingerprint.nullable is True
    assert strategy_run_fingerprint_type.length == 71
    assert strategy_run_fingerprint_type.collation == "utf8mb4_bin"
    indexes = {
        index.name: tuple(column.name for column in index.columns)
        for index in table.indexes
    }
    assert indexes["ix_cl_decision_event_strategy_run_observed"] == (
        "strategy_run_id",
        "strategy_run_epoch",
        "strategy_run_fingerprint",
        "observed_at",
    )


def test_legal_lifecycle_transitions_are_append_only(
    event_store,
    make_decision_event,
) -> None:
    event = make_decision_event()
    event_store.append_event(event)
    states = (
        EventState.DETECTED,
        EventState.RISK_CHECKED,
        EventState.REVIEW_PENDING,
        EventState.CONFIRMED,
        EventState.ACTED,
    )
    for index, (from_state, to_state) in enumerate(zip(states, states[1:])):
        _append_transition(
            event_store,
            event.event_id,
            from_state,
            to_state,
            event.observed_at + timedelta(seconds=index),
        )

    transitions = event_store.list_transitions(event.event_id)
    assert tuple(item.to_state for item in transitions) == states[1:]
    assert event_store.current_state(event.event_id) is EventState.ACTED
    assert event_store.count_transitions(event.event_id) == 4


def test_confirmed_cannot_return_to_review_pending(
    event_store,
    make_decision_event,
) -> None:
    event = make_decision_event()
    event_store.append_event(event)
    _append_transition(
        event_store,
        event.event_id,
        EventState.DETECTED,
        EventState.RISK_CHECKED,
        event.observed_at,
    )
    _append_transition(
        event_store,
        event.event_id,
        EventState.RISK_CHECKED,
        EventState.REVIEW_PENDING,
        event.observed_at + timedelta(seconds=1),
    )
    _append_transition(
        event_store,
        event.event_id,
        EventState.REVIEW_PENDING,
        EventState.CONFIRMED,
        event.observed_at + timedelta(seconds=2),
    )

    with pytest.raises(InvalidEventTransition, match="illegal transition"):
        _append_transition(
            event_store,
            event.event_id,
            EventState.CONFIRMED,
            EventState.REVIEW_PENDING,
            event.observed_at + timedelta(seconds=3),
        )


def test_acted_is_terminal(event_store, make_decision_event) -> None:
    event = make_decision_event()
    event_store.append_event(event)
    path = (
        (EventState.DETECTED, EventState.RISK_CHECKED),
        (EventState.RISK_CHECKED, EventState.REVIEW_PENDING),
        (EventState.REVIEW_PENDING, EventState.CONFIRMED),
        (EventState.CONFIRMED, EventState.ACTED),
    )
    for index, (from_state, to_state) in enumerate(path):
        _append_transition(
            event_store,
            event.event_id,
            from_state,
            to_state,
            event.observed_at + timedelta(seconds=index),
        )

    with pytest.raises(InvalidEventTransition, match="illegal transition"):
        _append_transition(
            event_store,
            event.event_id,
            EventState.ACTED,
            EventState.INVALIDATED,
            event.observed_at + timedelta(seconds=4),
        )


def test_identical_transition_append_is_idempotent(
    event_store,
    make_decision_event,
) -> None:
    event = make_decision_event()
    event_store.append_event(event)
    occurred_at = event.observed_at

    first = _append_transition(
        event_store,
        event.event_id,
        EventState.DETECTED,
        EventState.RISK_CHECKED,
        occurred_at,
    )
    second = _append_transition(
        event_store,
        event.event_id,
        EventState.DETECTED,
        EventState.RISK_CHECKED,
        occurred_at + timedelta(seconds=1),
    )

    assert second == first
    assert event_store.count_transitions(event.event_id) == 1


def test_stale_from_state_is_rejected_by_compare_and_set(
    event_store,
    make_decision_event,
) -> None:
    event = make_decision_event()
    event_store.append_event(event)
    _append_transition(
        event_store,
        event.event_id,
        EventState.DETECTED,
        EventState.RISK_CHECKED,
        event.observed_at,
    )

    with pytest.raises(InvalidEventTransition, match="current state"):
        event_store.append_transition(
            event.event_id,
            EventState.DETECTED,
            EventState.INVALIDATED,
            occurred_at=event.observed_at + timedelta(seconds=1),
            reason="stale writer",
            actor="pytest",
        )


def test_stale_from_state_uses_dedicated_state_conflict(
    event_store,
    make_decision_event,
) -> None:
    event = make_decision_event()
    event_store.append_event(event)
    _append_transition(
        event_store,
        event.event_id,
        EventState.DETECTED,
        EventState.RISK_CHECKED,
        event.observed_at,
    )
    conflict_type = getattr(
        event_store_module,
        "EventStateConflictError",
        None,
    )

    assert conflict_type is not None
    assert issubclass(conflict_type, InvalidEventTransition)
    with pytest.raises(conflict_type, match="current state"):
        event_store.append_transition(
            event.event_id,
            EventState.DETECTED,
            EventState.INVALIDATED,
            occurred_at=event.observed_at + timedelta(seconds=1),
            reason="stale writer",
            actor="pytest",
        )


def test_transition_chain_rolls_back_all_rows_on_flush_failure(
    event_store,
    make_decision_event,
) -> None:
    event = make_decision_event()
    event_store.append_event(event)
    chain = (
        TransitionSpec(
            EventState.DETECTED,
            EventState.RISK_CHECKED,
            event.observed_at,
            "risk_allowed",
            "risk_engine",
        ),
        TransitionSpec(
            EventState.RISK_CHECKED,
            EventState.REVIEW_PENDING,
            event.observed_at,
            "risk_allowed",
            "event_service",
        ),
    )
    session_class = event_store._session_factory.class_

    def fail_chain_flush(session, flush_context, instances):
        transition_rows = [
            row
            for row in session.new
            if isinstance(row, TableByDecisionTransition)
        ]
        if len(transition_rows) == 2:
            raise RuntimeError("injected chain failure")

    sqlalchemy_event.listen(session_class, "before_flush", fail_chain_flush)
    try:
        with pytest.raises(RuntimeError, match="injected chain failure"):
            event_store.append_transition_chain(event.event_id, chain)
    finally:
        sqlalchemy_event.remove(session_class, "before_flush", fail_chain_flush)

    assert event_store.current_state(event.event_id) is EventState.DETECTED
    assert event_store.count_transitions(event.event_id) == 0

    transitions = event_store.append_transition_chain(event.event_id, chain)

    assert tuple(item.to_state for item in transitions) == (
        EventState.RISK_CHECKED,
        EventState.REVIEW_PENDING,
    )


def test_event_snapshot_returns_state_and_transitions_from_one_read(
    event_store,
    make_decision_event,
) -> None:
    event = make_decision_event()
    event_store.append_event(event)
    _append_transition(
        event_store,
        event.event_id,
        EventState.DETECTED,
        EventState.RISK_CHECKED,
        event.observed_at,
    )

    snapshot = event_store.get_snapshot(event.event_id)

    assert snapshot.event == event
    assert snapshot.state is EventState.RISK_CHECKED
    assert snapshot.transitions[-1].to_state is snapshot.state


def test_mysql_transition_timestamp_uses_microsecond_precision() -> None:
    ddl = str(
        CreateTable(TableByDecisionTransition.__table__).compile(
            dialect=mysql.dialect()
        )
    )

    assert "occurred_at DATETIME(6) NOT NULL" in ddl
