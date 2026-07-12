from __future__ import annotations

import sqlite3

import pytest
from sqlalchemy import text

from chanlun.db_models.decision_support import (
    TableByDecisionEvent,
    TableByLLMReview,
    TableByPaperAdmissionAuthorization,
)
from chanlun.persistence import db as db_module
from chanlun.persistence.db import DB


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


def _create_legacy_table(
    database_path,
    table,
    omitted_column: str | None = None,
) -> None:
    retained_columns = [
        column.name for column in table.columns if column.name != omitted_column
    ]
    definitions = [
        f'"{column}" INTEGER PRIMARY KEY'
        if column == "id"
        else f'"{column}" TEXT'
        for column in retained_columns
    ]
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            f'CREATE TABLE "{table.name}" ({", ".join(definitions)})'
        )


def test_sqlite_engine_enforces_integrity_pragmas_on_every_connection(
    monkeypatch,
    tmp_path,
):
    _configure_sqlite_database(monkeypatch, tmp_path, "pragma_integrity")
    database = DB.__wrapped__()
    try:
        with database.engine.connect() as first, database.engine.connect() as second:
            observed = [
                (
                    connection.execute(text("PRAGMA foreign_keys")).scalar_one(),
                    connection.execute(text("PRAGMA busy_timeout")).scalar_one(),
                )
                for connection in (first, second)
            ]
    finally:
        database.engine.dispose()

    assert observed == [(1, 5_000), (1, 5_000)]


def test_sqlite_startup_rejects_legacy_llm_review_without_risk_snapshot_id(
    monkeypatch,
    tmp_path,
):
    database_path = _configure_sqlite_database(
        monkeypatch,
        tmp_path,
        "legacy_review",
    )
    _create_legacy_table(
        database_path,
        TableByLLMReview.__table__,
        "risk_snapshot_id",
    )

    with pytest.raises(
        RuntimeError,
        match=(
            "SQLite decision-support schema column is missing: "
            "cl_decision_llm_review risk_snapshot_id"
        ),
    ):
        DB.__wrapped__()


@pytest.mark.parametrize(
    "omitted_column",
    tuple(TableByPaperAdmissionAuthorization.__table__.columns.keys()),
)
def test_sqlite_startup_requires_every_paper_authorization_column(
    monkeypatch,
    tmp_path,
    omitted_column,
):
    database_path = _configure_sqlite_database(
        monkeypatch,
        tmp_path,
        "legacy_paper_authorization",
    )
    _create_legacy_table(
        database_path,
        TableByPaperAdmissionAuthorization.__table__,
        omitted_column,
    )

    with pytest.raises(
        RuntimeError,
        match=(
            "SQLite decision-support schema column is missing: "
            "cl_decision_paper_admission_authorization "
            + omitted_column
        ),
    ):
        DB.__wrapped__()


@pytest.mark.parametrize(
    "omitted_column",
    (
        "strategy_run_id",
        "strategy_run_epoch",
        "strategy_run_fingerprint",
    ),
)
def test_sqlite_startup_requires_strategy_run_columns(
    monkeypatch,
    tmp_path,
    omitted_column,
):
    database_path = _configure_sqlite_database(
        monkeypatch,
        tmp_path,
        "legacy_decision_event_columns",
    )
    _create_legacy_table(
        database_path,
        TableByDecisionEvent.__table__,
        omitted_column,
    )

    with pytest.raises(
        RuntimeError,
        match=(
            "SQLite decision-support schema column is missing: "
            "cl_decision_event "
            + omitted_column
        ),
    ):
        DB.__wrapped__()


def test_sqlite_startup_requires_strategy_run_observed_index(
    monkeypatch,
    tmp_path,
):
    database_path = _configure_sqlite_database(
        monkeypatch,
        tmp_path,
        "legacy_decision_event_index",
    )
    _create_legacy_table(database_path, TableByDecisionEvent.__table__)

    with pytest.raises(
        RuntimeError,
        match="SQLite decision-support schema index is missing: cl_decision_event",
    ):
        DB.__wrapped__()


def test_sqlite_schema_validation_is_a_mysql_noop(monkeypatch):
    monkeypatch.setattr(db_module.config, "DB_TYPE", "mysql")
    database = object.__new__(DB.__wrapped__)
    database.engine = object()

    database._validate_sqlite_decision_support_schema()
