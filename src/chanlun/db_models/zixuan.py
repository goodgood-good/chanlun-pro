from sqlalchemy import (
    Column,
    DateTime,
    Integer,
    String,
)
from chanlun.db_models.base import Base


class TableByZixuan(Base):
    """自选股/标的明细表 (cl_zixuan_watchlist)，记录各自选组内的标的列表。"""

    __tablename__ = "cl_zixuan_watchlist"
    id = Column(Integer, primary_key=True, autoincrement=True)
    market = Column(String(20), comment="市场")
    zx_group = Column(String(20), comment="自选组")
    stock_code = Column(String(20), comment="标的代码")
    stock_name = Column(String(100), comment="标的名称")
    position = Column(Integer, comment="位置")  # 控制组内排序顺序
    add_datetime = Column(DateTime, comment="添加时间")
    stock_color = Column(String(20), comment="自选颜色")
    stock_memo = Column(String(100), comment="附加信息")
    __table_args__ = {"mysql_collate": "utf8mb4_general_ci"}
