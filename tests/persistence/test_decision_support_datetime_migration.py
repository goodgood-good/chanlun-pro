from __future__ import annotations

from collections import deque

import pytest
from sqlalchemy import Boolean, DateTime, Integer, String
from sqlalchemy import create_engine as sqlalchemy_create_engine
from sqlalchemy.dialects import mysql
from sqlalchemy.dialects.mysql import DATETIME as MySQLDateTime
from sqlalchemy.dialects.mysql import INTEGER as MySQLInteger
from sqlalchemy.dialects.mysql import TIMESTAMP as MySQLTimestamp
from sqlalchemy.dialects.mysql import LONGTEXT as MySQLLongText
from sqlalchemy.dialects.mysql import TINYINT as MySQLTinyInt
from sqlalchemy.dialects.mysql import VARCHAR as MySQLVarchar

from chanlun.db_models.decision_support import (
    TableByLLMReview,
    TableByLLMReviewAttempt,
    TableByLLMReviewClaim,
)
from chanlun.persistence import db as db_module
from chanlun.persistence.db import DB


_TARGETS = (
    ("cl_decision_event", "observed_at"),
    ("cl_decision_transition", "occurred_at"),
    ("cl_decision_review", "reviewed_at"),
    ("cl_decision_user_decision", "decided_at"),
    ("cl_decision_risk_snapshot", "observed_at"),
    ("cl_decision_risk_snapshot", "evaluated_at"),
    ("cl_decision_risk_snapshot", "expires_at"),
    ("cl_decision_paper_admission_authorization", "authorized_at"),
    ("cl_decision_paper_admission_authorization", "risk_expires_at"),
    ("cl_decision_risk_latch_audit", "occurred_at"),
    ("cl_decision_llm_review_claim", "lease_expires_at"),
    ("cl_decision_llm_review_claim", "created_at"),
    ("cl_decision_llm_review_attempt", "started_at"),
    ("cl_decision_llm_review_attempt", "completed_at"),
    ("cl_decision_llm_review", "created_at"),
)


_LLM_REVIEW_COLUMNS = {
    "cl_decision_llm_review_claim": frozenset(
        {
            "id", "review_id", "event_id", "packet_fingerprint", "provider", "model",
            "prompt_version", "owner_token", "fencing_token", "lease_expires_at",
            "finalized", "created_at",
        }
    ),
    "cl_decision_llm_review_attempt": frozenset(
        {
            "id", "attempt_id", "review_id", "event_id", "owner_token", "fencing_token",
            "attempt_number", "provider", "model", "ok", "retryable", "response_content",
            "response_content_bytes", "response_content_sha256", "response_content_truncated",
            "raw_response", "raw_response_bytes", "raw_response_sha256", "raw_response_truncated",
            "error_code", "error_message", "error_message_bytes", "error_message_sha256",
            "error_message_truncated", "latency_ms", "started_at", "completed_at",
        }
    ),
    "cl_decision_llm_review": frozenset(
        {
            "id", "review_id", "event_id", "risk_snapshot_id", "packet_fingerprint", "reviewed_data_fingerprint",
            "provider", "model", "prompt_version", "fencing_token", "status", "provider_ok",
            "verdict", "response_content", "response_content_bytes", "response_content_sha256",
            "response_content_truncated", "raw_response", "raw_response_bytes",
            "raw_response_sha256", "raw_response_truncated", "parsed_response_json",
            "validation_errors_json", "attempt_count", "latency_ms", "error_code", "error_message",
            "error_message_bytes", "error_message_sha256", "error_message_truncated", "created_at",
        }
    ),
}
_IDENTITY_COLUMNS = {
    "cl_decision_llm_review_claim": frozenset({"review_id", "packet_fingerprint", "provider", "model", "prompt_version", "owner_token"}),
    "cl_decision_llm_review_attempt": frozenset({"attempt_id", "review_id", "owner_token", "provider", "model"}),
    "cl_decision_llm_review": frozenset({"review_id", "risk_snapshot_id", "packet_fingerprint", "reviewed_data_fingerprint", "provider", "model", "prompt_version"}),
}
_AUDIT_TEXT_COLUMNS = {
    "cl_decision_llm_review_attempt": frozenset({"response_content", "raw_response", "error_message"}),
    "cl_decision_llm_review": frozenset({"response_content", "raw_response", "parsed_response_json", "validation_errors_json", "error_message"}),
}
_LLM_REVIEW_UNIQUE_CONSTRAINTS = {
    "cl_decision_llm_review_claim": frozenset(
        {
            ("review_id",),
            ("event_id", "packet_fingerprint", "provider", "model", "prompt_version"),
        }
    ),
    "cl_decision_llm_review_attempt": frozenset(
        {
            ("attempt_id",),
            ("review_id", "owner_token", "fencing_token", "attempt_number"),
        }
    ),
    "cl_decision_llm_review": frozenset(
        {
            ("review_id",),
            ("event_id", "packet_fingerprint", "provider", "model", "prompt_version"),
        }
    ),
}
_LLM_REVIEW_FOREIGN_KEYS = {
    "cl_decision_llm_review_claim": (("event_id", "cl_decision_event", "event_id"),),
    "cl_decision_llm_review_attempt": (("review_id", "cl_decision_llm_review_claim", "review_id"), ("event_id", "cl_decision_event", "event_id")),
    "cl_decision_llm_review": (("review_id", "cl_decision_llm_review_claim", "review_id"), ("event_id", "cl_decision_event", "event_id"), ("risk_snapshot_id", "cl_decision_risk_snapshot", "snapshot_id")),
}
_LLM_REVIEW_TABLES = {
    table.__tablename__: table.__table__
    for table in (TableByLLMReviewClaim, TableByLLMReviewAttempt, TableByLLMReview)
}


def _reflected_mysql_type(column):
    expected = column.type.dialect_impl(mysql.dialect())
    affinity = expected._type_affinity
    if isinstance(expected, MySQLLongText):
        return MySQLLongText()
    if affinity is Boolean:
        return MySQLTinyInt(display_width=1)
    if affinity is Integer:
        return MySQLInteger()
    if affinity is DateTime:
        return MySQLDateTime(fsp=getattr(expected, "fsp", None))
    if affinity is String:
        return MySQLVarchar(
            expected.length,
            collation=getattr(expected, "collation", None),
        )
    raise AssertionError(f"unsupported fixture column type: {column}")


def _complete_llm_review_columns():
    return {
        table_name: [
            {
                "name": name,
                "type": _reflected_mysql_type(_LLM_REVIEW_TABLES[table_name].c[name]),
                "nullable": _LLM_REVIEW_TABLES[table_name].c[name].nullable,
            }
            for name in sorted(names)
        ]
        for table_name, names in _LLM_REVIEW_COLUMNS.items()
    }


def _complete_llm_review_foreign_keys():
    return {
        table: [
            {
                "constrained_columns": [column],
                "referred_table": referred_table,
                "referred_columns": [referred_column],
                "options": {"ondelete": "RESTRICT"},
            }
            for column, referred_table, referred_column in foreign_keys
        ]
        for table, foreign_keys in _LLM_REVIEW_FOREIGN_KEYS.items()
    }


def _complete_llm_review_inspector(**overrides):
    required = _LLM_REVIEW_UNIQUE_CONSTRAINTS
    kwargs = {"table_names": set(required), "unique_by_table": {table: set(constraints) for table, constraints in required.items()}, "review_columns_by_table": _complete_llm_review_columns(), "foreign_keys_by_table": _complete_llm_review_foreign_keys(), "primary_keys_by_table": {table: ("id",) for table in required}}
    kwargs.update(overrides)
    return _Inspector({}, **kwargs)


class _ScalarResult:
    def __init__(self, value):
        self._value = value

    def scalar(self):
        return self._value


class _FakeConnection:
    def __init__(
        self,
        *,
        lock_result=1,
        release_result=1,
        fail_alter=False,
    ):
        self.lock_result = lock_result
        self.release_result = release_result
        self.fail_alter = fail_alter
        self.calls = []
        self.closed = False

    def execute(self, statement, parameters=None):
        sql = " ".join(str(statement).split())
        self.calls.append((sql, parameters))
        if sql.startswith("SELECT GET_LOCK"):
            return _ScalarResult(self.lock_result)
        if sql.startswith("SELECT RELEASE_LOCK"):
            return _ScalarResult(self.release_result)
        if sql.startswith("ALTER TABLE") and self.fail_alter:
            raise RuntimeError("injected alter failure")
        return _ScalarResult(1)

    def close(self):
        self.closed = True


class _FakeEngine:
    def __init__(self, connection):
        self.connection = connection
        self.connect_count = 0

    def connect(self):
        self.connect_count += 1
        return self.connection


class _Inspector:
    def __init__(
        self,
        precision_by_target,
        *,
        column_overrides=None,
        table_names=None,
        unique_by_table=None,
        review_columns_by_table=None,
        foreign_keys_by_table=None,
        primary_keys_by_table=None,
    ):
        self.precision_by_target = precision_by_target
        self.column_overrides = column_overrides or {}
        self.table_names = table_names
        self.unique_by_table = unique_by_table or {}
        self.review_columns_by_table = review_columns_by_table
        self.foreign_keys_by_table = foreign_keys_by_table or {}
        self.primary_keys_by_table = primary_keys_by_table or {}

    def get_table_names(self):
        if self.table_names is not None:
            return list(self.table_names)
        return sorted({table for table, _ in self.precision_by_target})

    def get_columns(self, table):
        if self.review_columns_by_table is not None:
            return [dict(column) for column in self.review_columns_by_table.get(table, ())]

        columns = []
        for (target_table, column), fsp in self.precision_by_target.items():
            if target_table != table:
                continue
            override = self.column_overrides.get((target_table, column), {})
            if override.get("omit"):
                continue
            item = {
                "name": column,
                "type": MySQLDateTime(fsp=fsp),
                "nullable": False,
                "default": None,
                "comment": None,
                "autoincrement": False,
            }
            item.update(override)
            columns.append(item)
        return columns

    def get_unique_constraints(self, table):
        return [
            {"column_names": list(columns)}
            for columns in self.unique_by_table.get(table, ())
        ]

    def get_foreign_keys(self, table):
        return [
            dict(foreign_key)
            for foreign_key in self.foreign_keys_by_table.get(table, ())
        ]

    def get_pk_constraint(self, table):
        return {"constrained_columns": list(self.primary_keys_by_table.get(table, ()))}


def _db(engine=None):
    value = object.__new__(DB.__wrapped__)
    value.engine = engine or object()
    return value


def test_datetime_precision_migration_is_sqlite_noop(monkeypatch):
    monkeypatch.setattr(db_module.config, "DB_TYPE", "sqlite")
    value = _db()

    value._migrate_decision_support_datetime_precision()


def test_llm_review_constraint_validation_is_sqlite_noop(monkeypatch):
    monkeypatch.setattr(db_module.config, "DB_TYPE", "sqlite")

    _db()._validate_llm_review_constraints()


def test_llm_review_constraint_validation_accepts_complete_schema(monkeypatch):
    monkeypatch.setattr(db_module.config, "DB_TYPE", "mysql")
    inspector = _complete_llm_review_inspector()
    monkeypatch.setattr(db_module, "inspect", lambda engine: inspector)

    _db()._validate_llm_review_constraints()


def test_llm_review_constraint_validation_rejects_legacy_table_shape(monkeypatch):
    monkeypatch.setattr(db_module.config, "DB_TYPE", "mysql")
    columns = _complete_llm_review_columns()
    columns["cl_decision_llm_review_claim"] = [column for column in columns["cl_decision_llm_review_claim"] if column["name"] != "created_at"]
    inspector = _complete_llm_review_inspector(review_columns_by_table=columns)
    monkeypatch.setattr(db_module, "inspect", lambda engine: inspector)

    with pytest.raises(RuntimeError, match="column is missing"):
        _db()._validate_llm_review_constraints()


def test_llm_review_constraint_validation_rejects_non_binary_identity_column(monkeypatch):
    monkeypatch.setattr(db_module.config, "DB_TYPE", "mysql")
    columns = _complete_llm_review_columns()
    for column in columns["cl_decision_llm_review_claim"]:
        if column["name"] == "review_id":
            column["type"] = MySQLVarchar(255, collation="utf8mb4_general_ci")
    inspector = _complete_llm_review_inspector(review_columns_by_table=columns)
    monkeypatch.setattr(db_module, "inspect", lambda engine: inspector)

    with pytest.raises(RuntimeError, match="identity column must be VARCHAR utf8mb4_bin"):
        _db()._validate_llm_review_constraints()


def test_llm_review_constraint_validation_rejects_non_longtext_audit_column(monkeypatch):
    monkeypatch.setattr(db_module.config, "DB_TYPE", "mysql")
    columns = _complete_llm_review_columns()
    for column in columns["cl_decision_llm_review_attempt"]:
        if column["name"] == "raw_response":
            column["type"] = MySQLVarchar(255)
    inspector = _complete_llm_review_inspector(review_columns_by_table=columns)
    monkeypatch.setattr(db_module, "inspect", lambda engine: inspector)

    with pytest.raises(RuntimeError, match="audit column must be LONGTEXT"):
        _db()._validate_llm_review_constraints()


@pytest.mark.parametrize(
    "table,column,bad_type",
    (
        (
            "cl_decision_llm_review_claim",
            "review_id",
            MySQLVarchar(191, collation="utf8mb4_bin"),
        ),
        ("cl_decision_llm_review_claim", "fencing_token", MySQLVarchar(255)),
        ("cl_decision_llm_review_attempt", "raw_response_bytes", MySQLVarchar(255)),
        ("cl_decision_llm_review", "provider_ok", MySQLVarchar(255)),
        ("cl_decision_llm_review", "status", MySQLVarchar(20)),
    ),
)
def test_llm_review_constraint_validation_rejects_wrong_physical_type(
    monkeypatch,
    table,
    column,
    bad_type,
):
    monkeypatch.setattr(db_module.config, "DB_TYPE", "mysql")
    columns = _complete_llm_review_columns()
    next(item for item in columns[table] if item["name"] == column)["type"] = bad_type
    inspector = _complete_llm_review_inspector(review_columns_by_table=columns)
    monkeypatch.setattr(db_module, "inspect", lambda engine: inspector)

    with pytest.raises(RuntimeError, match="column|identity"):
        _db()._validate_llm_review_constraints()


def test_llm_review_constraint_validation_rejects_wrong_nullability(monkeypatch):
    monkeypatch.setattr(db_module.config, "DB_TYPE", "mysql")
    columns = _complete_llm_review_columns()
    claim_columns = columns["cl_decision_llm_review_claim"]
    next(item for item in claim_columns if item["name"] == "finalized")["nullable"] = True
    inspector = _complete_llm_review_inspector(review_columns_by_table=columns)
    monkeypatch.setattr(db_module, "inspect", lambda engine: inspector)

    with pytest.raises(RuntimeError, match="nullability"):
        _db()._validate_llm_review_constraints()


def test_llm_review_constraint_validation_rejects_cascading_foreign_key(monkeypatch):
    monkeypatch.setattr(db_module.config, "DB_TYPE", "mysql")
    foreign_keys = _complete_llm_review_foreign_keys()
    foreign_keys["cl_decision_llm_review_claim"][0]["options"] = {
        "ondelete": "CASCADE"
    }
    inspector = _complete_llm_review_inspector(foreign_keys_by_table=foreign_keys)
    monkeypatch.setattr(db_module, "inspect", lambda engine: inspector)

    with pytest.raises(RuntimeError, match="RESTRICT"):
        _db()._validate_llm_review_constraints()


def test_llm_review_constraint_validation_rejects_missing_primary_key(monkeypatch):
    monkeypatch.setattr(db_module.config, "DB_TYPE", "mysql")
    primary_keys = {table: ("id",) for table in _LLM_REVIEW_UNIQUE_CONSTRAINTS}
    primary_keys["cl_decision_llm_review"] = ()
    inspector = _complete_llm_review_inspector(primary_keys_by_table=primary_keys)
    monkeypatch.setattr(db_module, "inspect", lambda engine: inspector)

    with pytest.raises(RuntimeError, match="primary key"):
        _db()._validate_llm_review_constraints()


@pytest.mark.parametrize(
    "table,column,referred_table,referred_column",
    (
        ("cl_decision_llm_review_claim", "event_id", "cl_decision_event", "event_id"),
        ("cl_decision_llm_review_attempt", "review_id", "cl_decision_llm_review_claim", "review_id"),
        ("cl_decision_llm_review_attempt", "event_id", "cl_decision_event", "event_id"),
        ("cl_decision_llm_review", "review_id", "cl_decision_llm_review_claim", "review_id"),
        ("cl_decision_llm_review", "event_id", "cl_decision_event", "event_id"),
        ("cl_decision_llm_review", "risk_snapshot_id", "cl_decision_risk_snapshot", "snapshot_id"),
    ),
)
def test_llm_review_constraint_validation_rejects_missing_foreign_key(monkeypatch, table, column, referred_table, referred_column):
    monkeypatch.setattr(db_module.config, "DB_TYPE", "mysql")
    foreign_keys = _complete_llm_review_foreign_keys()
    foreign_keys[table] = [foreign_key for foreign_key in foreign_keys[table] if foreign_key["constrained_columns"] != [column]]
    inspector = _complete_llm_review_inspector(foreign_keys_by_table=foreign_keys)
    monkeypatch.setattr(db_module, "inspect", lambda engine: inspector)

    with pytest.raises(RuntimeError, match="foreign key is missing: " + table + " " + column + " -> " + referred_table + "." + referred_column):
        _db()._validate_llm_review_constraints()


@pytest.mark.parametrize("missing", ("table", "constraint"))
def test_llm_review_constraint_validation_fails_closed(monkeypatch, missing):
    table_names = set(_LLM_REVIEW_UNIQUE_CONSTRAINTS)
    unique_by_table = {table: set(constraints) for table, constraints in _LLM_REVIEW_UNIQUE_CONSTRAINTS.items()}
    first_table = next(iter(table_names))
    if missing == "table":
        table_names.remove(first_table)
        message = "table is missing"
    else:
        unique_by_table[first_table].pop()
        message = "unique constraint is missing"
    monkeypatch.setattr(db_module.config, "DB_TYPE", "mysql")
    inspector = _complete_llm_review_inspector(table_names=table_names, unique_by_table=unique_by_table)
    monkeypatch.setattr(db_module, "inspect", lambda engine: inspector)

    with pytest.raises(RuntimeError, match=message):
        _db()._validate_llm_review_constraints()


def test_datetime_precision_inspection_reports_only_non_microsecond_columns(
    monkeypatch,
):
    monkeypatch.setattr(db_module.config, "DB_TYPE", "mysql")
    precision = dict.fromkeys(_TARGETS, 0)
    precision[_TARGETS[0]] = 6
    precision[_TARGETS[2]] = None
    inspector = _Inspector(precision)
    monkeypatch.setattr(db_module, "inspect", lambda engine: inspector)

    assert _db()._decision_support_datetime_precision_gaps() == _TARGETS[1:]


@pytest.mark.parametrize(
    ("override", "message"),
    (
        ({"type": MySQLTimestamp(fsp=6)}, "unexpected type"),
        ({"nullable": True}, "unsafe attributes"),
        ({"default": "CURRENT_TIMESTAMP(6)"}, "unsafe attributes"),
        ({"comment": "business timestamp"}, "unsafe attributes"),
        ({"computed": {"sqltext": "CURRENT_TIMESTAMP(6)"}}, "unsafe attributes"),
    ),
)
def test_datetime_precision_inspection_fails_closed_for_unsafe_column_shape(
    monkeypatch,
    override,
    message,
):
    monkeypatch.setattr(db_module.config, "DB_TYPE", "mysql")
    inspector = _Inspector(
        dict.fromkeys(_TARGETS, 6),
        column_overrides={_TARGETS[0]: override},
    )
    monkeypatch.setattr(db_module, "inspect", lambda engine: inspector)

    with pytest.raises(RuntimeError, match=message):
        _db()._decision_support_datetime_precision_gaps()


@pytest.mark.parametrize("missing", ("table", "column"))
def test_datetime_precision_inspection_rejects_missing_target(
    monkeypatch,
    missing,
):
    monkeypatch.setattr(db_module.config, "DB_TYPE", "mysql")
    if missing == "table":
        inspector = _Inspector(
            dict.fromkeys(_TARGETS, 6),
            table_names={table for table, _ in _TARGETS[1:]},
        )
        message = "table is missing"
    else:
        inspector = _Inspector(
            dict.fromkeys(_TARGETS, 6),
            column_overrides={_TARGETS[0]: {"omit": True}},
        )
        message = "column is missing"
    monkeypatch.setattr(db_module, "inspect", lambda engine: inspector)

    with pytest.raises(RuntimeError, match=message):
        _db()._decision_support_datetime_precision_gaps()


def test_datetime_precision_migration_alters_allowlisted_targets_and_rechecks(
    monkeypatch,
):
    monkeypatch.setattr(db_module.config, "DB_TYPE", "mysql")
    connection = _FakeConnection()
    value = _db(_FakeEngine(connection))
    states = deque((_TARGETS, _TARGETS, ()))
    value._decision_support_datetime_precision_gaps = lambda: states.popleft()

    value._migrate_decision_support_datetime_precision()

    alters = [sql for sql, _ in connection.calls if sql.startswith("ALTER TABLE")]
    assert alters == [
        "ALTER TABLE `cl_decision_event` MODIFY `observed_at` DATETIME(6) NOT NULL",
        "ALTER TABLE `cl_decision_transition` MODIFY `occurred_at` DATETIME(6) NOT NULL",
        "ALTER TABLE `cl_decision_review` MODIFY `reviewed_at` DATETIME(6) NOT NULL",
        "ALTER TABLE `cl_decision_user_decision` MODIFY `decided_at` DATETIME(6) NOT NULL",
        "ALTER TABLE `cl_decision_risk_snapshot` MODIFY `observed_at` DATETIME(6) NOT NULL",
        "ALTER TABLE `cl_decision_risk_snapshot` MODIFY `evaluated_at` DATETIME(6) NOT NULL",
        "ALTER TABLE `cl_decision_risk_snapshot` MODIFY `expires_at` DATETIME(6) NOT NULL",
        "ALTER TABLE `cl_decision_paper_admission_authorization` MODIFY `authorized_at` DATETIME(6) NOT NULL",
        "ALTER TABLE `cl_decision_paper_admission_authorization` MODIFY `risk_expires_at` DATETIME(6) NOT NULL",
        "ALTER TABLE `cl_decision_risk_latch_audit` MODIFY `occurred_at` DATETIME(6) NOT NULL",
        "ALTER TABLE `cl_decision_llm_review_claim` MODIFY `lease_expires_at` DATETIME(6) NOT NULL",
        "ALTER TABLE `cl_decision_llm_review_claim` MODIFY `created_at` DATETIME(6) NOT NULL",
        "ALTER TABLE `cl_decision_llm_review_attempt` MODIFY `started_at` DATETIME(6) NOT NULL",
        "ALTER TABLE `cl_decision_llm_review_attempt` MODIFY `completed_at` DATETIME(6) NOT NULL",
        "ALTER TABLE `cl_decision_llm_review` MODIFY `created_at` DATETIME(6) NOT NULL",
    ]
    assert connection.calls[0] == (
        "SELECT GET_LOCK(:name, :timeout)",
        {
            "name": DB.__wrapped__.DECISION_SUPPORT_DATETIME_LOCK,
            "timeout": DB.__wrapped__.MYSQL_SCHEMA_LOCK_TIMEOUT,
        },
    )
    assert connection.calls[-1] == (
        "SELECT RELEASE_LOCK(:name)",
        {"name": DB.__wrapped__.DECISION_SUPPORT_DATETIME_LOCK},
    )
    assert connection.closed is True


def test_datetime_precision_migration_is_idempotent_without_lock(monkeypatch):
    monkeypatch.setattr(db_module.config, "DB_TYPE", "mysql")
    engine = _FakeEngine(_FakeConnection())
    value = _db(engine)
    value._decision_support_datetime_precision_gaps = lambda: ()

    value._migrate_decision_support_datetime_precision()

    assert engine.connect_count == 0


def test_datetime_precision_migration_rechecks_after_acquiring_lock(
    monkeypatch,
):
    monkeypatch.setattr(db_module.config, "DB_TYPE", "mysql")
    connection = _FakeConnection()
    value = _db(_FakeEngine(connection))
    states = deque(((_TARGETS[0],), ()))
    value._decision_support_datetime_precision_gaps = lambda: states.popleft()

    value._migrate_decision_support_datetime_precision()

    assert not any(
        sql.startswith("ALTER TABLE") for sql, _ in connection.calls
    )
    assert connection.calls[-1][0].startswith("SELECT RELEASE_LOCK")


def test_datetime_precision_migration_rejects_lock_failure(monkeypatch):
    monkeypatch.setattr(db_module.config, "DB_TYPE", "mysql")
    connection = _FakeConnection(lock_result=0)
    value = _db(_FakeEngine(connection))
    value._decision_support_datetime_precision_gaps = lambda: (_TARGETS[0],)

    with pytest.raises(RuntimeError, match="migration lock"):
        value._migrate_decision_support_datetime_precision()

    assert connection.closed is True
    assert not any(
        sql.startswith("ALTER TABLE") for sql, _ in connection.calls
    )


def test_datetime_precision_migration_rejects_lock_release_failure(monkeypatch):
    monkeypatch.setattr(db_module.config, "DB_TYPE", "mysql")
    connection = _FakeConnection(release_result=0)
    value = _db(_FakeEngine(connection))
    states = deque(((_TARGETS[0],), ()))
    value._decision_support_datetime_precision_gaps = lambda: states.popleft()

    with pytest.raises(RuntimeError, match="lock release"):
        value._migrate_decision_support_datetime_precision()

    assert connection.closed is True


@pytest.mark.parametrize("fail_alter", (False, True))
def test_datetime_precision_migration_releases_lock_on_failure(
    monkeypatch,
    fail_alter,
):
    monkeypatch.setattr(db_module.config, "DB_TYPE", "mysql")
    connection = _FakeConnection(fail_alter=fail_alter)
    value = _db(_FakeEngine(connection))
    if fail_alter:
        states = deque(((_TARGETS[0],), (_TARGETS[0],)))
        expected = "injected alter failure"
    else:
        states = deque(
            ((_TARGETS[0],), (_TARGETS[0],), (_TARGETS[0],))
        )
        expected = "microsecond precision"
    value._decision_support_datetime_precision_gaps = lambda: states.popleft()

    with pytest.raises(RuntimeError, match=expected):
        value._migrate_decision_support_datetime_precision()

    assert any(
        sql.startswith("SELECT RELEASE_LOCK") for sql, _ in connection.calls
    )
    assert connection.closed is True


def test_mysql_connection_timeouts_cover_schema_lock_and_ddl(monkeypatch):
    captured = {}
    engine = sqlalchemy_create_engine("sqlite:///:memory:")

    def capture_engine(*args, **kwargs):
        captured.update(kwargs)
        return engine

    monkeypatch.setattr(db_module.config, "DB_TYPE", "mysql")
    monkeypatch.delenv("CHANLUN_TESTING", raising=False)
    monkeypatch.setattr(db_module, "create_engine", capture_engine)
    monkeypatch.setattr(db_module.Base.metadata, "create_all", lambda _engine: None)
    monkeypatch.setattr(
        DB.__wrapped__,
        "_migrate_decision_support_datetime_precision",
        lambda self: None,
    )
    monkeypatch.setattr(
        DB.__wrapped__,
        "_migrate_alert_task_unique_constraint",
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
        "_migrate_decision_event_strategy_run_schema",
        lambda self: None,
    )
    monkeypatch.setattr(
        DB.__wrapped__,
        "_validate_decision_event_strategy_run_schema",
        lambda self: None,
    )

    DB.__wrapped__()

    assert captured["connect_args"] == {
        "connect_timeout": 5,
        "read_timeout": DB.__wrapped__.MYSQL_DDL_TIMEOUT,
        "write_timeout": DB.__wrapped__.MYSQL_DDL_TIMEOUT,
    }
    assert (
        DB.__wrapped__.MYSQL_DDL_TIMEOUT
        > DB.__wrapped__.MYSQL_SCHEMA_LOCK_TIMEOUT
    )
