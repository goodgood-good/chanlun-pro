import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.dialects import mysql
from sqlalchemy.orm import sessionmaker

from chanlun import config
from chanlun.db_models.cache import TableByCache
from chanlun.persistence.db import DB


def _isolated_db():
    engine = create_engine("sqlite:///:memory:")
    TableByCache.__table__.create(engine)
    db_obj = object.__new__(DB.__wrapped__)
    db_obj.Session = sessionmaker(bind=engine, expire_on_commit=False)
    return db_obj, engine


def test_cache_set_many_rolls_back_every_key_when_one_row_fails():
    db_obj, engine = _isolated_db()
    db_obj.cache_set_many(
        {
            "req_proxy": {"host": "old", "port": "1"},
            "fs_keys": {"fs_app_id": "old"},
        }
    )
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                CREATE TRIGGER reject_fs_keys_update
                BEFORE UPDATE ON cl_cache
                WHEN NEW.k = 'fs_keys'
                BEGIN
                    SELECT RAISE(ABORT, 'rejected fs_keys update');
                END
                """
            )
        )

    with pytest.raises(Exception, match="rejected fs_keys update"):
        db_obj.cache_set_many(
            {
                "req_proxy": {"host": "new", "port": "2"},
                "fs_keys": {"fs_app_id": "new"},
            }
        )

    assert db_obj.cache_get("req_proxy") == {"host": "old", "port": "1"}
    assert db_obj.cache_get("fs_keys") == {"fs_app_id": "old"}


class _RecordingSession:
    def __init__(self):
        self.statement = None
        self.commits = 0
        self.rollbacks = 0

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False

    def execute(self, statement):
        self.statement = statement

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1


def test_cache_set_many_uses_one_mysql_multirow_upsert(monkeypatch):
    db_obj = object.__new__(DB.__wrapped__)
    session = _RecordingSession()
    db_obj.Session = lambda: session
    monkeypatch.setattr(config, "DB_TYPE", "mysql")

    db_obj.cache_set_many({"req_proxy": {"host": "proxy"}, "fs_keys": {"id": "x"}})

    sql = str(session.statement.compile(dialect=mysql.dialect()))
    assert sql.count("%s") == 6
    assert "ON DUPLICATE KEY UPDATE" in sql
    assert session.commits == 1
    assert session.rollbacks == 0
