import datetime
import json
import os
import pathlib
import sys
import threading
import time
import warnings
from typing import List, Union

import pandas as pd
from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    Float,
    Integer,
    String,
    UniqueConstraint,
    event,
    Index,
    create_engine,
    func,
    inspect,
    text,
)
from sqlalchemy.dialects import mysql as mysql_dialect
from sqlalchemy.dialects.mysql import DATETIME as MySQLDateTime
from sqlalchemy.dialects.mysql import INTEGER as MySQLInteger
from sqlalchemy.dialects.mysql import LONGTEXT as MySQLLongText
from sqlalchemy.dialects.mysql import TINYINT as MySQLTinyInt
from sqlalchemy.dialects.mysql import VARCHAR as MySQLVarchar
from sqlalchemy.dialects.mysql import insert
from sqlalchemy.engine import URL
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import QueuePool

from chanlun import config, fun
from chanlun.market import Market
from chanlun.config import get_data_path
from chanlun.tools.log_util import LogUtil

warnings.filterwarnings("ignore")

# https://docs.sqlalchemy.org/en/20/core/types.html

from chanlun.db_models.base import Base
from chanlun.db_models.decision_support import (
    TableByDecisionEvent,
    TableByDecisionReview,
    TableByDecisionTransition,
    TableByPaperAdmissionAuthorization,
    TableByRiskLatchAudit,
    TableByRiskSnapshot,
    TableByUserDecision,
    TableByLLMReview,
    TableByLLMReviewAttempt,
    TableByLLMReviewClaim,
)
from chanlun.db_models.alert_record import TableByAlertRecord
from chanlun.db_models.alert_task import TableByAlertTask
from chanlun.db_models.cache import TableByCache
from chanlun.db_models.order import TableByOrder
from chanlun.db_models.tv_charts import TableByTVCharts
from chanlun.db_models.tv_marks import TableByTVMarks
from chanlun.db_models.tv_marks_price import TableByTVMarksPrice
from chanlun.db_models.zixuan import TableByZixuan
from chanlun.db_models.zixuan_group import TableByZxGroup


_REGISTERED_DECISION_SUPPORT_MODELS = (
    TableByDecisionEvent,
    TableByDecisionReview,
    TableByDecisionTransition,
    TableByPaperAdmissionAuthorization,
    TableByRiskLatchAudit,
    TableByRiskSnapshot,
    TableByUserDecision,
    TableByLLMReview,
    TableByLLMReviewAttempt,
    TableByLLMReviewClaim,
)

_SQLITE_BUSY_TIMEOUT_MS = 5_000


def _configure_sqlite_connection(dbapi_connection, _connection_record) -> None:
    cursor = dbapi_connection.cursor()
    try:
        cursor.execute("PRAGMA foreign_keys = ON")
        cursor.execute(f"PRAGMA busy_timeout = {_SQLITE_BUSY_TIMEOUT_MS}")
        cursor.execute("PRAGMA foreign_keys")
        foreign_keys = cursor.fetchone()
        cursor.execute("PRAGMA busy_timeout")
        busy_timeout = cursor.fetchone()
    finally:
        cursor.close()

    if foreign_keys != (1,):
        raise RuntimeError("SQLite foreign key enforcement could not be enabled")
    if busy_timeout != (_SQLITE_BUSY_TIMEOUT_MS,):
        raise RuntimeError("SQLite busy timeout could not be configured")


def _llm_review_mysql_type_matches(expected_column, actual_type) -> bool:
    expected_type = expected_column.type.dialect_impl(mysql_dialect.dialect())
    if isinstance(expected_type, MySQLLongText):
        return isinstance(actual_type, MySQLLongText)

    affinity = expected_type._type_affinity
    if affinity is Boolean:
        return isinstance(actual_type, Boolean) or (
            isinstance(actual_type, MySQLTinyInt)
            and getattr(actual_type, "display_width", None) == 1
        )
    if affinity is Integer:
        return isinstance(actual_type, MySQLInteger) and not isinstance(
            actual_type,
            MySQLTinyInt,
        )
    if affinity is DateTime:
        return isinstance(actual_type, MySQLDateTime) and getattr(
            actual_type,
            "fsp",
            None,
        ) == getattr(expected_type, "fsp", None)
    if affinity is String:
        if not isinstance(actual_type, MySQLVarchar):
            return False
        if getattr(actual_type, "length", None) != getattr(
            expected_type,
            "length",
            None,
        ):
            return False
        expected_collation = getattr(expected_type, "collation", None)
        return expected_collation is None or getattr(
            actual_type,
            "collation",
            None,
        ) == expected_collation
    return False


def _build_mysql_database_url(user, password, host, port, database) -> URL:
    """Build a MySQL URL without reparsing reserved credential characters."""
    return URL.create(
        "mysql+pymysql",
        username=user,
        password=password,
        host=host,
        port=int(port),
        database=database,
        query={"charset": "utf8mb4"},
    )


def _assert_safe_test_database_config() -> None:
    if os.environ.get("CHANLUN_TESTING") != "1":
        return

    expected_raw = os.environ.get("CHANLUN_TEST_DATA_PATH")
    configured_path = pathlib.Path(config.DATA_PATH).expanduser()
    if str(config.DATA_PATH).startswith("."):
        configured_path = pathlib.Path.home() / configured_path
    configured_path = configured_path.resolve()
    expected_path = pathlib.Path(expected_raw).resolve() if expected_raw else None

    if config.DB_TYPE != "sqlite" or expected_path is None or configured_path != expected_path:
        raise RuntimeError(
            "Tests require an isolated SQLite database under CHANLUN_TEST_DATA_PATH"
        )


@fun.singleton
class DB(object):
    """SQLAlchemy ORM 封装的数据库访问单例，支持 MySQL 和 SQLite。"""

    def __new__(cls, *args, **kwargs):
        _assert_safe_test_database_config()
        return super().__new__(cls)

    ALERT_TASK_UNIQUE_SCHEMA_KEY = "__schema_alert_task_unique_v1"
    ALERT_TASK_UNIQUE_INDEX = "uq_cl_alert_task_market_task_name_v1"
    DECISION_SUPPORT_DATETIME_LOCK = "cl_decision_support_datetime_fsp6_v1"
    DECISION_EVENT_STRATEGY_RUN_LOCK = (
        "cl_decision_event_strategy_run_schema_v1"
    )
    LLM_REVIEW_RISK_SNAPSHOT_LOCK = (
        "cl_decision_llm_review_risk_snapshot_schema_v1"
    )
    LLM_REVIEW_RISK_SNAPSHOT_FOREIGN_KEY_NAME = (
        "fk_cl_decision_llm_review_risk_snapshot_id"
    )
    DECISION_EVENT_STRATEGY_RUN_INDEX_NAME = (
        "ix_cl_decision_event_strategy_run_observed"
    )
    DECISION_EVENT_STRATEGY_RUN_COLUMN_DDL = (
        (
            "strategy_run_id",
            "VARCHAR(80) CHARACTER SET utf8mb4 COLLATE utf8mb4_bin NULL",
        ),
        ("strategy_run_epoch", "INT NULL"),
        (
            "strategy_run_fingerprint",
            "VARCHAR(71) CHARACTER SET utf8mb4 COLLATE utf8mb4_bin NULL",
        ),
    )
    MYSQL_SCHEMA_LOCK_TIMEOUT = 30
    MYSQL_DDL_TIMEOUT = 180
    SQLITE_BUSY_TIMEOUT_MS = _SQLITE_BUSY_TIMEOUT_MS
    SQLITE_DECISION_SUPPORT_REQUIRED_COLUMNS = {
        table.__tablename__: frozenset(table.__table__.columns.keys())
        for table in _REGISTERED_DECISION_SUPPORT_MODELS
    }
    DECISION_EVENT_STRATEGY_RUN_INDEX = (
        "strategy_run_id",
        "strategy_run_epoch",
        "strategy_run_fingerprint",
        "observed_at",
    )
    SQLITE_DECISION_SUPPORT_REQUIRED_INDEXES = {
        "cl_decision_event": frozenset({DECISION_EVENT_STRATEGY_RUN_INDEX}),
    }
    DECISION_SUPPORT_DATETIME_COLUMNS = (
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
    LLM_REVIEW_UNIQUE_CONSTRAINTS = {
        "cl_decision_llm_review_claim": frozenset(
            {
                ("review_id",),
                (
                    "event_id",
                    "packet_fingerprint",
                    "provider",
                    "model",
                    "prompt_version",
                ),
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
                (
                    "event_id",
                    "packet_fingerprint",
                    "provider",
                    "model",
                    "prompt_version",
                ),
            }
        ),
    }

    LLM_REVIEW_REQUIRED_COLUMNS = {
        table.__tablename__: frozenset(table.__table__.columns.keys())
        for table in (TableByLLMReviewClaim, TableByLLMReviewAttempt, TableByLLMReview)
    }
    LLM_REVIEW_TABLE_MODELS = {
        "cl_decision_llm_review_claim": TableByLLMReviewClaim,
        "cl_decision_llm_review_attempt": TableByLLMReviewAttempt,
        "cl_decision_llm_review": TableByLLMReview,
    }
    LLM_REVIEW_IDENTITY_COLUMNS = {
        "cl_decision_llm_review_claim": frozenset({"review_id", "packet_fingerprint", "provider", "model", "prompt_version", "owner_token"}),
        "cl_decision_llm_review_attempt": frozenset({"attempt_id", "review_id", "owner_token", "provider", "model"}),
        "cl_decision_llm_review": frozenset({"review_id", "risk_snapshot_id", "packet_fingerprint", "reviewed_data_fingerprint", "provider", "model", "prompt_version"}),
    }
    LLM_REVIEW_AUDIT_TEXT_COLUMNS = {
        "cl_decision_llm_review_attempt": frozenset({"response_content", "raw_response", "error_message"}),
        "cl_decision_llm_review": frozenset({"response_content", "raw_response", "parsed_response_json", "validation_errors_json", "error_message"}),
    }
    LLM_REVIEW_FOREIGN_KEYS = {
        "cl_decision_llm_review_claim": frozenset({("event_id", "cl_decision_event", "event_id")}),
        "cl_decision_llm_review_attempt": frozenset({("review_id", "cl_decision_llm_review_claim", "review_id"), ("event_id", "cl_decision_event", "event_id")}),
        "cl_decision_llm_review": frozenset({("review_id", "cl_decision_llm_review_claim", "review_id"), ("event_id", "cl_decision_event", "event_id"), ("risk_snapshot_id", "cl_decision_risk_snapshot", "snapshot_id")}),
    }
    def __init__(self) -> None:
        if config.DB_TYPE == "sqlite":
            db_path = get_data_path() / "db"
            if db_path.is_dir() is False:
                db_path.mkdir(parents=True)
            self.engine = create_engine(
                f"sqlite:///{str(db_path / f'{config.DB_DATABASE}.sqlite')}",
                echo=False,
                poolclass=QueuePool,
                pool_size=10,
                max_overflow=20,
                pool_timeout=10,
                connect_args={
                    "check_same_thread": False,
                    "timeout": self.SQLITE_BUSY_TIMEOUT_MS / 1_000,
                },
            )
            event.listen(self.engine, "connect", _configure_sqlite_connection)
        elif config.DB_TYPE == "mysql":
            self.engine = create_engine(
                _build_mysql_database_url(
                    config.DB_USER,
                    config.DB_PWD,
                    config.DB_HOST,
                    config.DB_PORT,
                    config.DB_DATABASE,
                ),
                echo=False,
                poolclass=QueuePool,
                pool_recycle=1800,
                pool_pre_ping=True,
                pool_use_lifo=True,
                pool_reset_on_return="rollback",
                pool_size=10,
                max_overflow=20,
                pool_timeout=10,
                connect_args={
                    "connect_timeout": 5,
                    "read_timeout": self.MYSQL_DDL_TIMEOUT,
                    "write_timeout": self.MYSQL_DDL_TIMEOUT,
                },
            )
        else:
            raise Exception("DB_TYPE 配置错误")

        # 避免提交后对象过期导致二次加载，提高查询性能
        self.Session = sessionmaker(bind=self.engine, expire_on_commit=False)

        Base.metadata.create_all(self.engine)
        self._validate_sqlite_decision_support_schema()
        self._migrate_decision_event_strategy_run_schema()
        self._validate_decision_event_strategy_run_schema()
        self._migrate_decision_support_datetime_precision()
        self._migrate_llm_review_risk_snapshot_schema()
        self._validate_llm_review_constraints()
        self._migrate_alert_task_unique_constraint()

        self.__cache_tables = {}
        # 轻量级缓存：最后一根K线时间，降低重复查询成本。
        # 为避免多线程并发读写出现可见性问题（写入新 K 线时缓存与 DB 不一致），
        # 使用一个独立的锁保护 _last_dt_cache 的所有读写。
        self._last_dt_cache: dict = {}
        self._last_dt_cache_generation: dict = {}
        self._last_dt_cache_lock = threading.Lock()
        # R6-#2: 保护 __cache_tables 的 check-then-act + 动态建 ORM 表类(同 get_exchange 审查 B-1)
        self._cache_tables_lock = threading.Lock()

    def _validate_sqlite_decision_support_schema(self) -> None:
        if config.DB_TYPE != "sqlite":
            return

        inspector = inspect(self.engine)
        actual_tables = set(inspector.get_table_names())
        missing_tables = (
            set(self.SQLITE_DECISION_SUPPORT_REQUIRED_COLUMNS) - actual_tables
        )
        if missing_tables:
            raise RuntimeError(
                "SQLite decision-support schema table is missing: "
                + ", ".join(sorted(missing_tables))
            )

        for table_name in sorted(self.SQLITE_DECISION_SUPPORT_REQUIRED_COLUMNS):
            actual_columns = {
                str(column["name"])
                for column in inspector.get_columns(table_name)
            }
            missing_columns = (
                self.SQLITE_DECISION_SUPPORT_REQUIRED_COLUMNS[table_name]
                - actual_columns
            )
            if missing_columns:
                raise RuntimeError(
                    "SQLite decision-support schema column is missing: "
                    + table_name
                    + " "
                    + ", ".join(sorted(missing_columns))
                )

        for (
            table_name,
            required_indexes,
        ) in self.SQLITE_DECISION_SUPPORT_REQUIRED_INDEXES.items():
            actual_indexes = {
                tuple(index.get("column_names") or ())
                for index in inspector.get_indexes(table_name)
            }
            missing_indexes = required_indexes - actual_indexes
            if missing_indexes:
                formatted = ", ".join(
                    "(" + ",".join(columns) + ")"
                    for columns in sorted(missing_indexes)
                )
                raise RuntimeError(
                    "SQLite decision-support schema index is missing: "
                    + table_name
                    + " "
                    + formatted
                )

    def _decision_event_strategy_run_schema_gaps(self):
        table_name = TableByDecisionEvent.__tablename__
        inspector = inspect(self.engine)
        if table_name not in set(inspector.get_table_names()):
            raise RuntimeError(
                "decision-event strategy-run migration table is missing: "
                + table_name
            )

        actual_columns = {
            str(column.get("name"))
            for column in inspector.get_columns(table_name)
        }
        missing_columns = tuple(
            column_name
            for column_name, _column_ddl in self.DECISION_EVENT_STRATEGY_RUN_COLUMN_DDL
            if column_name not in actual_columns
        )
        actual_indexes = {
            tuple(index.get("column_names") or ())
            for index in inspector.get_indexes(table_name)
        }
        return (
            missing_columns,
            self.DECISION_EVENT_STRATEGY_RUN_INDEX not in actual_indexes,
        )

    def _migrate_decision_event_strategy_run_schema(self) -> None:
        if config.DB_TYPE != "mysql":
            return

        missing_columns, missing_index = (
            self._decision_event_strategy_run_schema_gaps()
        )
        if not missing_columns and not missing_index:
            return

        table_name = TableByDecisionEvent.__tablename__
        column_definitions = dict(self.DECISION_EVENT_STRATEGY_RUN_COLUMN_DDL)
        connection = self.engine.connect()
        lock_acquired = False
        try:
            acquired = connection.execute(
                text("SELECT GET_LOCK(:name, :timeout)"),
                {
                    "name": self.DECISION_EVENT_STRATEGY_RUN_LOCK,
                    "timeout": self.MYSQL_SCHEMA_LOCK_TIMEOUT,
                },
            ).scalar()
            if acquired != 1:
                raise RuntimeError(
                    "decision-event strategy-run migration lock acquisition failed"
                )
            lock_acquired = True

            missing_columns, missing_index = (
                self._decision_event_strategy_run_schema_gaps()
            )
            for column_name in missing_columns:
                connection.execute(
                    text(
                        f"ALTER TABLE `{table_name}` ADD COLUMN `{column_name}` "
                        + column_definitions[column_name]
                    )
                )

            if missing_index:
                index_columns = ", ".join(
                    f"`{column_name}`"
                    for column_name in self.DECISION_EVENT_STRATEGY_RUN_INDEX
                )
                connection.execute(
                    text(
                        f"CREATE INDEX `{self.DECISION_EVENT_STRATEGY_RUN_INDEX_NAME}` "
                        f"ON `{table_name}` ({index_columns})"
                    )
                )

            remaining_columns, remaining_index = (
                self._decision_event_strategy_run_schema_gaps()
            )
            if remaining_columns or remaining_index:
                missing = list(remaining_columns)
                if remaining_index:
                    missing.append(self.DECISION_EVENT_STRATEGY_RUN_INDEX_NAME)
                raise RuntimeError(
                    "decision-event strategy-run migration incomplete: "
                    + ", ".join(missing)
                )
        finally:
            active_error = sys.exc_info()[1]
            release_error = None
            try:
                if lock_acquired:
                    released = connection.execute(
                        text("SELECT RELEASE_LOCK(:name)"),
                        {"name": self.DECISION_EVENT_STRATEGY_RUN_LOCK},
                    ).scalar()
                    if released != 1:
                        release_error = RuntimeError(
                            "decision-event strategy-run migration lock release failed"
                        )
            except Exception as exc:
                release_error = exc
            finally:
                connection.close()
            if release_error is not None:
                if active_error is None:
                    if isinstance(release_error, RuntimeError):
                        raise release_error
                    raise RuntimeError(
                        "decision-event strategy-run migration lock release failed"
                    ) from release_error
                LogUtil.error(
                    "[schema migration] decision-event strategy-run lock release "
                    f"failed after migration error: {release_error}"
                )

    def _validate_decision_event_strategy_run_schema(self) -> None:
        if config.DB_TYPE != "mysql":
            return

        table_name = TableByDecisionEvent.__tablename__
        inspector = inspect(self.engine)
        if table_name not in set(inspector.get_table_names()):
            raise RuntimeError(
                "decision-event strategy-run table is missing: " + table_name
            )

        expected_columns = {
            column_name: TableByDecisionEvent.__table__.c[column_name]
            for column_name in (
                "strategy_run_id",
                "strategy_run_epoch",
                "strategy_run_fingerprint",
            )
        }
        actual_columns = {
            str(column.get("name")): column
            for column in inspector.get_columns(table_name)
        }
        missing_columns = set(expected_columns) - set(actual_columns)
        if missing_columns:
            raise RuntimeError(
                "decision-event strategy-run column is missing: "
                + table_name
                + " "
                + ", ".join(sorted(missing_columns))
            )

        for column_name, expected_column in expected_columns.items():
            actual_column = actual_columns[column_name]
            if not _llm_review_mysql_type_matches(
                expected_column,
                actual_column.get("type"),
            ):
                raise RuntimeError(
                    "decision-event strategy-run column schema mismatch: "
                    + table_name
                    + " "
                    + column_name
                )
            actual_nullable = actual_column.get("nullable")
            if type(actual_nullable) is not bool or actual_nullable is not True:
                raise RuntimeError(
                    "decision-event strategy-run column nullability mismatch: "
                    + table_name
                    + " "
                    + column_name
                )

        actual_indexes = {
            tuple(index.get("column_names") or ())
            for index in inspector.get_indexes(table_name)
        }
        if self.DECISION_EVENT_STRATEGY_RUN_INDEX not in actual_indexes:
            raise RuntimeError(
                "decision-event strategy-run index is missing: "
                + table_name
                + " ("
                + ",".join(self.DECISION_EVENT_STRATEGY_RUN_INDEX)
                + ")"
            )

    def _llm_review_risk_snapshot_schema_state(self, bind=None):
        table_name = TableByLLMReview.__tablename__
        referred_table = TableByRiskSnapshot.__tablename__
        inspector = inspect(self.engine if bind is None else bind)
        tables = set(inspector.get_table_names())
        missing_tables = {table_name, referred_table} - tables
        if missing_tables:
            raise RuntimeError(
                "LLM review risk-snapshot migration table is missing: "
                + ", ".join(sorted(missing_tables))
            )

        columns = {
            str(column.get("name")): column
            for column in inspector.get_columns(table_name)
        }
        foreign_key_present = any(
            tuple(foreign_key.get("constrained_columns") or ())
            == ("risk_snapshot_id",)
            and foreign_key.get("referred_table") == referred_table
            and tuple(foreign_key.get("referred_columns") or ())
            == ("snapshot_id",)
            for foreign_key in inspector.get_foreign_keys(table_name)
        )
        return columns.get("risk_snapshot_id"), foreign_key_present

    def _validate_llm_review_risk_snapshot_migration_column(self, column) -> None:
        if column is None:
            return
        expected_column = TableByLLMReview.__table__.c["risk_snapshot_id"]
        unsafe_attributes = (
            column.get("default") is not None
            or column.get("comment") not in (None, "")
            or column.get("computed") is not None
            or column.get("identity") is not None
            or column.get("autoincrement") not in (None, False)
        )
        if (
            not _llm_review_mysql_type_matches(
                expected_column,
                column.get("type"),
            )
            or column.get("nullable") is not False
            or unsafe_attributes
        ):
            raise RuntimeError(
                "LLM review risk-snapshot column has an unsafe shape; "
                "manual migration required"
            )

    def _migrate_llm_review_risk_snapshot_schema(self) -> None:
        if config.DB_TYPE != "mysql":
            return

        column, foreign_key_present = (
            self._llm_review_risk_snapshot_schema_state()
        )
        self._validate_llm_review_risk_snapshot_migration_column(column)
        if column is not None and foreign_key_present:
            return

        table_name = TableByLLMReview.__tablename__
        referred_table = TableByRiskSnapshot.__tablename__
        connection = self.engine.connect()
        lock_acquired = False
        table_locks_acquired = False
        try:
            acquired = connection.execute(
                text("SELECT GET_LOCK(:name, :timeout)"),
                {
                    "name": self.LLM_REVIEW_RISK_SNAPSHOT_LOCK,
                    "timeout": self.MYSQL_SCHEMA_LOCK_TIMEOUT,
                },
            ).scalar()
            if acquired != 1:
                raise RuntimeError(
                    "LLM review risk-snapshot migration lock acquisition failed"
                )
            lock_acquired = True

            column, foreign_key_present = (
                self._llm_review_risk_snapshot_schema_state()
            )
            if column is None or not foreign_key_present:
                connection.execute(
                    text(
                        f"LOCK TABLES `{table_name}` WRITE, "
                        f"`{referred_table}` READ"
                    )
                )
                table_locks_acquired = True
                column, foreign_key_present = (
                    self._llm_review_risk_snapshot_schema_state(connection)
                )
                self._validate_llm_review_risk_snapshot_migration_column(
                    column
                )
            if column is None:
                if foreign_key_present:
                    raise RuntimeError(
                        "LLM review risk-snapshot schema is inconsistent; "
                        "manual migration required"
                    )
                row_count = connection.execute(
                    text(f"SELECT COUNT(*) FROM `{table_name}`")
                ).scalar()
                if row_count != 0:
                    raise RuntimeError(
                        "LLM review risk-snapshot column cannot be inferred for "
                        "a nonempty table; manual migration required"
                    )
                connection.execute(
                    text(
                        f"ALTER TABLE `{table_name}` "
                        "ADD COLUMN `risk_snapshot_id` "
                        "VARCHAR(255) CHARACTER SET utf8mb4 "
                        "COLLATE utf8mb4_bin NOT NULL, "
                        f"ADD CONSTRAINT `{self.LLM_REVIEW_RISK_SNAPSHOT_FOREIGN_KEY_NAME}` "
                        "FOREIGN KEY (`risk_snapshot_id`) REFERENCES "
                        f"`{referred_table}` (`snapshot_id`) ON DELETE RESTRICT"
                    )
                )
            elif not foreign_key_present:
                orphan_count = connection.execute(
                    text(
                        f"SELECT COUNT(*) FROM `{table_name}` "
                        f"LEFT JOIN `{referred_table}` "
                        f"ON `{table_name}`.`risk_snapshot_id` = "
                        f"`{referred_table}`.`snapshot_id` "
                        f"WHERE `{table_name}`.`risk_snapshot_id` IS NULL "
                        f"OR `{referred_table}`.`snapshot_id` IS NULL"
                    )
                ).scalar()
                if orphan_count != 0:
                    raise RuntimeError(
                        "LLM review risk-snapshot orphan rows exist; "
                        "manual migration required"
                    )
                connection.execute(
                    text(
                        f"ALTER TABLE `{table_name}` "
                        f"ADD CONSTRAINT `{self.LLM_REVIEW_RISK_SNAPSHOT_FOREIGN_KEY_NAME}` "
                        "FOREIGN KEY (`risk_snapshot_id`) REFERENCES "
                        f"`{referred_table}` (`snapshot_id`) ON DELETE RESTRICT"
                    )
                )

            if table_locks_acquired:
                connection.execute(text("UNLOCK TABLES"))
                table_locks_acquired = False

            remaining_column, remaining_foreign_key = (
                self._llm_review_risk_snapshot_schema_state()
            )
            if remaining_column is None or not remaining_foreign_key:
                raise RuntimeError(
                    "LLM review risk-snapshot migration incomplete"
                )
        finally:
            active_error = sys.exc_info()[1]
            cleanup_errors = []
            try:
                if table_locks_acquired:
                    connection.execute(text("UNLOCK TABLES"))
            except Exception as exc:
                cleanup_errors.append(
                    (
                        "LLM review risk-snapshot table unlock failed",
                        exc,
                    )
                )
            try:
                if lock_acquired:
                    released = connection.execute(
                        text("SELECT RELEASE_LOCK(:name)"),
                        {"name": self.LLM_REVIEW_RISK_SNAPSHOT_LOCK},
                    ).scalar()
                    if released != 1:
                        cleanup_errors.append(
                            (
                                "LLM review risk-snapshot migration lock release failed",
                                RuntimeError(
                                    "LLM review risk-snapshot migration lock release failed"
                                ),
                            )
                        )
            except Exception as exc:
                cleanup_errors.append(
                    (
                        "LLM review risk-snapshot migration lock release failed",
                        exc,
                    )
                )
            finally:
                connection.close()
            if cleanup_errors:
                if active_error is None:
                    message, cleanup_error = cleanup_errors[0]
                    if isinstance(cleanup_error, RuntimeError):
                        raise cleanup_error
                    raise RuntimeError(message) from cleanup_error
                for message, cleanup_error in cleanup_errors:
                    LogUtil.error(
                        "[schema migration] "
                        f"{message} after migration error: {cleanup_error}"
                    )

    def _validate_llm_review_constraints(self) -> None:
        if config.DB_TYPE != "mysql":
            return
        inspector = inspect(self.engine)
        tables = set(inspector.get_table_names())
        for table_name, required_uniques in self.LLM_REVIEW_UNIQUE_CONSTRAINTS.items():
            if table_name not in tables:
                raise RuntimeError("LLM review audit table is missing: " + table_name)

            columns = {column["name"]: column for column in inspector.get_columns(table_name)}
            missing_columns = self.LLM_REVIEW_REQUIRED_COLUMNS[table_name] - set(columns)
            if missing_columns:
                raise RuntimeError(
                    "LLM review audit column is missing: "
                    + table_name
                    + " "
                    + ", ".join(sorted(missing_columns))
                )

            model = self.LLM_REVIEW_TABLE_MODELS[table_name]
            for expected_column in model.__table__.columns:
                column_name = expected_column.name
                actual_column = columns[column_name]
                if not _llm_review_mysql_type_matches(
                    expected_column,
                    actual_column["type"],
                ):
                    if column_name in self.LLM_REVIEW_IDENTITY_COLUMNS.get(
                        table_name,
                        (),
                    ):
                        message = (
                            "LLM review audit identity column must be VARCHAR "
                            "utf8mb4_bin with its exact declared length: "
                        )
                    elif column_name in self.LLM_REVIEW_AUDIT_TEXT_COLUMNS.get(
                        table_name,
                        (),
                    ):
                        message = "LLM review audit column must be LONGTEXT: "
                    else:
                        message = "LLM review audit column schema mismatch: "
                    raise RuntimeError(message + table_name + " " + column_name)

                actual_nullable = actual_column.get("nullable")
                if (
                    type(actual_nullable) is not bool
                    or actual_nullable is not expected_column.nullable
                ):
                    raise RuntimeError(
                        "LLM review audit column nullability mismatch: "
                        + table_name
                        + " "
                        + column_name
                    )

            primary_key = tuple(
                inspector.get_pk_constraint(table_name).get(
                    "constrained_columns",
                    (),
                )
                or ()
            )
            if primary_key != ("id",):
                raise RuntimeError(
                    "LLM review audit primary key must be id: " + table_name
                )

            actual_foreign_keys = {
                (
                    tuple(foreign_key.get("constrained_columns") or ()),
                    foreign_key.get("referred_table"),
                    tuple(foreign_key.get("referred_columns") or ()),
                ): foreign_key
                for foreign_key in inspector.get_foreign_keys(table_name)
            }
            for column_name, referred_table, referred_column in self.LLM_REVIEW_FOREIGN_KEYS.get(table_name, ()):
                foreign_key = actual_foreign_keys.get(
                    ((column_name,), referred_table, (referred_column,))
                )
                if foreign_key is None:
                    raise RuntimeError(
                        "LLM review audit foreign key is missing: "
                        + table_name
                        + " "
                        + column_name
                        + " -> "
                        + referred_table
                        + "."
                        + referred_column
                    )
                options = foreign_key.get("options") or {}
                ondelete = options.get("ondelete")
                if ondelete is not None and str(ondelete).upper() != "RESTRICT":
                    raise RuntimeError(
                        "LLM review audit foreign key must use RESTRICT deletion: "
                        + table_name
                        + " "
                        + column_name
                    )

            actual_uniques = {
                tuple(constraint.get("column_names") or ())
                for constraint in inspector.get_unique_constraints(table_name)
            }
            missing_uniques = required_uniques - actual_uniques
            if missing_uniques:
                formatted = ", ".join("(" + ",".join(item) + ")" for item in sorted(missing_uniques))
                raise RuntimeError(
                    "LLM review audit unique constraint is missing: "
                    + table_name
                    + " "
                    + formatted
                )

    def _decision_support_datetime_precision_gaps(self):
        if config.DB_TYPE != "mysql":
            return ()
        inspector = inspect(self.engine)
        tables = set(inspector.get_table_names())
        gaps = []
        for table_name, column_name in self.DECISION_SUPPORT_DATETIME_COLUMNS:
            if table_name not in tables:
                raise RuntimeError(
                    "decision-support datetime table is missing: " + table_name
                )
            columns = {
                str(item.get("name")): item
                for item in inspector.get_columns(table_name)
            }
            if column_name not in columns:
                raise RuntimeError(
                    "decision-support datetime column is missing: "
                    f"{table_name}.{column_name}"
                )
            column = columns[column_name]
            column_type = column.get("type")
            target = f"{table_name}.{column_name}"
            if not isinstance(column_type, MySQLDateTime):
                raise RuntimeError(
                    "decision-support datetime has unexpected type; "
                    f"manual migration required: {target}"
                )
            unsafe_attributes = (
                column.get("nullable") is not False
                or column.get("default") is not None
                or column.get("comment") not in (None, "")
                or column.get("computed") is not None
                or column.get("identity") is not None
                or column.get("autoincrement") not in (None, False)
            )
            if unsafe_attributes:
                raise RuntimeError(
                    "decision-support datetime has unsafe attributes; "
                    f"manual migration required: {target}"
                )
            if getattr(column_type, "fsp", None) != 6:
                gaps.append((table_name, column_name))
        return tuple(gaps)

    def _migrate_decision_support_datetime_precision(self) -> None:
        if config.DB_TYPE != "mysql":
            return
        gaps = self._decision_support_datetime_precision_gaps()
        if not gaps:
            return

        connection = self.engine.connect()
        lock_acquired = False
        try:
            acquired = connection.execute(
                text("SELECT GET_LOCK(:name, :timeout)"),
                {
                    "name": self.DECISION_SUPPORT_DATETIME_LOCK,
                    "timeout": self.MYSQL_SCHEMA_LOCK_TIMEOUT,
                },
            ).scalar()
            if acquired != 1:
                raise RuntimeError(
                    "decision-support datetime migration lock acquisition failed"
                )
            lock_acquired = True
            gaps = self._decision_support_datetime_precision_gaps()
            if not gaps:
                return
            for table_name, column_name in gaps:
                connection.execute(
                    text(
                        f"ALTER TABLE `{table_name}` MODIFY `{column_name}` "
                        "DATETIME(6) NOT NULL"
                    )
                )

            remaining = self._decision_support_datetime_precision_gaps()
            if remaining:
                columns = ", ".join(
                    f"{table_name}.{column_name}"
                    for table_name, column_name in remaining
                )
                raise RuntimeError(
                    "decision-support datetimes lack microsecond precision: "
                    + columns
                )
        finally:
            active_error = sys.exc_info()[1]
            release_error = None
            try:
                if lock_acquired:
                    released = connection.execute(
                        text("SELECT RELEASE_LOCK(:name)"),
                        {"name": self.DECISION_SUPPORT_DATETIME_LOCK},
                    ).scalar()
                    if released != 1:
                        release_error = RuntimeError(
                            "decision-support datetime migration lock release failed"
                        )
            except Exception as exc:
                release_error = exc
            finally:
                connection.close()
            if release_error is not None:
                if active_error is None:
                    if isinstance(release_error, RuntimeError):
                        raise release_error
                    raise RuntimeError(
                        "decision-support datetime migration lock release failed"
                    ) from release_error
                LogUtil.error(
                    "[schema migration] decision-support datetime lock release "
                    f"failed after migration error: {release_error}"
                )

    def _alert_task_has_unique_constraint(self) -> bool:
        """检查是否已有覆盖 (market, task_name) 的唯一约束或唯一索引。"""
        inspector = inspect(self.engine)
        if "cl_alert_task" not in inspector.get_table_names():
            return False
        target = ("market", "task_name")
        constraints = inspector.get_unique_constraints("cl_alert_task")
        if any(
            tuple(item.get("column_names") or ()) == target
            for item in constraints
        ):
            return True
        return any(
            item.get("unique")
            and tuple(item.get("column_names") or ()) == target
            for item in inspector.get_indexes("cl_alert_task")
        )

    def _migrate_alert_task_unique_constraint(self) -> None:
        """幂等升级旧库：去重后为告警任务建立真实唯一索引。"""
        marker = self.cache_get(self.ALERT_TASK_UNIQUE_SCHEMA_KEY)
        if marker == {"version": 1} and self._alert_task_has_unique_constraint():
            return

        advisory_connection = None
        deduplicated_rows = 0
        try:
            if config.DB_TYPE == "mysql":
                advisory_connection = self.engine.connect()
                acquired = advisory_connection.execute(
                    text("SELECT GET_LOCK(:name, :timeout)"),
                    {"name": self.ALERT_TASK_UNIQUE_INDEX, "timeout": 30},
                ).scalar()
                if acquired != 1:
                    raise RuntimeError("获取 AlertTask schema migration lock 超时")

            if not self._alert_task_has_unique_constraint():
                try:
                    with self.engine.begin() as connection:
                        # 旧表可能已被并发 query-first 写出重复任务。保留最大 id（最后插入）
                        # 的配置；SQLite 中 DELETE + CREATE INDEX 同事务，MySQL DDL 会隐式
                        # commit，但命名 advisory lock 可确保应用实例间串行且可幂等重试。
                        delete_result = connection.execute(
                            text(
                                """
                                DELETE FROM cl_alert_task
                                WHERE id NOT IN (
                                    SELECT keep_id FROM (
                                        SELECT MAX(id) AS keep_id
                                        FROM cl_alert_task
                                        GROUP BY market, task_name
                                    ) AS alert_task_keep_rows
                                )
                                """
                            )
                        )
                        if delete_result.rowcount and delete_result.rowcount > 0:
                            deduplicated_rows = delete_result.rowcount
                        connection.execute(
                            text(
                                f"CREATE UNIQUE INDEX {self.ALERT_TASK_UNIQUE_INDEX} "
                                "ON cl_alert_task (market, task_name)"
                            )
                        )
                except Exception:
                    # 外部迁移或并发启动可能已先建好索引；重新检查，只有真实缺失才失败。
                    if not self._alert_task_has_unique_constraint():
                        raise

            if deduplicated_rows:
                LogUtil.warning(
                    f"[schema migration] cl_alert_task 去重 {deduplicated_rows} 行，"
                    "每组保留最大 id"
                )

            # marker 不是唯一真相：每次仍核验物理索引。索引已成功但 marker 写失败时，
            # 下次启动只需补 marker，不会再次删除数据或重复建索引。
            self.cache_set(self.ALERT_TASK_UNIQUE_SCHEMA_KEY, {"version": 1})
        finally:
            if advisory_connection is not None:
                try:
                    advisory_connection.execute(
                        text("SELECT RELEASE_LOCK(:name)"),
                        {"name": self.ALERT_TASK_UNIQUE_INDEX},
                    )
                finally:
                    advisory_connection.close()

    def _get_last_dt_cache(self, market: str, code: str, frequency: str):
        """加锁读 _last_dt_cache。"""
        with self._last_dt_cache_lock:
            return self._last_dt_cache.get((market, code, frequency))

    def _set_last_dt_cache(self, market: str, code: str, frequency: str, value):
        """加锁写 _last_dt_cache。"""
        with self._last_dt_cache_lock:
            key = (market, code, frequency)
            self._last_dt_cache_generation.setdefault(key, 0)
            self._last_dt_cache[key] = value

    def _get_last_dt_cache_snapshot(self, market: str, code: str, frequency: str):
        """原子读取缓存值和 generation，并登记可能在途的查询 key。"""
        with self._last_dt_cache_lock:
            key = (market, code, frequency)
            generation = self._last_dt_cache_generation.setdefault(key, 0)
            return self._last_dt_cache.get(key), generation

    def _set_last_dt_cache_if_generation(
        self,
        market: str,
        code: str,
        frequency: str,
        expected_generation: int,
        value,
    ) -> bool:
        """仅当查询期间没有提交写入时缓存结果。"""
        with self._last_dt_cache_lock:
            key = (market, code, frequency)
            if self._last_dt_cache_generation.get(key, 0) != expected_generation:
                return False
            self._last_dt_cache[key] = value
            return True

    def _last_dt_cache_generation_is_current(
        self, market: str, code: str, frequency: str, expected_generation: int
    ) -> bool:
        with self._last_dt_cache_lock:
            return (
                self._last_dt_cache_generation.get((market, code, frequency), 0)
                == expected_generation
            )

    def _invalidate_last_dt_cache(
        self, market: str, code: str, frequency: str = None
    ):
        """加锁失效指定周期；frequency 为空时清除此标的全部周期。"""
        with self._last_dt_cache_lock:
            if frequency is not None:
                key = (market, code, frequency)
                self._last_dt_cache.pop(key, None)
                self._last_dt_cache_generation[key] = (
                    self._last_dt_cache_generation.get(key, 0) + 1
                )
                return
            stale_keys = {
                key
                for key in (
                    set(self._last_dt_cache) | set(self._last_dt_cache_generation)
                )
                if key[0] == market and key[1] == code
            }
            for key in stale_keys:
                self._last_dt_cache.pop(key, None)
                self._last_dt_cache_generation[key] = (
                    self._last_dt_cache_generation.get(key, 0) + 1
                )

    def klines_tables(self, market: str, stock_code: str):
        stock_code = (
            stock_code.replace(".", "_")
            .replace("-", "_")
            .replace("/", "_")
            .replace("@", "_")
            .lower()
        )
        if market == Market.HK.value:
            table_name = f"{market}_klines_{stock_code[-3:]}"
        elif market == Market.A.value:
            table_name = f"{market}_klines_{stock_code[:7]}"
        elif market == Market.US.value:
            table_name = f"{market}_klines_{stock_code}"
        elif market == Market.FX.value:
            table_name = f"{market}_klines_{stock_code}"
        elif market == Market.CURRENCY.value:
            table_name = f"{market}_klines_{stock_code}"
        elif market == Market.CURRENCY_SPOT.value:
            table_name = f"{market}_klines_{stock_code}"
        elif market == Market.FUTURES.value:
            table_name = f"{market}_klines_{stock_code}"
        else:
            raise Exception(f"市场错误：{market}")

        if table_name in self.__cache_tables:
            return self.__cache_tables[table_name]

        # R6-#2: 无锁 check-then-act 下多线程首访同一冷表会各自执行下方 class TableByKlines
        # (向共享 Base.metadata 注册同名 Table), 第2+个线程撞 InvalidRequestError。进程锁
        # + double-check 串行化建表(镜像 exchange.get_exchange 审查 B-1)。
        with self._cache_tables_lock:
            if table_name in self.__cache_tables:
                return self.__cache_tables[table_name]

            class TableByKlines(Base):
                __tablename__ = table_name
                __table_args__ = (
                    UniqueConstraint("code", "dt", "f", name="table_code_dt_f_unique"),
                    # 频繁的 (code,f,dt) 查询与排序，建立复合索引
                    Index("idx_code_f_dt", "code", "f", "dt"),
                    {"mysql_collate": "utf8mb4_general_ci"},
                )
                code = Column(String(20), primary_key=True, comment="标的代码")
                dt = Column(DateTime, primary_key=True, comment="日期")
                f = Column(String(5), primary_key=True, comment="周期")
                o = Column(Float)
                c = Column(Float)
                h = Column(Float)
                l = Column(Float)
                v = Column(Float)
                # 注意：__table_args__ 已在上方统一声明，避免覆盖

            if market == Market.FUTURES.value:
                # 期货市场，添加持仓列
                TableByKlines.p = Column(Float, comment="持仓量")

            self.__cache_tables[table_name] = TableByKlines
            Base.metadata.create_all(self.engine)
            return TableByKlines

    def klines_query(
        self,
        market: str,
        code: str,
        frequency: str,
        start_date: datetime.datetime = None,
        end_date: datetime.datetime = None,
        limit: int = 5000,
        order: str = "desc",
        auto_reverse: bool = False,
    ) -> List:
        """
        获取k线数据。

        ⚠️ 注意返回方向：
        - ``order='desc'``（默认）+ ``limit`` 是为了"取最近 N 根"而设计的，
          因此返回结果是 **按 dt 降序** 的（最新的在 [0] 位置）。
        - 缠论计算 / 大多数业务消费方期望的是 **升序**（最早的在 [0] 位置）。
        - 调用方如果需要升序，可以：
            a) 显式传 ``order='asc'``（但当 limit 生效时会取到"最早 N 根"，语义不同）；
            b) 传 ``auto_reverse=True``：保持 desc+limit 的"取最近 N 根"语义，
               但在返回前自动反转为升序，同时兼顾"最近 N 根 + 升序"两个诉求。

        :param market:
        :param code:
        :param frequency:
        :param start_date:
        :param end_date:
        :param limit:
        :param order: ``'desc'`` 或 ``'asc'``。
        :param auto_reverse: 仅在 ``order='desc'`` 时生效，将结果反转为升序返回。
        """
        with self.Session() as session:
            table = self.klines_tables(market, code)
            filter = (table.code == code, table.f == frequency)
            if start_date is not None:
                filter += (table.dt >= start_date,)
            if end_date is not None:
                filter += (table.dt <= end_date,)
            query = session.query(table).filter(*filter)
            if order == "desc":
                query = query.order_by(table.dt.desc())
            else:
                query = query.order_by(table.dt.asc())
            if limit is not None:
                query = query.limit(limit)
            rows = query.all()
            if auto_reverse and order == "desc":
                rows = list(reversed(rows))
            return rows

    def klines_last_datetime(self, market, code, frequency):
        """查询指定标的 K 线表中最新一条记录的日期字符串，无数据时返回 None。"""
        while True:
            cached, generation = self._get_last_dt_cache_snapshot(
                market, code, frequency
            )
            if cached is not None:
                return cached

            with self.Session() as session:
                table = self.klines_tables(market, code)
                last_date = (
                    session.query(table.dt)
                    .filter(table.code == code)
                    .filter(table.f == frequency)
                    .order_by(table.dt.desc())
                    .first()
                )
                if last_date is None:
                    if self._last_dt_cache_generation_is_current(
                        market, code, frequency, generation
                    ):
                        return None
                    continue
                if market == "a":
                    result = last_date[0].strftime("%Y-%m-%d")
                else:
                    result = last_date[0].strftime("%Y-%m-%d %H:%M:%S")

            if self._set_last_dt_cache_if_generation(
                market, code, frequency, generation, result
            ):
                return result

    # MySQL upsert 的批大小：默认 1000，受 max_allowed_packet 限制建议 500~5000。
    # 之前直接调到 20000 在大列宽 K 线表上很容易触发 "MySQL server has gone away"。
    KLINES_INSERT_BATCH_SIZE = 1000

    def klines_insert(
        self, market: str, code: str, frequency: str, klines: pd.DataFrame
    ):
        """
        插入k线 (性能优化版)
        """
        if klines.empty:
            return True

        # 1. 数据预处理 (Pandas 向量化操作，替代 iterrows)
        df = klines.copy()

        # 统一处理时间：去除时区信息。鲁棒化处理：
        # - 列已经是 tz-aware datetime 时，做 tz_localize(None)
        # - 列是 tz-naive datetime 时，原样返回
        # - 列是 object/混合类型时，用 pd.to_datetime 统一标准化
        date_col = df["date"]
        if not pd.api.types.is_datetime64_any_dtype(date_col):
            date_col = pd.to_datetime(date_col, errors="coerce", utc=False)
        if getattr(date_col.dt, "tz", None) is not None:
            date_col = date_col.dt.tz_localize(None)
        df["dt"] = date_col
        # 异常 date 值不能写入 DB（主键），直接抛出让调用方处理。
        if df["dt"].isna().any():
            raise ValueError(
                f"klines_insert({market},{code},{frequency}) 中存在无法解析的 date 值"
            )

        rename_map = {
            "open": "o",
            "close": "c",
            "high": "h",
            "low": "l",
            "volume": "v",
            "position": "p",
        }
        df.rename(columns=rename_map, inplace=True)

        df["code"] = code
        df["f"] = frequency

        db_columns = ["code", "dt", "f", "o", "c", "h", "l", "v"]
        if "p" in df.columns:
            db_columns.append("p")

        # 只保留实际存在的列，防止上游未传 position 时出现 KeyError
        final_columns = [col for col in db_columns if col in df.columns]
        data_to_insert = df[final_columns].to_dict(orient="records")

        with self.Session() as session:
            table = self.klines_tables(market, code)

            # SQLite 也用方言级原子 upsert(on_conflict_do_update),与下方 MySQL 分支对齐:
            # 原"逐行 query→add/update"在多 worker(default 池 10 线程)并发写同一 (code,dt,f)
            # 时,两线程都 query 到 None → 都 add → 第二个 commit 撞 UniqueConstraint → 整批
            # 回滚失败(审查 F4)。on_conflict 按唯一键 (code,dt,f) 原子"插入或更新",无竞态。
            if config.DB_TYPE == "sqlite":
                from sqlalchemy.dialects.sqlite import insert as sqlite_insert

                batch_size = self.KLINES_INSERT_BATCH_SIZE
                try:
                    for i in range(0, len(data_to_insert), batch_size):
                        batch = data_to_insert[i : i + batch_size]
                        insert_stmt = sqlite_insert(table).values(batch)
                        # 主键/唯一键列不参与 update(与 MySQL 分支同口径)
                        update_columns = {
                            x.name: x
                            for x in insert_stmt.excluded
                            if x.name not in ("code", "dt", "f")
                        }
                        upsert_stmt = insert_stmt.on_conflict_do_update(
                            index_elements=["code", "dt", "f"],
                            set_=update_columns,
                        )
                        session.execute(upsert_stmt)
                    session.commit()
                    # 必须在 commit 返回后失效；否则并发读可在提交窗口把旧值重新写回缓存。
                    self._invalidate_last_dt_cache(market, code, frequency)
                except Exception as e:
                    session.rollback()
                    LogUtil.error(f"SQLite Batch Upsert Error: {e}")
                    raise
                return True

            # MySQL 批量 upsert
            batch_size = self.KLINES_INSERT_BATCH_SIZE
            try:
                for i in range(0, len(data_to_insert), batch_size):
                    batch = data_to_insert[i : i + batch_size]
                    insert_stmt = insert(table).values(batch)
                    # 主键/唯一索引列不参与 update
                    update_columns = {
                        x.name: x
                        for x in insert_stmt.inserted
                        if x.name not in ("code", "dt", "f")
                    }
                    upsert_stmt = insert_stmt.on_duplicate_key_update(**update_columns)
                    session.execute(upsert_stmt)
                session.commit()
            except Exception as e:
                session.rollback()
                LogUtil.error(f"Batch Insert Error: {e}")
                raise

        # MySQL 分支同样只在提交成功后失效；失败回滚时保留原缓存仍与数据库一致。
        self._invalidate_last_dt_cache(market, code, frequency)
        return True

    def klines_delete(
        self,
        market: str,
        code: str,
        frequency: str = None,
        dt: datetime.datetime = None,
    ):
        """删除 K 线记录；frequency/dt 为空时删除该标的全部数据。"""
        with self.Session() as session:
            try:
                table = self.klines_tables(market, code)
                q = session.query(table).filter(table.code == code)
                if frequency is not None:
                    q = q.filter(table.f == frequency)
                if dt is not None:
                    q = q.filter(table.dt == dt)
                q.delete()
                session.commit()
            except Exception:
                session.rollback()
                raise

        # commit 成功后再失效；frequency=None 表示本标的全部周期。
        self._invalidate_last_dt_cache(market, code, frequency)
        return True

    def zx_get_groups(self, market: str) -> List[TableByZxGroup]:
        """
        获取自选分组
        """
        with self.Session() as session:
            return (
                session.query(TableByZxGroup)
                .filter(TableByZxGroup.market == market)
                .order_by(TableByZxGroup.add_dt.asc())
                .all()
            )

    def zx_add_group(self, market: str, zx_group: str) -> bool:
        """
        添加自选分组
        """
        with self.Session() as session:
            try:
                session.add(
                    TableByZxGroup(
                        market=market, zx_group=zx_group, add_dt=datetime.datetime.now()
                    )
                )
                session.commit()
            except Exception:
                session.rollback()
                raise

        return True

    def zx_del_group(self, market: str, zx_group: str) -> bool:
        """
        删除自选分组
        """
        with self.Session() as session:
            try:
                session.query(TableByZxGroup).filter(
                    TableByZxGroup.market == market, TableByZxGroup.zx_group == zx_group
                ).delete()
                session.commit()
            except Exception:
                session.rollback()
                raise

        return True

    def zx_get_group_stocks(self, market: str, zx_group: str) -> List[TableByZixuan]:
        """
        获取自选组下的股票列表
        """
        with self.Session() as session:
            stocks = (
                session.query(TableByZixuan)
                .filter(TableByZixuan.zx_group == zx_group)
                .filter(TableByZixuan.market == market)
                .order_by(TableByZixuan.position.asc())
                .all()
            )
        return stocks

    def zx_add_group_stock(
        self,
        market: str,
        zx_group: str,
        stock_code: str,
        stock_name: str,
        memo: str = "",
        color: str = "",
        location: str = "bottom",
    ):
        with self.Session() as session:
            try:
                # 先删后插实现幂等：避免重复添加同一标的
                session.query(TableByZixuan).filter(
                    TableByZixuan.market == market,
                    TableByZixuan.zx_group == zx_group,
                    TableByZixuan.stock_code == stock_code,
                ).delete()

                position = 0
                if location == "top":
                    # 自选组的股票位置+1
                    session.query(TableByZixuan).filter(
                        TableByZixuan.market == market,
                        TableByZixuan.zx_group == zx_group
                    ).update(
                        {TableByZixuan.position: TableByZixuan.position + 1},
                        synchronize_session=False,
                    )
                else:
                    # 获取自选组的 position 最大值
                    max_position = (
                        session.query(func.max(TableByZixuan.position))
                        .filter(TableByZixuan.market == market)
                        .filter(TableByZixuan.zx_group == zx_group)
                        .scalar()
                    )
                    position = max_position + 1 if max_position is not None else 0
                zx_stock = TableByZixuan(
                    market=market,
                    zx_group=zx_group,
                    stock_code=stock_code,
                    stock_name=stock_name,
                    stock_color=color,
                    position=position,
                    stock_memo=memo,
                    add_datetime=datetime.datetime.now(),
                )
                session.add(zx_stock)
                session.commit()
            except Exception:
                session.rollback()
                raise

        return True

    def zx_del_group_stock(self, market: str, zx_group: str, stock_code: str):
        with self.Session() as session:
            try:
                session.query(TableByZixuan).filter(TableByZixuan.market == market).filter(
                    TableByZixuan.zx_group == zx_group
                ).filter(TableByZixuan.stock_code == stock_code).delete()
                session.commit()
            except Exception:
                session.rollback()
                raise

        return True

    def zx_update_stock_color(
        self, market: str, zx_group: str, stock_code: str, color: str
    ):
        with self.Session() as session:
            try:
                session.query(TableByZixuan).filter(TableByZixuan.market == market).filter(
                    TableByZixuan.zx_group == zx_group
                ).filter(TableByZixuan.stock_code == stock_code).update(
                    {"stock_color": color}, synchronize_session=False
                )
                session.commit()
            except Exception:
                session.rollback()
                raise

        return True

    def zx_update_stock_name(
        self, market: str, zx_group: str, stock_code: str, stock_name: str
    ):
        with self.Session() as session:
            try:
                session.query(TableByZixuan).filter(TableByZixuan.market == market).filter(
                    TableByZixuan.zx_group == zx_group
                ).filter(TableByZixuan.stock_code == stock_code).update(
                    {"stock_name": stock_name}, synchronize_session=False
                )
                session.commit()
            except Exception:
                session.rollback()
                raise

        return True

    def zx_stock_sort_top(self, market: str, zx_group: str, stock_code: str):
        with self.Session() as session:
            try:
                session.query(TableByZixuan).filter(TableByZixuan.market == market).filter(
                    TableByZixuan.zx_group == zx_group
                ).update(
                    {"position": TableByZixuan.position + 1}, synchronize_session=False
                )
                session.query(TableByZixuan).filter(TableByZixuan.market == market).filter(
                    TableByZixuan.zx_group == zx_group
                ).filter(TableByZixuan.stock_code == stock_code).update(
                    {"position": 0}, synchronize_session=False
                )
                session.commit()
            except Exception:
                session.rollback()
                raise

        return True

    def zx_stock_sort_bottom(self, market: str, zx_group: str, stock_code: str):
        with self.Session() as session:
            try:
                max_position = (
                    session.query(func.max(TableByZixuan.position))
                    .filter(TableByZixuan.market == market)
                    .filter(TableByZixuan.zx_group == zx_group)
                    .scalar()
                )
                session.query(TableByZixuan).filter(TableByZixuan.market == market).filter(
                    TableByZixuan.zx_group == zx_group
                ).filter(TableByZixuan.stock_code == stock_code).update(
                    {"position": (max_position or 0) + 1},  # R15-C3: 空组 MAX=None 守零
                    synchronize_session=False,
                )
                session.commit()
            except Exception:
                session.rollback()
                raise

        return True

    def zx_clear_by_group(self, market: str, zx_group: str):
        with self.Session() as session:
            try:
                session.query(TableByZixuan).filter(TableByZixuan.market == market).filter(
                    TableByZixuan.zx_group == zx_group
                ).delete(synchronize_session=False)
                session.commit()
            except Exception:
                session.rollback()
                raise

        return True

    def zx_replace_group_stocks(
        self, market: str, zx_group: str, stocks: List[dict]
    ) -> bool:
        """在单个事务中用完整快照替换自选组内容。"""
        with self.Session() as session:
            try:
                session.query(TableByZixuan).filter(
                    TableByZixuan.market == market,
                    TableByZixuan.zx_group == zx_group,
                ).delete(synchronize_session=False)
                now = datetime.datetime.now()
                for position, stock in enumerate(stocks):
                    code = stock["code"]
                    session.add(
                        TableByZixuan(
                            market=market,
                            zx_group=zx_group,
                            stock_code=code,
                            stock_name=stock.get("name") or code,
                            position=position,
                            add_datetime=now,
                            stock_color=stock.get("color", ""),
                            stock_memo=stock.get("memo", ""),
                        )
                    )
                session.commit()
            except Exception:
                session.rollback()
                raise
        return True

    def zx_query_group_by_code(self, market: str, stock_code: str) -> List[str]:
        """查询指定标的所在的所有自选分组名称列表。"""
        with self.Session() as session:
            return [
                _[0]
                for _ in (
                    session.query(TableByZixuan.zx_group)
                    .filter(TableByZixuan.market == market)
                    .filter(TableByZixuan.stock_code == stock_code)
                    .distinct()
                    .all()
                )
            ]

    def order_save(
        self,
        market: str,
        stock_code: str,
        stock_name: str,
        order_type: str,
        order_price: float,
        order_amount: float,
        order_memo: str,
        order_time: Union[str, datetime.datetime],
    ):
        with self.Session() as session:
            try:
                order = TableByOrder(
                    market=market,
                    stock_code=stock_code,
                    stock_name=stock_name,
                    order_type=order_type,
                    order_price=order_price,
                    order_amount=order_amount,
                    order_memo=order_memo,
                    dt=order_time,
                )
                session.add(order)
                session.commit()
            except Exception:
                session.rollback()
                raise

        return True

    def order_query_by_code(self, market: str, stock_code: str) -> List[TableByOrder]:
        with self.Session() as session:
            orders = (
                session.query(TableByOrder)
                .filter(TableByOrder.market == market)
                .filter(TableByOrder.stock_code == stock_code)
                .all()
            )

        # 返回与历史接口兼容的字典格式
        return [
            {
                "code": _o.stock_code,
                "name": _o.stock_name,
                "datetime": _o.dt,
                "type": _o.order_type,
                "price": _o.order_price,
                "amount": _o.order_amount,
                "info": _o.order_memo,
            }
            for _o in orders
        ]

    def order_clear_by_code(self, market: str, stock_code: str):
        with self.Session() as session:
            try:
                session.query(TableByOrder).filter(TableByOrder.market == market).filter(
                    TableByOrder.stock_code == stock_code
                ).delete()
                session.commit()
            except Exception:
                session.rollback()
                raise

        return True

    def task_save(
        self,
        market: str,
        task_name: str,
        zx_group: str,
        frequency: str,
        interval_minutes: int,
        check_bi_type: str,
        check_bi_beichi: str,
        check_bi_mmd: str,
        check_xd_type: str,
        check_xd_beichi: str,
        check_xd_mmd: str,
        check_idx_ma_info: str,
        check_idx_macd_info: str,
        is_run: int,
        is_send_msg: int,
    ):
        with self.Session() as session:
            try:
                session.add(
                    TableByAlertTask(
                        market=market,
                        task_name=task_name,
                        zx_group=zx_group,
                        frequency=frequency,
                        interval_minutes=interval_minutes,
                        check_bi_type=check_bi_type,
                        check_bi_beichi=check_bi_beichi,
                        check_bi_mmd=check_bi_mmd,
                        check_xd_type=check_xd_type,
                        check_xd_beichi=check_xd_beichi,
                        check_xd_mmd=check_xd_mmd,
                        check_idx_ma_info=check_idx_ma_info,
                        check_idx_macd_info=check_idx_macd_info,
                        is_run=is_run,
                        is_send_msg=is_send_msg,
                        dt=datetime.datetime.now(),
                    )
                )
                session.commit()
            except Exception:
                session.rollback()
                raise

        return True

    def task_query(self, market: str = None, id: int = None) -> List[TableByAlertTask]:
        """
        查询任务列表。

        签名为兼容历史调用方仍然返回 ``List``。

        约束：
        - 当传 ``id`` 时，``id`` 是主键，理论上只会有 0 或 1 条；
          ``.limit(1)`` 防御脏数据（极端情况存在多条同 id），
          避免上层静默丢弃多余记录。
        """
        with self.Session() as session:
            query = session.query(TableByAlertTask)
            filter = ()
            if market is not None:
                filter += (TableByAlertTask.market == market,)
            if id is not None:
                filter += (TableByAlertTask.id == id,)
            if len(filter) > 0:
                query = query.filter(*filter)
            if id is not None:
                # 主键查询只可能命中 1 条；显式 limit(1) 既加快查询，也防御脏数据。
                query = query.limit(1)
            return query.all()

    def task_delete(self, id: int):
        with self.Session() as session:
            try:
                session.query(TableByAlertTask).filter(TableByAlertTask.id == id).delete()
                session.commit()
            except Exception:
                session.rollback()
                raise

        return True

    def task_update(
        self,
        id: int,
        market: str,
        task_name: str,
        zx_group: str,
        frequency: str,
        interval_minutes: int,
        check_bi_type: str,
        check_bi_beichi: str,
        check_bi_mmd: str,
        check_xd_type: str,
        check_xd_beichi: str,
        check_xd_mmd: str,
        check_idx_ma_info: str,
        check_idx_macd_info: str,
        is_run: int,
        is_send_msg: int,
    ):
        with self.Session() as session:
            try:
                session.query(TableByAlertTask).filter(
                    TableByAlertTask.market == market,
                    TableByAlertTask.id == id,
                ).update(
                    {
                        TableByAlertTask.task_name: task_name,
                        TableByAlertTask.zx_group: zx_group,
                        TableByAlertTask.frequency: frequency,
                        TableByAlertTask.interval_minutes: interval_minutes,
                        TableByAlertTask.check_bi_type: check_bi_type,
                        TableByAlertTask.check_bi_beichi: check_bi_beichi,
                        TableByAlertTask.check_bi_mmd: check_bi_mmd,
                        TableByAlertTask.check_xd_type: check_xd_type,
                        TableByAlertTask.check_xd_beichi: check_xd_beichi,
                        TableByAlertTask.check_xd_mmd: check_xd_mmd,
                        TableByAlertTask.check_idx_ma_info: check_idx_ma_info,
                        TableByAlertTask.check_idx_macd_info: check_idx_macd_info,
                        TableByAlertTask.is_run: is_run,
                        TableByAlertTask.is_send_msg: is_send_msg,
                        TableByAlertTask.dt: datetime.datetime.now(),
                    }
                )
                session.commit()
            except Exception:
                session.rollback()
                raise
        return True

    def alert_record_save(
        self,
        market: str,
        task_name: str,
        stock_code: str,
        stock_name: str,
        frequency: str,
        alert_msg: str,
        bi_is_done: str,
        bi_is_td: str,
        line_type: str,
        line_dt: datetime.datetime,
    ):
        """
        保存预警记录
        :param market:
        :param stock_code:
        :param stock_name:
        :param frequency:
        :param alert_msg:
        :param bi_is_down:
        :param bi_is_td:
        :param line_dt:
        :return:
        """
        with self.Session() as session:
            try:
                recored = TableByAlertRecord(
                    market=market,
                    task_name=task_name,
                    stock_code=stock_code,
                    stock_name=stock_name,
                    frequency=frequency,
                    alert_msg=alert_msg,
                    bi_is_done=bi_is_done,
                    bi_is_td=bi_is_td,
                    line_type=line_type,
                    line_dt=line_dt.replace(tzinfo=None),
                    alert_dt=datetime.datetime.now(),
                )
                session.add(recored)
                session.commit()
            except Exception:
                session.rollback()
                raise

        return True

    def alert_record_query_by_code(
        self,
        market: str,
        stock_code: str,
        frequency: str,
        line_type: str,
        line_dt: datetime.datetime,
    ) -> TableByAlertRecord:
        """查询指定标的、周期、线类型和线起点时间的最新预警记录。"""
        with self.Session() as session:
            return (
                session.query(TableByAlertRecord)
                .filter(
                    TableByAlertRecord.market == market,
                    TableByAlertRecord.stock_code == stock_code,
                    TableByAlertRecord.frequency == frequency,
                    TableByAlertRecord.line_type == line_type,
                    TableByAlertRecord.line_dt == line_dt,
                )
                .order_by(TableByAlertRecord.alert_dt.desc())
                .first()
            )

    def alert_record_query(
        self, market: str, task_name: str = None
    ) -> List[TableByAlertRecord]:
        """查询预警记录列表（最近 100 条，降序），可按 task_name 过滤。"""
        with self.Session() as session:
            query = session.query(TableByAlertRecord)
            query = query.filter(TableByAlertRecord.market == market)
            if task_name:
                query = query.filter(TableByAlertRecord.task_name == task_name)
            return query.order_by(TableByAlertRecord.alert_dt.desc()).limit(100).all()

    def marks_add(
        self,
        market: str,
        stock_code: str,
        stock_name: str,
        frequency: str,
        mark_time: int,
        mark_label: str,
        mark_tooltip: str,
        mark_shape: str,
        mark_color: str,
    ):
        """
        添加代码在 tv 时间轴显示的信息
        :param market:
        :param stock_code:
        :param stock_name:
        :param frequency:   需要在什么周期显示，默认 ‘’，所有周期，可以是 'd', '30m', '5m' 这样之下指定周期下展示
        :param mark_time:   int 时间戳
        :param mark_label:  时间刻度标记的标签，英文字母，最大 两位
        :param mark_tooltip:    工具提示内容
        :param mark_shape:  "circle" | "earningUp" | "earningDown" | "earning" 形状
        :param mark_color: 颜色 rgb，比如 'red'  '#FF0000'
        :return:
        """
        with self.Session() as session:
            try:
                # 同 (market, code, mark_time, mark_label) 只保留一条，先删后插
                session.query(TableByTVMarks).filter(
                    TableByTVMarks.market == market,
                    TableByTVMarks.stock_code == stock_code,
                    TableByTVMarks.mark_time == mark_time,
                    TableByTVMarks.mark_label == mark_label,
                ).delete()

                mark = TableByTVMarks(
                    market=market,
                    stock_code=stock_code,
                    stock_name=stock_name,
                    frequency=frequency,
                    mark_time=mark_time,
                    mark_label=mark_label,
                    mark_tooltip=mark_tooltip,
                    mark_shape=mark_shape,
                    mark_color=mark_color,
                    dt=datetime.datetime.now(),
                )
                session.add(mark)
                session.commit()
            except Exception:
                session.rollback()
                raise

        return True

    def marks_query(
        self, market: str, stock_code: str, start_date: int = None
    ) -> List[TableByTVMarks]:
        """查询时间轴图表标记列表，start_date（时间戳）可限制查询起点。"""
        with self.Session() as session:
            query = session.query(TableByTVMarks).filter(
                TableByTVMarks.market == market,
                TableByTVMarks.stock_code == stock_code,
            )
            if start_date is not None:
                query = query.filter(TableByTVMarks.mark_time >= start_date)

            return query.order_by(TableByTVMarks.mark_time.asc()).all()

    def marks_del(self, market: str, mark_label: str):
        with self.Session() as session:
            try:
                session.query(TableByTVMarks).filter(
                    TableByTVMarks.market == market, TableByTVMarks.mark_label == mark_label
                ).delete()
                session.commit()
            except Exception:
                session.rollback()
                raise

        return True

    def marks_add_by_price(
        self,
        market: str,
        stock_code: str,
        stock_name: str,
        frequency: str,
        mark_time: int,
        mark_label: str,
        mark_text: str,
        mark_label_color: str,
        mark_color: str,
    ):
        """
        添加代码在 tv 价格主图显示的信息
        """
        with self.Session() as session:
            try:
                # 同 (market, code, mark_time, mark_label) 只保留一条，先删后插
                session.query(TableByTVMarksPrice).filter(
                    TableByTVMarksPrice.market == market,
                    TableByTVMarksPrice.stock_code == stock_code,
                    TableByTVMarksPrice.mark_time == mark_time,
                    TableByTVMarksPrice.mark_label == mark_label,
                ).delete()

                mark = TableByTVMarksPrice(
                    market=market,
                    stock_code=stock_code,
                    stock_name=stock_name,
                    frequency=frequency,
                    mark_time=mark_time,
                    mark_color=mark_color,
                    mark_text=mark_text,
                    mark_label=mark_label,
                    mark_label_font_color=mark_label_color,
                    mark_min_size=1,
                    dt=datetime.datetime.now(),
                )
                session.add(mark)
                session.commit()
            except Exception:
                session.rollback()
                raise

        return True

    def marks_query_by_price(
        self, market: str, stock_code: str, start_date: int = None
    ) -> List[TableByTVMarksPrice]:
        """查询价格主图标记列表，start_date（时间戳）可限制查询起点。"""
        with self.Session() as session:
            query = session.query(TableByTVMarksPrice).filter(
                TableByTVMarksPrice.market == market,
                TableByTVMarksPrice.stock_code == stock_code,
            )
            if start_date is not None:
                query = query.filter(TableByTVMarksPrice.mark_time >= start_date)
            return query.order_by(TableByTVMarksPrice.mark_time.asc()).all()

    def marks_del_by_price(self, market: str, mark_label: str):
        with self.Session() as session:
            try:
                # 原代码混用了 TableByTVMarks.market，实际应删 TableByTVMarksPrice，统一修正。
                session.query(TableByTVMarksPrice).filter(
                    TableByTVMarksPrice.market == market,
                    TableByTVMarksPrice.mark_label == mark_label,
                ).delete()
                session.commit()
            except Exception:
                session.rollback()
                raise

        return True

    def marks_del_all_by_code(self, market: str, code: str):
        """
        删除代码的所有标记
        """
        with self.Session() as session:
            try:
                session.query(TableByTVMarks).filter(
                    TableByTVMarks.market == market,
                    TableByTVMarks.stock_code == code,
                ).delete()
                session.query(TableByTVMarksPrice).filter(
                    TableByTVMarksPrice.market == market,
                    TableByTVMarksPrice.stock_code == code,
                ).delete()
                session.commit()
            except Exception:
                session.rollback()
                raise
        return True

    def tv_chart_list(self, chart_type, client_id, user_id):
        with self.Session() as session:
            return (
                session.query(TableByTVCharts)
                .filter(
                    TableByTVCharts.chart_type == chart_type,
                    TableByTVCharts.client_id == client_id,
                    TableByTVCharts.user_id == user_id,
                )
                .all()
            )

    def tv_chart_save(
        self, chart_type, client_id, user_id, name, content, symbol, resolution
    ):
        """保存图表布局，返回记录 id；drawing/study_template 类型按名称做覆盖更新。"""
        with self.Session() as session:
            # drawing/study_template 按名称覆盖，避免同名模板重复堆积
            if chart_type in ["drawing", "study_template", "template"]:
                chart = (
                    session.query(TableByTVCharts)
                    .filter(
                        TableByTVCharts.name == name,
                        TableByTVCharts.chart_type == chart_type,
                        TableByTVCharts.client_id == client_id,
                        TableByTVCharts.user_id == user_id,
                    )
                    .first()
                )
                if chart:
                    chart.content = content
                    chart.symbol = symbol
                    chart.resolution = resolution
                    chart.timestamp = int(time.time())
                    session.commit()
                    return chart.id

            chart = TableByTVCharts(
                chart_type=chart_type,
                client_id=client_id,
                user_id=user_id,
                name=name,
                content=content,
                symbol=symbol,
                resolution=resolution,
                timestamp=int(time.time()),
            )
            session.add(chart)
            session.commit()
            return chart.id


    def tv_chart_update(
        self, chart_type, id, client_id, user_id, name, content, symbol, resolution
    ):
        with self.Session() as session:
            session.query(TableByTVCharts).filter(
                TableByTVCharts.id == id,
                TableByTVCharts.client_id == client_id,
                TableByTVCharts.user_id == user_id,
                TableByTVCharts.chart_type == chart_type,
            ).update(
                {
                    TableByTVCharts.name: name,
                    TableByTVCharts.content: content,
                    TableByTVCharts.symbol: symbol,
                    TableByTVCharts.resolution: resolution,
                    TableByTVCharts.timestamp: int(time.time()),
                }
            )
            session.commit()
        return True

    def tv_chart_get(self, chart_type, id, client_id, user_id):
        with self.Session() as session:
            return (
                session.query(TableByTVCharts)
                .filter(
                    TableByTVCharts.id == id,
                    TableByTVCharts.chart_type == chart_type,
                    TableByTVCharts.client_id == client_id,
                    TableByTVCharts.user_id == user_id,
                )
                .first()
            )

    def tv_chart_get_by_name(self, chart_type, name, client_id, user_id):
        with self.Session() as session:
            return (
                session.query(TableByTVCharts)
                .filter(
                    TableByTVCharts.name == name,
                    TableByTVCharts.chart_type == chart_type,
                    TableByTVCharts.client_id == client_id,
                    TableByTVCharts.user_id == user_id,
                )
                .order_by(TableByTVCharts.timestamp.desc())
                .first()
            )

    def tv_chart_del(self, chart_type, id, client_id, user_id):
        with self.Session() as session:
            try:
                session.query(TableByTVCharts).filter(
                    TableByTVCharts.id == id,
                    TableByTVCharts.chart_type == chart_type,
                    TableByTVCharts.client_id == client_id,
                    TableByTVCharts.user_id == user_id,
                ).delete()
                session.commit()
            except Exception:
                session.rollback()
                raise
        return True

    def tv_chart_del_by_name(self, chart_type, name, client_id, user_id):
        with self.Session() as session:
            try:
                session.query(TableByTVCharts).filter(
                    TableByTVCharts.name == name,
                    TableByTVCharts.chart_type == chart_type,
                    TableByTVCharts.client_id == client_id,
                    TableByTVCharts.user_id == user_id,
                ).delete()
                session.commit()
            except Exception:
                session.rollback()
                raise
        return True

    # cache_get 过期清理节流：避免每次读 cache 都扫全表 delete，造成写放大。
    # 同进程内每 _CACHE_GC_INTERVAL_SEC 秒最多触发一次过期清理；
    # 用类属性 + 简单时间戳即可，不需要锁（多次执行也只是重复 delete，幂等无害）。
    _CACHE_GC_INTERVAL_SEC = 300
    _last_cache_gc_at = 0.0

    def cache_get(self, key: str):
        for attempt in range(2):
            try:
                with self.Session() as session:
                    now = int(time.time())
                    cache = (
                        session.query(TableByCache)
                        .filter(TableByCache.k == key)
                        .first()
                    )
                    if cache and (cache.expire == 0 or cache.expire > now):
                        return json.loads(cache.v)

                    # 过期清理节流：只有距离上次清理超过 _CACHE_GC_INTERVAL_SEC 秒才执行；
                    # 并且 delete + commit 必须自己包 try/except + rollback，
                    # 失败时不能让外层吞掉（外层 except 会把它当成可重试的连接异常）。
                    if (now - DB._last_cache_gc_at) >= self._CACHE_GC_INTERVAL_SEC:
                        try:
                            session.query(TableByCache).filter(
                                TableByCache.expire != 0,
                                TableByCache.expire < now,
                            ).delete()
                            session.commit()
                            DB._last_cache_gc_at = now
                        except Exception as gc_exc:
                            session.rollback()
                            LogUtil.warning(
                                f"[db.cache_get] gc expired cache failed: {gc_exc}"
                            )
                            # 仍然刷新时间戳，避免坏 case 下连续重试拖慢主流程。
                            DB._last_cache_gc_at = now
                return None
            except Exception as e:
                err = str(e)
                retryable = (
                    "Packet sequence number wrong" in err
                    or "MySQL server has gone away" in err
                    or "Lost connection to MySQL server" in err
                    or "server has gone away" in err
                )
                if attempt == 0 and retryable:
                    LogUtil.warning(
                        f"[db.cache_get] retry key={key} because db connection error: {e}"
                    )
                    # 注意：原实现这里调用了 self.engine.dispose()，
                    # 它会一次性关闭整个连接池里的所有连接，让其他线程下一次取连接
                    # 时全都要重建 —— 在缓存失效高频场景下容易引发雪崩。
                    # SQLAlchemy 的 pool_pre_ping=True（见 __init__）已经能在每次取连接时
                    # 检测并替换掉单条失效连接，足够应对 "MySQL gone away" / "Lost connection"，
                    # 因此这里不需要再 dispose 整个池。
                    time.sleep(0.05)
                    continue
                LogUtil.error(f"[db.cache_get] failed key={key}: {e}", exc_info=True)
                return None
        return None

    def cache_set_many(self, values: dict, expire: int = 0):
        """Atomically upsert multiple cache keys in a single transaction."""
        if not values:
            return True

        # Serialize every value before opening the transaction. A malformed value
        # must not leave an earlier key committed while a later key fails.
        payloads = [
            {"k": key, "v": json.dumps(val), "expire": expire}
            for key, val in values.items()
        ]
        with self.Session() as session:
            try:
                if config.DB_TYPE == "sqlite":
                    from sqlalchemy.dialects.sqlite import insert as sqlite_insert

                    stmt = sqlite_insert(TableByCache).values(payloads)
                    stmt = stmt.on_conflict_do_update(
                        index_elements=["k"],
                        set_={"v": stmt.excluded.v, "expire": stmt.excluded.expire},
                    )
                else:
                    stmt = insert(TableByCache).values(payloads)
                    stmt = stmt.on_duplicate_key_update(
                        v=stmt.inserted.v, expire=stmt.inserted.expire
                    )
                session.execute(stmt)
                session.commit()
            except Exception:
                session.rollback()
                raise

        return True

    def cache_set(self, key: str, val: dict, expire: int = 0):
        # Keep single-key writes on the same dialect-specific upsert path as
        # multi-key settings writes so their transaction behavior cannot drift.
        return self.cache_set_many({key: val}, expire=expire)

    def cache_del(self, key: str):
        with self.Session() as session:
            try:
                session.query(TableByCache).filter(TableByCache.k == key).delete()
                session.commit()
            except Exception:
                session.rollback()
                raise

        return True


class _LazyDB:
    """Thread-safe proxy that defers database creation until first real use."""

    def __init__(self) -> None:
        object.__setattr__(self, "_instance", None)
        object.__setattr__(self, "_lock", threading.Lock())

    def is_initialized(self) -> bool:
        return object.__getattribute__(self, "_instance") is not None

    def _get_instance(self):
        instance = object.__getattribute__(self, "_instance")
        if instance is None:
            lock = object.__getattribute__(self, "_lock")
            with lock:
                instance = object.__getattribute__(self, "_instance")
                if instance is None:
                    instance = DB()
                    object.__setattr__(self, "_instance", instance)
        return instance

    def __getattr__(self, name):
        return getattr(self._get_instance(), name)

    def __setattr__(self, name, value) -> None:
        if name in {"_instance", "_lock"}:
            object.__setattr__(self, name, value)
            return
        setattr(self._get_instance(), name, value)


db = _LazyDB()

if __name__ == "__main__":
    db = DB()

    db.marks_add_by_price(
        "a",
        "SH.600378",
        "昊华科技",
        "30m",
        fun.str_to_timeint("2025-07-03 14:00:00"),
        "A",
        "测试标记2",
        "green",
        "red",
    )

