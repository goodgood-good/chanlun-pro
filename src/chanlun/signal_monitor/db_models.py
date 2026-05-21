# -*- coding: utf-8 -*-
"""信号监控的两张 ORM 表。

用项目共享的 ``Base``（``chanlun.db_models.base``），但建表由本包
``repository._ensure_tables`` 自管，不依赖 ``db.py`` 去 import / 建表。
"""
from sqlalchemy import (
    Column,
    DateTime,
    Index,
    Integer,
    String,
    Text,
)

from chanlun.db_models.base import Base


class TableBySignalMonitorTask(Base):
    """缠论买卖信号监控任务配置表 (cl_signal_monitor_task)。

    与 legacy ``cl_alert_task`` 并存、互不影响。
    """

    __tablename__ = "cl_signal_monitor_task"
    id = Column(Integer, primary_key=True, autoincrement=True)
    market = Column(String(20), comment="市场")
    task_name = Column(String(100), comment="任务名称")
    zx_group = Column(String(50), comment="自选组")
    operation_level = Column(String(20), comment="操作级别周期，如 30m")
    level_ladder = Column(String(200), comment="级别梯队，逗号分隔，由高到低，如 d,30m,5m")
    signal_kinds = Column(String(200), comment="要监控的信号类型，逗号分隔")
    min_grade = Column(String(2), comment="最低分级 A/B/C，低于此的信号被过滤")
    interval_minutes = Column(Integer, comment="检查间隔分钟")
    is_run = Column(Integer, comment="是否运行 1/0")
    is_send_msg = Column(Integer, comment="是否发送消息 1/0")
    dt = Column(DateTime, comment="任务添加、修改时间")
    __table_args__ = {"mysql_collate": "utf8mb4_general_ci"}


class TableBySignalRecord(Base):
    """缠论买卖信号触发记录表 (cl_signal_record)。

    每次信号命中写一行，既是历史，也用于跨扫描轮次按 ``identity`` 去重。
    """

    __tablename__ = "cl_signal_record"
    id = Column(Integer, primary_key=True, autoincrement=True)
    market = Column(String(20), comment="市场")
    task_name = Column(String(100), comment="任务名称")
    stock_code = Column(String(20), comment="标的")
    stock_name = Column(String(100), comment="标的名称")
    operation_level = Column(String(20), comment="操作级别周期")
    signal_kind = Column(String(30), comment="信号类型")
    direction = Column(String(10), comment="信号方向 bullish/bearish")
    identity = Column(String(255), comment="信号去重身份键")
    grade = Column(String(2), comment="信号分级 A/B/C")
    score = Column(Integer, comment="信号评分 0-100")
    alert_msg = Column(Text, comment="信号说明")
    signal_dt = Column(DateTime, comment="信号触发 K 线时间")
    alert_dt = Column(DateTime, comment="提醒时间")
    __table_args__ = (
        Index("idx_cl_signal_record_identity", "market", "identity"),
        {"mysql_collate": "utf8mb4_general_ci"},
    )
