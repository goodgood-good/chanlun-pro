from __future__ import annotations

from datetime import datetime, timezone
import sqlite3
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import create_engine, inspect, select
from sqlalchemy.dialects import mysql

from chanlun.db_models.decision_support import (
    TableByDecisionEvent,
    TableByExitAttentionObservation,
    TableByPhysicalOneMinuteCheckpoint,
    TableBySectorPreferenceRevision,
    TableBySectorSelection,
    TableByTriggerEventLink,
    TableByTriggerObservation,
)
from chanlun.decision_support.fingerprints import sha256_json
from chanlun.persistence.db import DB
from chanlun.persistence import db as db_module


CN = ZoneInfo("Asia/Shanghai")


def _configure_sqlite_database(monkeypatch, tmp_path, database: str):
    monkeypatch.setenv("CHANLUN_TESTING", "1")
    monkeypatch.setenv("CHANLUN_TEST_DATA_PATH", str(tmp_path))
    monkeypatch.setattr(db_module.config, "DATA_PATH", str(tmp_path))
    monkeypatch.setattr(db_module.config, "DB_TYPE", "sqlite")
    monkeypatch.setattr(db_module.config, "DB_DATABASE", database)
    monkeypatch.setattr(db_module, "get_data_path", lambda: tmp_path)
    db_dir = tmp_path / "db"
    db_dir.mkdir(parents=True, exist_ok=True)
    return db_dir / f"{database}.sqlite"


def _create_legacy_preference_table(database_path, *, with_row: bool) -> None:
    with sqlite3.connect(database_path) as connection:
        connection.executescript(
            """
            CREATE TABLE cl_decision_sector_preference_revision (
                id INTEGER NOT NULL PRIMARY KEY AUTOINCREMENT,
                sector_id VARCHAR(255) NOT NULL,
                action VARCHAR(20) NOT NULL,
                revision INTEGER NOT NULL,
                expected_revision INTEGER NOT NULL,
                idempotency_key VARCHAR(128) NOT NULL,
                operator_id VARCHAR(191) NOT NULL,
                reason TEXT NOT NULL,
                changed_at DATETIME NOT NULL,
                pinned_at DATETIME,
                CONSTRAINT uq_cl_decision_sector_preference_revision
                    UNIQUE (sector_id, revision),
                CONSTRAINT uq_cl_decision_sector_preference_idempotency
                    UNIQUE (sector_id, idempotency_key)
            );
            CREATE INDEX ix_cl_decision_sector_preference_latest
                ON cl_decision_sector_preference_revision
                (sector_id, revision, id);
            """
        )
        if with_row:
            connection.execute(
                """
                INSERT INTO cl_decision_sector_preference_revision (
                    sector_id, action, revision, expected_revision,
                    idempotency_key, operator_id, reason, changed_at, pinned_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "sector:" + "a" * 64,
                    "exclude",
                    1,
                    0,
                    "legacy-1",
                    "user-1",
                    "legacy",
                    "2026-07-16 10:00:00.123456",
                    None,
                ),
            )


def _unique_columns(inspector, table_name: str) -> set[tuple[str, ...]]:
    return {
        tuple(item.get("column_names") or ())
        for item in inspector.get_unique_constraints(table_name)
    }


def _foreign_keys(inspector, table_name: str) -> set[tuple[str, str, str]]:
    result: set[tuple[str, str, str]] = set()
    for item in inspector.get_foreign_keys(table_name):
        constrained = tuple(item.get("constrained_columns") or ())
        referred = tuple(item.get("referred_columns") or ())
        if len(constrained) == 1 and len(referred) == 1:
            result.add(
                (
                    constrained[0],
                    str(item.get("referred_table")),
                    referred[0],
                )
            )
    return result


def test_opportunity_schema_creates_required_tables_constraints_and_foreign_keys(
    tmp_path,
):
    engine = create_engine(f"sqlite:///{tmp_path / 'opportunity.sqlite3'}")
    tables = (
        TableByDecisionEvent.__table__,
        TableBySectorSelection.__table__,
        TableByTriggerObservation.__table__,
        TableByExitAttentionObservation.__table__,
        TableByPhysicalOneMinuteCheckpoint.__table__,
        TableByTriggerEventLink.__table__,
        TableBySectorPreferenceRevision.__table__,
    )
    try:
        for table in tables:
            table.create(engine, checkfirst=True)

        inspector = inspect(engine)
        expected_tables = {
            "cl_decision_sector_selection",
            "cl_decision_trigger_observation",
            "cl_decision_exit_attention_observation",
            "cl_decision_physical_one_minute_checkpoint",
            "cl_decision_trigger_event_link",
            "cl_decision_sector_preference_revision",
        }
        assert expected_tables <= set(inspector.get_table_names())

        assert ("selection_id",) in _unique_columns(
            inspector,
            "cl_decision_sector_selection",
        )
        assert ("trigger_id",) in _unique_columns(
            inspector,
            "cl_decision_trigger_observation",
        )
        assert ("exit_attention_id",) in _unique_columns(
            inspector,
            "cl_decision_exit_attention_observation",
        )
        checkpoint_uniques = _unique_columns(
            inspector,
            "cl_decision_physical_one_minute_checkpoint",
        )
        assert ("checkpoint_id",) in checkpoint_uniques
        assert (
            "market",
            "code",
            "engine_policy_fingerprint",
            "strategy_run_id",
            "strategy_run_epoch",
            "strategy_run_fingerprint",
        ) in checkpoint_uniques

        link_uniques = _unique_columns(
            inspector,
            "cl_decision_trigger_event_link",
        )
        assert ("trigger_id",) in link_uniques
        assert ("event_id",) in link_uniques

        preference_uniques = _unique_columns(
            inspector,
            "cl_decision_sector_preference_revision",
        )
        assert ("sector_id", "revision") in preference_uniques
        assert ("sector_id", "idempotency_key") in preference_uniques

        assert (
            "selection_id",
            "cl_decision_sector_selection",
            "selection_id",
        ) in _foreign_keys(inspector, "cl_decision_trigger_observation")
        link_foreign_keys = _foreign_keys(
            inspector,
            "cl_decision_trigger_event_link",
        )
        assert (
            "trigger_id",
            "cl_decision_trigger_observation",
            "trigger_id",
        ) in link_foreign_keys
        assert (
            "event_id",
            "cl_decision_event",
            "event_id",
        ) in link_foreign_keys
    finally:
        engine.dispose()


def test_db_schema_attestation_registers_all_opportunity_tables_and_times():
    expected_tables = {
        "cl_decision_sector_selection",
        "cl_decision_trigger_observation",
        "cl_decision_exit_attention_observation",
        "cl_decision_physical_one_minute_checkpoint",
        "cl_decision_trigger_event_link",
        "cl_decision_sector_preference_revision",
    }
    assert expected_tables <= set(DB.SQLITE_DECISION_SUPPORT_REQUIRED_COLUMNS)

    expected_datetimes = {
        ("cl_decision_sector_selection", "observed_at"),
        ("cl_decision_sector_selection", "bar_closed_at"),
        ("cl_decision_trigger_observation", "bar_opened_at"),
        ("cl_decision_trigger_observation", "bar_closed_at"),
        ("cl_decision_trigger_observation", "observed_at"),
        ("cl_decision_trigger_observation", "physical_5m_closed_at"),
        ("cl_decision_exit_attention_observation", "bar_opened_at"),
        ("cl_decision_exit_attention_observation", "bar_closed_at"),
        ("cl_decision_exit_attention_observation", "observed_at"),
        (
            "cl_decision_physical_one_minute_checkpoint",
            "analysis_first_bar_closed_at",
        ),
        (
            "cl_decision_physical_one_minute_checkpoint",
            "bootstrap_closed_at",
        ),
        (
            "cl_decision_physical_one_minute_checkpoint",
            "last_bar_closed_at",
        ),
        ("cl_decision_physical_one_minute_checkpoint", "updated_at"),
        ("cl_decision_trigger_event_link", "linked_at"),
        ("cl_decision_sector_preference_revision", "changed_at"),
        ("cl_decision_sector_preference_revision", "pinned_at"),
    }
    assert expected_datetimes <= set(DB.DECISION_SUPPORT_DATETIME_COLUMNS)


def test_opportunity_schema_carries_durable_preference_audit_and_cycle_receipt():
    preference_columns = set(
        TableBySectorPreferenceRevision.__table__.columns.keys()
    )
    assert {
        "request_fingerprint",
        "payload_fingerprint",
        "payload_json",
    } <= preference_columns

    checkpoint_columns = set(
        TableByPhysicalOneMinuteCheckpoint.__table__.columns.keys()
    )
    assert {"cycle_fingerprint", "cycle_json"} <= checkpoint_columns


def test_trigger_event_link_uses_parent_event_id_mysql_type():
    dialect = mysql.dialect()
    parent_type = TableByDecisionEvent.event_id.type.dialect_impl(dialect)
    link_type = TableByTriggerEventLink.event_id.type.dialect_impl(dialect)

    assert type(link_type) is type(parent_type)
    assert link_type.length == parent_type.length
    assert link_type.collation == parent_type.collation


def test_sqlite_known_empty_legacy_opportunity_table_migrates_idempotently(
    monkeypatch,
    tmp_path,
):
    database_path = _configure_sqlite_database(
        monkeypatch,
        tmp_path,
        "legacy_opportunity_empty",
    )
    _create_legacy_preference_table(database_path, with_row=False)

    first = DB.__wrapped__()
    first.engine.dispose()
    second = DB.__wrapped__()
    try:
        columns = {
            str(item["name"])
            for item in inspect(second.engine).get_columns(
                "cl_decision_sector_preference_revision"
            )
        }
    finally:
        second.engine.dispose()

    assert {
        "request_fingerprint",
        "payload_fingerprint",
        "payload_json",
    } <= columns


def test_sqlite_nonempty_legacy_opportunity_table_fails_closed_without_data_loss(
    monkeypatch,
    tmp_path,
):
    database_path = _configure_sqlite_database(
        monkeypatch,
        tmp_path,
        "legacy_opportunity_nonempty",
    )
    _create_legacy_preference_table(database_path, with_row=True)

    with pytest.raises(
        RuntimeError,
        match="opportunity schema migration requires an empty known legacy table",
    ):
        DB.__wrapped__()

    with sqlite3.connect(database_path) as connection:
        count = connection.execute(
            "SELECT COUNT(*) FROM cl_decision_sector_preference_revision"
        ).fetchone()[0]
    assert count == 1


def test_sqlite_legacy_migration_rejects_unknown_trigger_without_mutation(
    monkeypatch,
    tmp_path,
):
    database_path = _configure_sqlite_database(
        monkeypatch,
        tmp_path,
        "legacy_opportunity_unknown_trigger",
    )
    _create_legacy_preference_table(database_path, with_row=False)
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            """
            CREATE TRIGGER unknown_guard
            BEFORE INSERT ON cl_decision_sector_preference_revision
            BEGIN
                SELECT RAISE(ABORT, 'guarded');
            END
            """
        )

    with pytest.raises(
        RuntimeError,
        match="opportunity legacy schema is not allowlisted",
    ):
        DB.__wrapped__()

    with sqlite3.connect(database_path) as connection:
        triggers = connection.execute(
            """
            SELECT name FROM sqlite_master
            WHERE type = 'trigger'
              AND tbl_name = 'cl_decision_sector_preference_revision'
            """
        ).fetchall()
    assert triggers == [("unknown_guard",)]


def test_sqlite_attestation_rejects_opportunity_link_without_unique_and_fk(
    monkeypatch,
    tmp_path,
):
    database_path = _configure_sqlite_database(
        monkeypatch,
        tmp_path,
        "broken_opportunity_link",
    )
    with sqlite3.connect(database_path) as connection:
        connection.executescript(
            """
            CREATE TABLE cl_decision_trigger_event_link (
                id INTEGER NOT NULL PRIMARY KEY AUTOINCREMENT,
                trigger_id VARCHAR(255) NOT NULL,
                event_id VARCHAR(255) NOT NULL,
                linked_at DATETIME NOT NULL
            );
            CREATE INDEX ix_cl_decision_trigger_event_link_linked
                ON cl_decision_trigger_event_link (linked_at, id);
            """
        )

    with pytest.raises(
        RuntimeError,
        match="SQLite opportunity schema unique constraint is missing",
    ):
        DB.__wrapped__()


def test_opportunity_timestamps_round_trip_aware_with_microseconds(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'time.sqlite3'}")
    for table in (
        TableBySectorSelection.__table__,
        TableByTriggerEventLink.__table__,
        TableBySectorPreferenceRevision.__table__,
    ):
        table.create(engine, checkfirst=True)
    observed = datetime(2026, 7, 16, 2, 0, 0, 123456, tzinfo=timezone.utc)
    expected = observed.astimezone(CN)
    selection_id = "selection:" + "1" * 64
    membership_fingerprint = "sha256:" + "2" * 64
    policy_fingerprint = "sha256:" + "3" * 64
    payload_fingerprint = "sha256:" + "4" * 64
    envelope_fingerprint = sha256_json(
        {
            "schema": "sector-selection-envelope/v1",
            "selection_id": selection_id,
            "market": "a",
            "scope": "sector_funnel",
            "bar_closed_at": expected.isoformat(),
            "membership_fingerprint": membership_fingerprint,
            "policy_fingerprint": policy_fingerprint,
            "payload_fingerprint": payload_fingerprint,
            "status": "complete",
            "stale": False,
        }
    )
    try:
        with engine.begin() as connection:
            connection.execute(
                TableBySectorSelection.__table__.insert(),
                {
                    "selection_id": selection_id,
                    "market": "a",
                    "scope": "sector_funnel",
                    "observed_at": observed,
                    "bar_closed_at": observed,
                    "membership_fingerprint": membership_fingerprint,
                    "policy_fingerprint": policy_fingerprint,
                    "payload_fingerprint": payload_fingerprint,
                    "status": "complete",
                    "stale": False,
                    "envelope_fingerprint": envelope_fingerprint,
                    "payload_json": "{}",
                },
            )
            connection.execute(
                TableByTriggerEventLink.__table__.insert(),
                {
                    "trigger_id": "trigger:" + "5" * 64,
                    "event_id": "event-1",
                    "linked_at": observed,
                },
            )
            connection.execute(
                TableBySectorPreferenceRevision.__table__.insert(),
                {
                    "sector_id": "sector:" + "6" * 64,
                    "action": "pin",
                    "revision": 1,
                    "expected_revision": 0,
                    "idempotency_key": "time-1",
                    "operator_id": "user-1",
                    "reason": "time",
                    "changed_at": observed,
                    "pinned_at": observed,
                    "request_fingerprint": "sha256:" + "7" * 64,
                    "payload_fingerprint": "sha256:" + "8" * 64,
                    "payload_json": "{}",
                },
            )
        with engine.connect() as connection:
            selection_times = connection.execute(
                select(
                    TableBySectorSelection.observed_at,
                    TableBySectorSelection.bar_closed_at,
                )
            ).one()
            linked_at = connection.scalar(select(TableByTriggerEventLink.linked_at))
            preference_times = connection.execute(
                select(
                    TableBySectorPreferenceRevision.changed_at,
                    TableBySectorPreferenceRevision.pinned_at,
                )
            ).one()
    finally:
        engine.dispose()

    values = (*selection_times, linked_at, *preference_times)
    assert all(value == expected for value in values)
    assert all(value.tzinfo is not None and value.utcoffset() is not None for value in values)
    assert all(value.microsecond == 123456 for value in values)
