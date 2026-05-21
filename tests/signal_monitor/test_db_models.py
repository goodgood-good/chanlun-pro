"""tests/signal_monitor/test_db_models.py — 信号监控两张表的 ORM 模型单测（内存 sqlite）。"""
from __future__ import annotations

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from chanlun.db_models.base import Base
from chanlun.signal_monitor.db_models import (
    TableBySignalMonitorTask,
    TableBySignalRecord,
)


def _mem_session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()


def test_signal_monitor_task_roundtrip():
    s = _mem_session()
    s.add(TableBySignalMonitorTask(
        market="a", task_name="t1", zx_group="g", operation_level="30m",
        level_ladder="d,30m,5m", signal_kinds="bi_beichi,xd_beichi",
        min_grade="B", interval_minutes=5, is_run=1, is_send_msg=1,
    ))
    s.commit()
    row = s.query(TableBySignalMonitorTask).filter_by(task_name="t1").first()
    assert row is not None
    assert row.operation_level == "30m"
    assert row.level_ladder == "d,30m,5m"
    assert row.min_grade == "B"


def test_signal_record_roundtrip():
    s = _mem_session()
    s.add(TableBySignalRecord(
        market="a", task_name="t1", stock_code="SH.600000", stock_name="x",
        operation_level="30m", signal_kind="bi_beichi", direction="bullish",
        identity="SH.600000|30m|bi_beichi|bullish|2024-01-01 10:00:00|down",
        grade="A", score=85, alert_msg="m",
    ))
    s.commit()
    row = s.query(TableBySignalRecord).filter_by(stock_code="SH.600000").first()
    assert row is not None
    assert row.grade == "A"
    assert row.score == 85
    assert row.direction == "bullish"
