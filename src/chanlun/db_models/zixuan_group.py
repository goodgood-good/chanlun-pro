from sqlalchemy import (
    Column,
    DateTime,
    String,
    UniqueConstraint,
)
from chanlun.db_models.base import Base


class TableByZxGroup(Base):
    """自选组定义表 (cl_zixuan_groups)，market + zx_group 联合主键唯一确定一个自选组。"""

    __tablename__ = "cl_zixuan_groups"
    __table_args__ = (
        UniqueConstraint("market", "zx_group", name="table_market_group_unique"),
    )
    market = Column(String(20), primary_key=True, comment="市场")
    zx_group = Column(String(20), primary_key=True, comment="自选组名称")
    add_dt = Column(DateTime, comment="添加时间")
    # __table_args__ 仅保留此处的 mysql_collate；上方 UniqueConstraint 版本在运行时被此行覆盖（已有行为，不改逻辑）
    __table_args__ = {"mysql_collate": "utf8mb4_general_ci"}
