from __future__ import annotations

import pytest
from sqlalchemy import create_engine as sqlalchemy_create_engine
from sqlalchemy.dialects.mysql import INTEGER as MySQLInteger
from sqlalchemy.dialects.mysql import VARCHAR as MySQLVarchar

from chanlun.persistence import db as db_module
from chanlun.persistence.db import DB


_TABLE = "cl_decision_event"
_INDEX_COLUMNS = (
    "strategy_run_id",
    "strategy_run_epoch",
    "strategy_run_fingerprint",
    "observed_at",
)
_LOCK_NAME = "cl_decision_event_strategy_run_schema_v1"


def _complete_columns():
    return [
        {
            "name": "strategy_run_id",
            "type": MySQLVarchar(80, collation="utf8mb4_bin"),
            "nullable": True,
        },
        {
            "name": "strategy_run_epoch",
            "type": MySQLInteger(),
            "nullable": True,
        },
        {
            "name": "strategy_run_fingerprint",
            "type": MySQLVarchar(71, collation="utf8mb4_bin"),
            "nullable": True,
        },
    ]


class _Inspector:
    def __init__(self, *, columns=None, indexes=None, tables=None):
        self.columns = _complete_columns() if columns is None else columns
        self.indexes = (
            [
                {
                    "name": "ix_cl_decision_event_strategy_run_observed",
                    "column_names": list(_INDEX_COLUMNS),
                    "unique": False,
                }
            ]
            if indexes is None
            else indexes
        )
        self.tables = {_TABLE} if tables is None else tables

    def get_table_names(self):
        return list(self.tables)

    def get_columns(self, table_name):
        assert table_name == _TABLE
        return [dict(column) for column in self.columns]

    def get_indexes(self, table_name):
        assert table_name == _TABLE
        return [dict(index) for index in self.indexes]


class _ScalarResult:
    def __init__(self, value):
        self._value = value

    def scalar(self):
        return self._value


class _MigrationConnection:
    def __init__(self, inspector):
        self.inspector = inspector
        self.calls = []
        self.closed = False

    def execute(self, statement, parameters=None):
        sql = " ".join(str(statement).split())
        self.calls.append((sql, parameters))
        if sql.startswith("SELECT GET_LOCK"):
            return _ScalarResult(1)
        if sql.startswith("SELECT RELEASE_LOCK"):
            return _ScalarResult(1)
        if sql.startswith("ALTER TABLE"):
            for expected in _complete_columns():
                if (
                    f"`{expected['name']}`" in sql
                    and all(
                        column["name"] != expected["name"]
                        for column in self.inspector.columns
                    )
                ):
                    self.inspector.columns.append(expected)
            return _ScalarResult(1)
        if sql.startswith("CREATE INDEX"):
            self.inspector.indexes.append(
                {
                    "name": "ix_cl_decision_event_strategy_run_observed",
                    "column_names": list(_INDEX_COLUMNS),
                    "unique": False,
                }
            )
            return _ScalarResult(1)
        raise AssertionError(f"unexpected SQL: {sql}")

    def close(self):
        self.closed = True


class _MigrationEngine:
    def __init__(self, connection):
        self.connection = connection
        self.connect_count = 0

    def connect(self):
        self.connect_count += 1
        return self.connection


def _database(engine=None):
    database = object.__new__(DB.__wrapped__)
    database.engine = engine or object()
    return database


def _install_mysql_inspector(monkeypatch, inspector):
    monkeypatch.setattr(db_module.config, "DB_TYPE", "mysql")
    monkeypatch.setattr(db_module, "inspect", lambda engine: inspector)


def test_mysql_strategy_run_schema_migration_adds_missing_shape(monkeypatch):
    inspector = _Inspector(columns=[], indexes=[])
    connection = _MigrationConnection(inspector)
    engine = _MigrationEngine(connection)
    _install_mysql_inspector(monkeypatch, inspector)
    database = _database(engine)

    database._migrate_decision_event_strategy_run_schema()
    database._validate_decision_event_strategy_run_schema()

    ddl = [
        sql
        for sql, _parameters in connection.calls
        if sql.startswith(("ALTER TABLE", "CREATE INDEX"))
    ]
    assert ddl == [
        "ALTER TABLE `cl_decision_event` ADD COLUMN `strategy_run_id` "
        "VARCHAR(80) CHARACTER SET utf8mb4 COLLATE utf8mb4_bin NULL",
        "ALTER TABLE `cl_decision_event` ADD COLUMN `strategy_run_epoch` INT NULL",
        "ALTER TABLE `cl_decision_event` ADD COLUMN `strategy_run_fingerprint` "
        "VARCHAR(71) CHARACTER SET utf8mb4 COLLATE utf8mb4_bin NULL",
        "CREATE INDEX `ix_cl_decision_event_strategy_run_observed` "
        "ON `cl_decision_event` (`strategy_run_id`, `strategy_run_epoch`, "
        "`strategy_run_fingerprint`, `observed_at`)",
    ]
    assert connection.calls[0] == (
        "SELECT GET_LOCK(:name, :timeout)",
        {
            "name": _LOCK_NAME,
            "timeout": DB.__wrapped__.MYSQL_SCHEMA_LOCK_TIMEOUT,
        },
    )
    assert connection.calls[-1] == (
        "SELECT RELEASE_LOCK(:name)",
        {"name": _LOCK_NAME},
    )
    assert connection.closed is True


def test_mysql_strategy_run_schema_migration_is_idempotent_without_lock(
    monkeypatch,
):
    inspector = _Inspector()
    connection = _MigrationConnection(inspector)
    engine = _MigrationEngine(connection)
    _install_mysql_inspector(monkeypatch, inspector)

    _database(engine)._migrate_decision_event_strategy_run_schema()

    assert engine.connect_count == 0
    assert connection.calls == []


def test_mysql_startup_migrates_strategy_run_schema_before_validation(
    monkeypatch,
):
    engine = sqlalchemy_create_engine("sqlite:///:memory:")
    calls = []
    monkeypatch.setattr(db_module.config, "DB_TYPE", "mysql")
    monkeypatch.delenv("CHANLUN_TESTING", raising=False)
    monkeypatch.setattr(db_module, "create_engine", lambda *args, **kwargs: engine)
    monkeypatch.setattr(db_module.Base.metadata, "create_all", lambda _engine: None)
    monkeypatch.setattr(
        DB.__wrapped__,
        "_migrate_decision_event_strategy_run_schema",
        lambda self: calls.append("migrate"),
        raising=False,
    )
    monkeypatch.setattr(
        DB.__wrapped__,
        "_validate_decision_event_strategy_run_schema",
        lambda self: calls.append("validate"),
    )
    monkeypatch.setattr(
        DB.__wrapped__,
        "_migrate_decision_support_datetime_precision",
        lambda self: None,
    )
    monkeypatch.setattr(
        DB.__wrapped__,
        "_migrate_llm_review_risk_snapshot_schema",
        lambda self: None,
    )
    monkeypatch.setattr(
        DB.__wrapped__,
        "_validate_llm_review_constraints",
        lambda self: None,
    )
    monkeypatch.setattr(
        DB.__wrapped__,
        "_migrate_alert_task_unique_constraint",
        lambda self: None,
    )

    try:
        DB.__wrapped__()
    finally:
        engine.dispose()

    assert calls == ["migrate", "validate"]


def test_mysql_strategy_run_schema_accepts_complete_shape(monkeypatch):
    _install_mysql_inspector(monkeypatch, _Inspector())

    _database()._validate_decision_event_strategy_run_schema()


@pytest.mark.parametrize(
    "missing_column",
    (
        "strategy_run_id",
        "strategy_run_epoch",
        "strategy_run_fingerprint",
    ),
)
def test_mysql_strategy_run_schema_rejects_missing_column(
    monkeypatch,
    missing_column,
):
    columns = [
        column
        for column in _complete_columns()
        if column["name"] != missing_column
    ]
    _install_mysql_inspector(monkeypatch, _Inspector(columns=columns))

    with pytest.raises(RuntimeError, match="column is missing"):
        _database()._validate_decision_event_strategy_run_schema()


@pytest.mark.parametrize(
    ("column_name", "bad_type"),
    (
        (
            "strategy_run_id",
            MySQLVarchar(79, collation="utf8mb4_bin"),
        ),
        (
            "strategy_run_id",
            MySQLVarchar(80, collation="utf8mb4_general_ci"),
        ),
        ("strategy_run_epoch", MySQLVarchar(11)),
        (
            "strategy_run_fingerprint",
            MySQLVarchar(70, collation="utf8mb4_bin"),
        ),
        (
            "strategy_run_fingerprint",
            MySQLVarchar(71, collation="utf8mb4_general_ci"),
        ),
    ),
)
def test_mysql_strategy_run_schema_rejects_type_length_or_collation(
    monkeypatch,
    column_name,
    bad_type,
):
    columns = _complete_columns()
    next(column for column in columns if column["name"] == column_name)[
        "type"
    ] = bad_type
    _install_mysql_inspector(monkeypatch, _Inspector(columns=columns))

    with pytest.raises(RuntimeError, match="column schema mismatch"):
        _database()._validate_decision_event_strategy_run_schema()


@pytest.mark.parametrize(
    "column_name",
    (
        "strategy_run_id",
        "strategy_run_epoch",
        "strategy_run_fingerprint",
    ),
)
def test_mysql_strategy_run_schema_requires_nullable_columns(
    monkeypatch,
    column_name,
):
    columns = _complete_columns()
    next(column for column in columns if column["name"] == column_name)[
        "nullable"
    ] = False
    _install_mysql_inspector(monkeypatch, _Inspector(columns=columns))

    with pytest.raises(RuntimeError, match="nullability mismatch"):
        _database()._validate_decision_event_strategy_run_schema()


def test_mysql_strategy_run_schema_requires_observed_index(monkeypatch):
    _install_mysql_inspector(monkeypatch, _Inspector(indexes=[]))

    with pytest.raises(RuntimeError, match="index is missing"):
        _database()._validate_decision_event_strategy_run_schema()
