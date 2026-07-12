import pytest
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

from chanlun.db_models.alert_task import TableByAlertTask
from chanlun.db_models.cache import TableByCache
from chanlun.persistence import db as db_module
from chanlun.persistence.db import DB


def _legacy_db(monkeypatch):
    monkeypatch.setattr(db_module.config, "DB_TYPE", "sqlite")
    engine = create_engine("sqlite:///:memory:")
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                CREATE TABLE cl_alert_task (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    market VARCHAR(20),
                    task_name VARCHAR(100)
                )
                """
            )
        )
        connection.execute(
            text(
                """
                INSERT INTO cl_alert_task (market, task_name) VALUES
                    ('a', 'dup'), ('a', 'dup'), ('hk', 'unique')
                """
            )
        )
    TableByCache.__table__.create(engine)
    db_obj = object.__new__(DB.__wrapped__)
    db_obj.engine = engine
    db_obj.Session = sessionmaker(bind=engine, expire_on_commit=False)
    return db_obj


def _rows(db_obj):
    with db_obj.engine.connect() as connection:
        return connection.execute(
            text(
                "SELECT id, market, task_name FROM cl_alert_task "
                "ORDER BY market, task_name, id"
            )
        ).all()


def _has_expected_unique_index(db_obj):
    inspector = inspect(db_obj.engine)
    target = ("market", "task_name")
    constraints = inspector.get_unique_constraints("cl_alert_task")
    indexes = inspector.get_indexes("cl_alert_task")
    return any(tuple(item.get("column_names") or ()) == target for item in constraints) or any(
        item.get("unique") and tuple(item.get("column_names") or ()) == target
        for item in indexes
    )


def test_alert_task_model_declares_unique_market_task_constraint():
    constraints = [
        constraint
        for constraint in TableByAlertTask.__table__.constraints
        if getattr(constraint, "name", None) == "table_market_task_name_unique"
    ]
    assert len(constraints) == 1
    assert tuple(column.name for column in constraints[0].columns) == (
        "market",
        "task_name",
    )


def test_alert_task_migration_deduplicates_existing_rows_and_is_idempotent(monkeypatch):
    db_obj = _legacy_db(monkeypatch)

    db_obj._migrate_alert_task_unique_constraint()
    first_rows = _rows(db_obj)
    db_obj._migrate_alert_task_unique_constraint()

    assert _rows(db_obj) == first_rows
    assert [(row.market, row.task_name) for row in first_rows] == [
        ("a", "dup"),
        ("hk", "unique"),
    ]
    assert first_rows[0].id == 2
    assert _has_expected_unique_index(db_obj)
    marker = db_obj.cache_get(db_obj.ALERT_TASK_UNIQUE_SCHEMA_KEY)
    assert marker == {"version": 1}


def test_alert_task_migration_enforces_unique_pairs(monkeypatch):
    db_obj = _legacy_db(monkeypatch)
    db_obj._migrate_alert_task_unique_constraint()

    with pytest.raises(IntegrityError):
        with db_obj.engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO cl_alert_task (market, task_name) "
                    "VALUES ('a', 'dup')"
                )
            )
