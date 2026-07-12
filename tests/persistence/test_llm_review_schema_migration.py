from __future__ import annotations

import pytest
from sqlalchemy import create_engine as sqlalchemy_create_engine
from sqlalchemy.dialects.mysql import VARCHAR as MySQLVarchar

from chanlun.persistence import db as db_module
from chanlun.persistence.db import DB


_TABLE = "cl_decision_llm_review"
_LOCK_NAME = "cl_decision_llm_review_risk_snapshot_schema_v1"
_FOREIGN_KEY = {
    "constrained_columns": ["risk_snapshot_id"],
    "referred_table": "cl_decision_risk_snapshot",
    "referred_columns": ["snapshot_id"],
    "options": {"ondelete": "RESTRICT"},
}


class _Inspector:
    def __init__(self, *, has_column: bool, has_foreign_key: bool):
        self.columns = []
        if has_column:
            self.add_column()
        self.foreign_keys = []
        if has_foreign_key:
            self.add_foreign_key()

    def add_column(self):
        self.columns.append(
            {
                "name": "risk_snapshot_id",
                "type": MySQLVarchar(255, collation="utf8mb4_bin"),
                "nullable": False,
            }
        )

    def add_foreign_key(self):
        self.foreign_keys.append(dict(_FOREIGN_KEY))

    def get_table_names(self):
        return [_TABLE, "cl_decision_risk_snapshot"]

    def get_columns(self, table_name):
        assert table_name == _TABLE
        return [dict(column) for column in self.columns]

    def get_foreign_keys(self, table_name):
        assert table_name == _TABLE
        return [dict(foreign_key) for foreign_key in self.foreign_keys]


class _ScalarResult:
    def __init__(self, value):
        self._value = value

    def scalar(self):
        return self._value


class _Connection:
    def __init__(self, inspector, *, row_count=0):
        self.inspector = inspector
        self.row_count = row_count
        self.calls = []
        self.closed = False

    def execute(self, statement, parameters=None):
        sql = " ".join(str(statement).split())
        self.calls.append((sql, parameters))
        if sql.startswith("SELECT GET_LOCK"):
            return _ScalarResult(1)
        if sql.startswith("LOCK TABLES") or sql == "UNLOCK TABLES":
            return _ScalarResult(1)
        if sql.startswith("SELECT COUNT(*)"):
            return _ScalarResult(self.row_count)
        if sql.startswith("ALTER TABLE"):
            if "ADD COLUMN" in sql:
                self.inspector.add_column()
            if "ADD CONSTRAINT" in sql:
                self.inspector.add_foreign_key()
            return _ScalarResult(1)
        if sql.startswith("SELECT RELEASE_LOCK"):
            return _ScalarResult(1)
        raise AssertionError(f"unexpected SQL: {sql}")

    def close(self):
        self.closed = True


class _Engine:
    def __init__(self, connection):
        self.connection = connection
        self.connect_count = 0

    def connect(self):
        self.connect_count += 1
        return self.connection


def _database(engine):
    database = object.__new__(DB.__wrapped__)
    database.engine = engine
    return database


def _install_mysql_inspector(monkeypatch, inspector):
    monkeypatch.setattr(db_module.config, "DB_TYPE", "mysql")
    monkeypatch.setattr(db_module, "inspect", lambda engine: inspector)


def test_mysql_startup_migrates_llm_review_schema_before_validation(
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
        lambda self: None,
    )
    monkeypatch.setattr(
        DB.__wrapped__,
        "_validate_decision_event_strategy_run_schema",
        lambda self: None,
    )
    monkeypatch.setattr(
        DB.__wrapped__,
        "_migrate_decision_support_datetime_precision",
        lambda self: None,
    )
    monkeypatch.setattr(
        DB.__wrapped__,
        "_migrate_llm_review_risk_snapshot_schema",
        lambda self: calls.append("migrate"),
        raising=False,
    )
    monkeypatch.setattr(
        DB.__wrapped__,
        "_validate_llm_review_constraints",
        lambda self: calls.append("validate"),
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


def test_mysql_llm_review_schema_migration_adds_risk_snapshot_binding(
    monkeypatch,
):
    inspector = _Inspector(has_column=False, has_foreign_key=False)
    connection = _Connection(inspector)
    engine = _Engine(connection)
    _install_mysql_inspector(monkeypatch, inspector)

    _database(engine)._migrate_llm_review_risk_snapshot_schema()

    ddl = [
        sql
        for sql, _parameters in connection.calls
        if sql.startswith("ALTER TABLE")
    ]
    assert ddl == [
        "ALTER TABLE `cl_decision_llm_review` ADD COLUMN `risk_snapshot_id` "
        "VARCHAR(255) CHARACTER SET utf8mb4 COLLATE utf8mb4_bin NOT NULL, "
        "ADD CONSTRAINT "
        "`fk_cl_decision_llm_review_risk_snapshot_id` FOREIGN KEY "
        "(`risk_snapshot_id`) REFERENCES "
        "`cl_decision_risk_snapshot` (`snapshot_id`) ON DELETE RESTRICT",
    ]
    assert connection.calls[0] == (
        "SELECT GET_LOCK(:name, :timeout)",
        {
            "name": _LOCK_NAME,
            "timeout": DB.__wrapped__.MYSQL_SCHEMA_LOCK_TIMEOUT,
        },
    )
    assert connection.calls[1][0] == (
        "LOCK TABLES `cl_decision_llm_review` WRITE, "
        "`cl_decision_risk_snapshot` READ"
    )
    assert connection.calls[-2][0] == "UNLOCK TABLES"
    assert connection.calls[-1] == (
        "SELECT RELEASE_LOCK(:name)",
        {"name": _LOCK_NAME},
    )
    assert connection.closed is True


def test_mysql_llm_review_schema_migration_rejects_nonempty_legacy_table(
    monkeypatch,
):
    inspector = _Inspector(has_column=False, has_foreign_key=False)
    connection = _Connection(inspector, row_count=1)
    engine = _Engine(connection)
    _install_mysql_inspector(monkeypatch, inspector)

    with pytest.raises(RuntimeError, match="manual migration required"):
        _database(engine)._migrate_llm_review_risk_snapshot_schema()

    assert not any(
        sql.startswith("ALTER TABLE") for sql, _parameters in connection.calls
    )
    assert connection.calls[-2][0] == "UNLOCK TABLES"
    assert connection.calls[-1][0] == "SELECT RELEASE_LOCK(:name)"
    assert connection.closed is True


def test_mysql_llm_review_schema_migration_completes_safe_partial_column(
    monkeypatch,
):
    inspector = _Inspector(has_column=True, has_foreign_key=False)
    connection = _Connection(inspector)
    engine = _Engine(connection)
    _install_mysql_inspector(monkeypatch, inspector)

    _database(engine)._migrate_llm_review_risk_snapshot_schema()

    orphan_queries = [
        sql
        for sql, _parameters in connection.calls
        if sql.startswith("SELECT COUNT(*)")
    ]
    assert orphan_queries == [
        "SELECT COUNT(*) FROM `cl_decision_llm_review` "
        "LEFT JOIN `cl_decision_risk_snapshot` "
        "ON `cl_decision_llm_review`.`risk_snapshot_id` = "
        "`cl_decision_risk_snapshot`.`snapshot_id` "
        "WHERE `cl_decision_llm_review`.`risk_snapshot_id` IS NULL "
        "OR `cl_decision_risk_snapshot`.`snapshot_id` IS NULL"
    ]
    assert inspector.foreign_keys == [_FOREIGN_KEY]
    assert connection.calls[-2][0] == "UNLOCK TABLES"
    assert connection.calls[-1][0] == "SELECT RELEASE_LOCK(:name)"
    assert connection.closed is True


def test_mysql_llm_review_schema_migration_rejects_unsafe_partial_column(
    monkeypatch,
):
    inspector = _Inspector(has_column=True, has_foreign_key=False)
    inspector.columns[0]["default"] = "legacy-snapshot"
    connection = _Connection(inspector)
    engine = _Engine(connection)
    _install_mysql_inspector(monkeypatch, inspector)

    with pytest.raises(RuntimeError, match="unsafe shape"):
        _database(engine)._migrate_llm_review_risk_snapshot_schema()

    assert engine.connect_count == 0
    assert connection.calls == []
    assert connection.closed is False


def test_mysql_llm_review_schema_migration_rejects_unsafe_complete_column(
    monkeypatch,
):
    inspector = _Inspector(has_column=True, has_foreign_key=True)
    inspector.columns[0]["default"] = "legacy-snapshot"
    connection = _Connection(inspector)
    engine = _Engine(connection)
    _install_mysql_inspector(monkeypatch, inspector)

    with pytest.raises(RuntimeError, match="unsafe shape"):
        _database(engine)._migrate_llm_review_risk_snapshot_schema()

    assert engine.connect_count == 0
    assert connection.calls == []


def test_mysql_llm_review_schema_migration_is_idempotent_without_lock(
    monkeypatch,
):
    inspector = _Inspector(has_column=True, has_foreign_key=True)
    connection = _Connection(inspector)
    engine = _Engine(connection)
    _install_mysql_inspector(monkeypatch, inspector)

    _database(engine)._migrate_llm_review_risk_snapshot_schema()

    assert engine.connect_count == 0
    assert connection.calls == []
