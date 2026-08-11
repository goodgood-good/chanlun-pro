from sqlalchemy import (
    Column,
    DateTime,
    String,
    UniqueConstraint,
)
from chanlun.db_models.base import Base


class TableByZxGroup(Base):
    """全局自选组定义表；生产行固定存放在 ``__global__`` 命名空间。"""

    __tablename__ = "cl_zixuan_groups"
    __table_args__ = (
        UniqueConstraint("market", "zx_group", name="table_market_group_unique"),
        {"mysql_collate": "utf8mb4_general_ci"},
    )
    market = Column(String(20), primary_key=True, comment="市场")
    zx_group = Column(String(20), primary_key=True, comment="自选组名称")
    add_dt = Column(DateTime, comment="添加时间")
