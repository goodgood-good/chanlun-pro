# -*- coding: utf-8 -*-
"""信号监控的持久化层（任务配置 + 触发记录 CRUD）。

CRUD 收拢在本模块，**不侵入项目共享的 db.py**：复用 ``db.engine`` / ``db.Session``，
并自管两张表的建表（``create_all`` 只建自己的表，幂等）。依赖方向单向：
``signal_monitor`` → ``chanlun.db``，反向零侵入。
"""
from __future__ import annotations

import datetime
from typing import List, Optional

from chanlun.persistence.db import db
from chanlun.db_models.base import Base
from chanlun.signal_monitor.db_models import (
    TableBySignalMonitorTask,
    TableBySignalRecord,
)

_tables_ready = False


def _ensure_tables() -> None:
    """惰性建表：只建本包两张表，幂等（已存在则跳过）。"""
    global _tables_ready
    if _tables_ready:
        return
    Base.metadata.create_all(
        db.engine,
        tables=[TableBySignalMonitorTask.__table__, TableBySignalRecord.__table__],
    )
    _tables_ready = True


# ----------------------------- 任务配置 CRUD -----------------------------
def signal_task_save(
    market: str, task_name: str, zx_group: str, operation_level: str,
    level_ladder: str, signal_kinds: str, min_grade: str,
    interval_minutes: int, is_run: int, is_send_msg: int,
) -> bool:
    """新增一个信号监控任务。"""
    _ensure_tables()
    with db.Session() as session:
        try:
            session.add(TableBySignalMonitorTask(
                market=market, task_name=task_name, zx_group=zx_group,
                operation_level=operation_level, level_ladder=level_ladder,
                signal_kinds=signal_kinds, min_grade=min_grade,
                interval_minutes=interval_minutes, is_run=is_run,
                is_send_msg=is_send_msg, dt=datetime.datetime.now(),
            ))
            session.commit()
        except Exception:
            session.rollback()
            raise
    return True


def signal_task_query(
    market: str = None, id: int = None
) -> List[TableBySignalMonitorTask]:
    """查询信号监控任务列表。id 为主键查询，limit(1) 防御脏数据。"""
    _ensure_tables()
    with db.Session() as session:
        query = session.query(TableBySignalMonitorTask)
        filters = ()
        if market is not None:
            filters += (TableBySignalMonitorTask.market == market,)
        if id is not None:
            filters += (TableBySignalMonitorTask.id == id,)
        if filters:
            query = query.filter(*filters)
        if id is not None:
            query = query.limit(1)
        return query.all()


def signal_task_update(
    id: int, market: str, task_name: str, zx_group: str, operation_level: str,
    level_ladder: str, signal_kinds: str, min_grade: str,
    interval_minutes: int, is_run: int, is_send_msg: int,
) -> bool:
    """更新信号监控任务。"""
    _ensure_tables()
    with db.Session() as session:
        try:
            session.query(TableBySignalMonitorTask).filter(
                TableBySignalMonitorTask.id == id,
            ).update({
                TableBySignalMonitorTask.market: market,
                TableBySignalMonitorTask.task_name: task_name,
                TableBySignalMonitorTask.zx_group: zx_group,
                TableBySignalMonitorTask.operation_level: operation_level,
                TableBySignalMonitorTask.level_ladder: level_ladder,
                TableBySignalMonitorTask.signal_kinds: signal_kinds,
                TableBySignalMonitorTask.min_grade: min_grade,
                TableBySignalMonitorTask.interval_minutes: interval_minutes,
                TableBySignalMonitorTask.is_run: is_run,
                TableBySignalMonitorTask.is_send_msg: is_send_msg,
                TableBySignalMonitorTask.dt: datetime.datetime.now(),
            })
            session.commit()
        except Exception:
            session.rollback()
            raise
    return True


def signal_task_delete(id: int) -> bool:
    """删除信号监控任务。"""
    _ensure_tables()
    with db.Session() as session:
        try:
            session.query(TableBySignalMonitorTask).filter(
                TableBySignalMonitorTask.id == id
            ).delete()
            session.commit()
        except Exception:
            session.rollback()
            raise
    return True


# ----------------------------- 触发记录 CRUD -----------------------------
def signal_record_save(
    market: str, task_name: str, stock_code: str, stock_name: str,
    operation_level: str, signal_kind: str, direction: str, identity: str,
    grade: str, score: int, alert_msg: str,
    signal_dt: datetime.datetime = None,
) -> bool:
    """保存一条信号触发记录。"""
    _ensure_tables()
    with db.Session() as session:
        try:
            if signal_dt is not None and getattr(signal_dt, "tzinfo", None) is not None:
                signal_dt = signal_dt.replace(tzinfo=None)
            session.add(TableBySignalRecord(
                market=market, task_name=task_name, stock_code=stock_code,
                stock_name=stock_name, operation_level=operation_level,
                signal_kind=signal_kind, direction=direction, identity=identity,
                grade=grade, score=score, alert_msg=alert_msg,
                signal_dt=signal_dt, alert_dt=datetime.datetime.now(),
            ))
            session.commit()
        except Exception:
            session.rollback()
            raise
    return True


def signal_record_query_by_identity(
    market: str, identity: str
) -> Optional[TableBySignalRecord]:
    """按 identity 查最近一条信号记录，用于跨轮次去重判断。"""
    _ensure_tables()
    with db.Session() as session:
        return (
            session.query(TableBySignalRecord)
            .filter(
                TableBySignalRecord.market == market,
                TableBySignalRecord.identity == identity,
            )
            .order_by(TableBySignalRecord.alert_dt.desc())
            .first()
        )


def signal_record_query(
    market: str, task_name: str = None
) -> List[TableBySignalRecord]:
    """查询信号记录列表（最近 100 条，降序），可按 task_name 过滤。"""
    _ensure_tables()
    with db.Session() as session:
        query = session.query(TableBySignalRecord).filter(
            TableBySignalRecord.market == market
        )
        if task_name:
            query = query.filter(TableBySignalRecord.task_name == task_name)
        return query.order_by(TableBySignalRecord.alert_dt.desc()).limit(100).all()
