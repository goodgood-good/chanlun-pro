import datetime
import json
import os
import pathlib
import threading
import time
import warnings
from typing import List

import pandas as pd
from sqlalchemy import (
    Column,
    DateTime,
    Float,
    String,
    UniqueConstraint,
    event,
    Index,
    create_engine,
    func,
    inspect,
)
from sqlalchemy.dialects.mysql import LONGTEXT as MySQLLongText, insert
from sqlalchemy.engine import URL
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import QueuePool

from chanlun import config, fun
from chanlun.market import Market
from chanlun.config import get_data_path
from chanlun.tools.log_util import LogUtil

warnings.filterwarnings("ignore")

# SQLAlchemy 类型参考：https://docs.sqlalchemy.org/en/20/core/types.html

from chanlun.db_models.base import Base
from chanlun.db_models.cache import TableByCache
from chanlun.db_models.tv_charts import TableByTVCharts, TV_CHART_NAME_MAX_LENGTH
from chanlun.db_models.zixuan import TableByZixuan
from chanlun.db_models.zixuan_group import TableByZxGroup

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

    if (
        config.DB_TYPE != "sqlite"
        or expected_path is None
        or configured_path != expected_path
    ):
        raise RuntimeError(
            "Tests require an isolated SQLite database under CHANLUN_TEST_DATA_PATH"
        )


def _ensure_tv_chart_name_capacity(engine) -> bool:
    """Expand the legacy MySQL drawing/layout name column when required.

    ``Base.metadata.create_all`` creates missing tables but deliberately does not
    alter existing columns.  Older installations therefore keep ``VARCHAR(50)``,
    which is one character too short for the standard currency-spot drawing key
    ``drawings_default_default_CURRENCY_SPOT:BTC/USDT_all``.

    SQLite does not enforce the declared VARCHAR length, so only MySQL requires
    an in-place schema upgrade.  Returning whether DDL ran keeps this migration
    straightforward to verify and makes repeated startups idempotent.
    """
    if engine.dialect.name not in {"mysql", "mariadb"}:
        return False

    columns = inspect(engine).get_columns(TableByTVCharts.__tablename__)
    name_column = next(
        (column for column in columns if column["name"] == "name"),
        None,
    )
    if name_column is None:
        raise RuntimeError("cl_tv_charts.name column is missing after table creation")

    current_length = getattr(name_column["type"], "length", None)
    if current_length is None or current_length >= TV_CHART_NAME_MAX_LENGTH:
        return False

    with engine.begin() as connection:
        connection.exec_driver_sql(
            "ALTER TABLE `cl_tv_charts` "
            f"MODIFY COLUMN `name` VARCHAR({TV_CHART_NAME_MAX_LENGTH}) NULL "
            "COMMENT '布局名称'"
        )
    LogUtil.info(
        "Expanded cl_tv_charts.name from VARCHAR(%s) to VARCHAR(%s)",
        current_length,
        TV_CHART_NAME_MAX_LENGTH,
    )
    return True


def _ensure_tv_chart_content_capacity(engine) -> bool:
    """Expand legacy MySQL ``content`` columns to LONGTEXT when required.

    A fresh SQLAlchemy ``Text`` column is only 64 KiB on MySQL, while saved
    TradingView layouts and manual drawing collections can be substantially
    larger.  Existing installations are upgraded in place; SQLite has no
    equivalent enforced text-size ceiling and needs no DDL.
    """
    if engine.dialect.name not in {"mysql", "mariadb"}:
        return False

    columns = inspect(engine).get_columns(TableByTVCharts.__tablename__)
    content_column = next(
        (column for column in columns if column["name"] == "content"),
        None,
    )
    if content_column is None:
        raise RuntimeError("cl_tv_charts.content column is missing after table creation")

    current_type = content_column["type"]
    if (
        isinstance(current_type, MySQLLongText)
        or str(current_type).upper() == "LONGTEXT"
    ):
        return False

    with engine.begin() as connection:
        connection.exec_driver_sql(
            "ALTER TABLE `cl_tv_charts` "
            "MODIFY COLUMN `content` LONGTEXT NULL COMMENT '布局内容'"
        )
    LogUtil.info(
        "Expanded cl_tv_charts.content from %s to LONGTEXT",
        current_type,
    )
    return True


@fun.singleton
class DB(object):
    """SQLAlchemy ORM 封装的数据库访问单例，支持 MySQL 和 SQLite。"""

    def __new__(cls, *args, **kwargs):
        _assert_safe_test_database_config()
        return super().__new__(cls)

    MYSQL_DDL_TIMEOUT = 180
    SQLITE_BUSY_TIMEOUT_MS = _SQLITE_BUSY_TIMEOUT_MS

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
        _ensure_tv_chart_name_capacity(self.engine)
        _ensure_tv_chart_content_capacity(self.engine)

        self.__cache_tables = {}
        # 轻量级缓存：最后一根K线时间，降低重复查询成本。
        # 为避免多线程并发读写出现可见性问题（写入新 K 线时缓存与 DB 不一致），
        # 使用一个独立的锁保护 _last_dt_cache 的所有读写。
        self._last_dt_cache: dict = {}
        self._last_dt_cache_generation: dict = {}
        self._last_dt_cache_lock = threading.Lock()
        # 保护 __cache_tables 的先检查后执行流程以及动态创建 ORM 表类。
        self._cache_tables_lock = threading.Lock()

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

    def _invalidate_last_dt_cache(self, market: str, code: str, frequency: str = None):
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

        # 无锁的先检查后执行会让多个线程首次访问同一冷表时各自创建下方 TableByKlines 类，
        # (向共享 Base.metadata 注册同名 Table), 第2+个线程撞 InvalidRequestError。进程锁
        # + double-check 串行化建表(镜像 exchange.get_exchange 审查 B-1)。
        with self._cache_tables_lock:
            if table_name in self.__cache_tables:
                return self.__cache_tables[table_name]

            orm_class_name = f"TableByKlines_{table_name}"
            table_attributes = {
                "__module__": __name__,
                "__tablename__": table_name,
                "__table_args__": (
                    UniqueConstraint("code", "dt", "f", name="table_code_dt_f_unique"),
                    # 频繁的 (code,f,dt) 查询与排序，建立复合索引
                    Index("idx_code_f_dt", "code", "f", "dt"),
                    {"mysql_collate": "utf8mb4_general_ci"},
                ),
                "code": Column(String(20), primary_key=True, comment="标的代码"),
                "dt": Column(DateTime, primary_key=True, comment="日期"),
                "f": Column(String(5), primary_key=True, comment="周期"),
                "o": Column(Float),
                "c": Column(Float),
                "h": Column(Float),
                "l": Column(Float),
                "v": Column(Float),
            }

            if market == Market.FUTURES.value:
                # 期货市场，添加持仓列
                table_attributes["p"] = Column(Float, comment="持仓量")

            # A fixed inner-class name makes SQLAlchemy replace the previous
            # class-registry entry every time a different market table is
            # declared.  Give each table model a stable, distinct diagnostic
            # identity while retaining the same metadata and cache contract.
            TableByKlines = type(orm_class_name, (Base,), table_attributes)

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

    def zx_get_global_groups(self) -> List[TableByZxGroup]:
        """Return the canonical market-independent watchlist definitions."""

        with self.Session() as session:
            return (
                session.query(TableByZxGroup)
                .filter(TableByZxGroup.market == "__global__")
                .order_by(TableByZxGroup.add_dt.asc(), TableByZxGroup.market.asc())
                .all()
            )

    def zx_add_global_group(self, zx_group: str) -> bool:
        """Create a market-independent group definition.

        New definitions use a reserved storage namespace.  Member rows keep
        their real market because that fact is required to route charts and
        quotes; it is not part of the group's identity.
        """

        with self.Session() as session:
            try:
                exists = (
                    session.query(TableByZxGroup)
                    .filter(
                        TableByZxGroup.market == "__global__",
                        TableByZxGroup.zx_group == zx_group,
                    )
                    .first()
                )
                if exists is not None:
                    return False
                session.add(
                    TableByZxGroup(
                        market="__global__",
                        zx_group=zx_group,
                        add_dt=datetime.datetime.now(),
                    )
                )
                session.commit()
            except Exception:
                session.rollback()
                raise
        return True

    def zx_del_global_group(self, zx_group: str) -> bool:
        """Delete one global group and all of its cross-market members."""

        with self.Session() as session:
            try:
                member_count = (
                    session.query(TableByZixuan)
                    .filter(TableByZixuan.zx_group == zx_group)
                    .delete(synchronize_session=False)
                )
                group_count = (
                    session.query(TableByZxGroup)
                    .filter(
                        TableByZxGroup.market == "__global__",
                        TableByZxGroup.zx_group == zx_group,
                    )
                    .delete(synchronize_session=False)
                )
                session.commit()
            except Exception:
                session.rollback()
                raise
        return bool(member_count or group_count)

    def zx_get_global_group_stocks(
        self,
        zx_group: str,
        *,
        limit: int | None = None,
        markets: tuple[str, ...] | None = None,
    ) -> List[TableByZixuan]:
        """Return ordered group members, optionally bounded in SQL.

        UI callers omit ``limit`` and retain the complete group.  Runtime
        monitors must pass an exact positive integer so a large local group is
        never materialized merely to truncate it in Python.  ``markets`` is
        applied before the limit, preventing A-share rows from consuming a
        bounded non-A monitor query.
        """

        if limit is not None:
            if type(limit) is not int:
                raise TypeError("limit must be an exact integer")
            if limit <= 0:
                raise ValueError("limit must be positive")
        market_scope = None
        if markets is not None:
            if not isinstance(markets, tuple) or any(
                type(market) is not str for market in markets
            ):
                raise TypeError("markets must be a tuple of strings")
            market_scope = tuple(
                dict.fromkeys(market.strip() for market in markets if market.strip())
            )
            if not market_scope:
                return []

        with self.Session() as session:
            query = session.query(TableByZixuan).filter(
                TableByZixuan.zx_group == zx_group
            )
            if market_scope is not None:
                query = query.filter(TableByZixuan.market.in_(market_scope))
            query = query.order_by(
                TableByZixuan.position.asc(),
                TableByZixuan.add_datetime.asc(),
                TableByZixuan.market.asc(),
                TableByZixuan.stock_code.asc(),
            )
            if limit is not None:
                query = query.limit(limit)
            return query.all()

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
                        TableByZixuan.zx_group == zx_group,
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
                session.query(TableByZixuan).filter(
                    TableByZixuan.market == market
                ).filter(TableByZixuan.zx_group == zx_group).filter(
                    TableByZixuan.stock_code == stock_code
                ).delete()
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
                session.query(TableByZixuan).filter(
                    TableByZixuan.market == market
                ).filter(TableByZixuan.zx_group == zx_group).filter(
                    TableByZixuan.stock_code == stock_code
                ).update({"stock_color": color}, synchronize_session=False)
                session.commit()
            except Exception:
                session.rollback()
                raise

        return True

    def zx_stock_sort_top(self, market: str, zx_group: str, stock_code: str):
        with self.Session() as session:
            try:
                session.query(TableByZixuan).filter(
                    TableByZixuan.market == market
                ).filter(TableByZixuan.zx_group == zx_group).update(
                    {"position": TableByZixuan.position + 1}, synchronize_session=False
                )
                session.query(TableByZixuan).filter(
                    TableByZixuan.market == market
                ).filter(TableByZixuan.zx_group == zx_group).filter(
                    TableByZixuan.stock_code == stock_code
                ).update({"position": 0}, synchronize_session=False)
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
                session.query(TableByZixuan).filter(
                    TableByZixuan.market == market
                ).filter(TableByZixuan.zx_group == zx_group).filter(
                    TableByZixuan.stock_code == stock_code
                ).update(
                    {"position": (max_position or 0) + 1},  # 空组最大值为 None 时按零处理
                    synchronize_session=False,
                )
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
            # drawing/study_template/preference 按名称覆盖，避免同名记录重复堆积
            if chart_type in ["drawing", "study_template", "template", "preference"]:
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
                    # pool_pre_ping 会在取连接时替换单条失效连接。这里保留连接池，
                    # 避免一次缓存读取错误迫使其他线程同时重建连接。
                    time.sleep(0.05)
                    continue
                LogUtil.error(f"[db.cache_get] failed key={key}: {e}", exc_info=True)
                return None
        return None

    def cache_set_many(self, values: dict, expire: int = 0):
        """Atomically upsert multiple cache keys in a single transaction."""
        if not values:
            return True

        # 开启事务前先序列化所有值；畸形值不能造成前面的键已提交、后面的键却失败。
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
        # 单键写入与多键设置写入共用同一数据库方言专用的插入或更新路径，
        # 防止两者事务行为漂移。
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
