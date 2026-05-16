from sqlalchemy import (
    Column,
    DateTime,
    Float,
    Integer,
    String,
)
from chanlun.db_models.base import Base

class TableByOrder(Base):
    """手动记账订单表 (cl_order)，用于记录手动下单/持仓信息。"""

    __tablename__ = "cl_order"
    id = Column(Integer, primary_key=True, autoincrement=True)
    market = Column(String(20), comment="市场")
    stock_code = Column(String(20), comment="标的代码")
    stock_name = Column(String(100), comment="标的名称")
    order_type = Column(String(20), comment="订单类型")
    order_price = Column(Float, comment="订单价格")
    order_amount = Column(Float, comment="订单数量")
    order_memo = Column(String(200), comment="订单备注")
    dt = Column(DateTime, comment="添加时间")
    __table_args__ = {"mysql_collate": "utf8mb4_general_ci"}
