from sqlalchemy import (
    Column,
    DateTime,
    Integer,
    String,
    UniqueConstraint,
)
from chanlun.db_models.base import Base


class TableByAlertTask(Base):
    """预警任务配置表 (cl_alert_task)，定义各市场/自选组的预警规则。"""

    __tablename__ = "cl_alert_task"
    __table_args__ = (
        UniqueConstraint("market", "task_name", name="table_market_task_name_unique"),
    )
    id = Column(Integer, primary_key=True, autoincrement=True)
    market = Column(String(20), comment="市场")
    task_name = Column(String(100), comment="任务名称")
    zx_group = Column(String(20), comment="自选组")
    frequency = Column(String(20), comment="检查周期")
    interval_minutes = Column(Integer, comment="检查间隔分钟")
    check_bi_type = Column(String(20), comment="检查笔的类型")
    check_bi_beichi = Column(String(200), comment="检查笔的背驰")
    check_bi_mmd = Column(String(200), comment="检查笔的买卖点")
    check_xd_type = Column(String(20), comment="检查线段的类型")
    check_xd_beichi = Column(String(200), comment="检查线段的背驰")
    check_xd_mmd = Column(String(200), comment="检查线段的买卖点")
    check_idx_ma_info = Column(String(200), comment="检查指数的均线")
    check_idx_macd_info = Column(String(200), comment="检查指数的MACD")
    is_run = Column(Integer, comment="是否运行")
    is_send_msg = Column(Integer, comment="是否发送消息")
    dt = Column(DateTime, comment="任务添加、修改时间")
    # __table_args__ 仅保留此处的 mysql_collate；上方 UniqueConstraint 版本在运行时被此行覆盖（已有行为，不改逻辑）
    __table_args__ = {"mysql_collate": "utf8mb4_general_ci"}
