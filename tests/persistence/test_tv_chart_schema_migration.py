from contextlib import nullcontext
from types import SimpleNamespace

from sqlalchemy import String, Text
from sqlalchemy.dialects import mysql
from sqlalchemy.dialects.mysql import LONGTEXT

from chanlun.db_models.tv_charts import (
    TableByTVCharts,
    TV_CHART_NAME_MAX_LENGTH,
)
from chanlun.persistence import db as db_module


class _RecordingConnection:
    def __init__(self):
        self.statements = []

    def exec_driver_sql(self, statement):
        self.statements.append(statement)


def _engine(dialect_name, connection=None):
    connection = connection or _RecordingConnection()
    return SimpleNamespace(
        dialect=SimpleNamespace(name=dialect_name),
        begin=lambda: nullcontext(connection),
    )


def test_tv_chart_name_model_uses_expanded_capacity():
    assert TableByTVCharts.name.type.length == TV_CHART_NAME_MAX_LENGTH


def test_tv_chart_content_model_uses_mysql_longtext():
    mysql_type = TableByTVCharts.content.type.dialect_impl(mysql.dialect())

    assert isinstance(mysql_type, LONGTEXT)


def test_legacy_mysql_name_column_is_expanded(monkeypatch):
    connection = _RecordingConnection()
    engine = _engine("mysql", connection)
    inspector = SimpleNamespace(
        get_columns=lambda table_name: [
            {"name": "id", "type": String(20)},
            {"name": "name", "type": String(50)},
        ]
    )
    monkeypatch.setattr(db_module, "inspect", lambda inspected: inspector)

    changed = db_module._ensure_tv_chart_name_capacity(engine)

    assert changed is True
    assert len(connection.statements) == 1
    assert "ALTER TABLE `cl_tv_charts`" in connection.statements[0]
    assert f"VARCHAR({TV_CHART_NAME_MAX_LENGTH})" in connection.statements[0]


def test_current_mysql_name_column_is_not_altered(monkeypatch):
    connection = _RecordingConnection()
    engine = _engine("mysql", connection)
    inspector = SimpleNamespace(
        get_columns=lambda table_name: [
            {"name": "name", "type": String(TV_CHART_NAME_MAX_LENGTH)}
        ]
    )
    monkeypatch.setattr(db_module, "inspect", lambda inspected: inspector)

    changed = db_module._ensure_tv_chart_name_capacity(engine)

    assert changed is False
    assert connection.statements == []


def test_sqlite_does_not_run_mysql_schema_upgrade(monkeypatch):
    engine = _engine("sqlite")

    def unexpected_inspection(_engine):
        raise AssertionError("SQLite schema must not be inspected by the MySQL migration")

    monkeypatch.setattr(db_module, "inspect", unexpected_inspection)

    assert db_module._ensure_tv_chart_name_capacity(engine) is False


def test_legacy_mysql_content_column_is_expanded(monkeypatch):
    connection = _RecordingConnection()
    engine = _engine("mysql", connection)
    inspector = SimpleNamespace(
        get_columns=lambda table_name: [
            {"name": "content", "type": Text()},
        ]
    )
    monkeypatch.setattr(db_module, "inspect", lambda inspected: inspector)

    changed = db_module._ensure_tv_chart_content_capacity(engine)

    assert changed is True
    assert connection.statements == [
        "ALTER TABLE `cl_tv_charts` "
        "MODIFY COLUMN `content` LONGTEXT NULL COMMENT '布局内容'"
    ]


def test_current_mysql_content_column_is_not_altered(monkeypatch):
    connection = _RecordingConnection()
    engine = _engine("mysql", connection)
    inspector = SimpleNamespace(
        get_columns=lambda table_name: [
            {"name": "content", "type": LONGTEXT()},
        ]
    )
    monkeypatch.setattr(db_module, "inspect", lambda inspected: inspector)

    changed = db_module._ensure_tv_chart_content_capacity(engine)

    assert changed is False
    assert connection.statements == []


def test_sqlite_does_not_run_content_schema_upgrade(monkeypatch):
    engine = _engine("sqlite")

    def unexpected_inspection(_engine):
        raise AssertionError("SQLite schema must not be inspected by the MySQL migration")

    monkeypatch.setattr(db_module, "inspect", unexpected_inspection)

    assert db_module._ensure_tv_chart_content_capacity(engine) is False
