from sqlalchemy import (
    Column,
    Integer,
    String,
    Text,
)
from sqlalchemy.dialects.mysql import LONGTEXT
from chanlun.db_models.base import Base


TV_CHART_NAME_MAX_LENGTH = 255
TV_CHART_CONTENT_TYPE = Text().with_variant(LONGTEXT(), "mysql").with_variant(
    LONGTEXT(), "mariadb"
)


class TableByTVCharts(Base):
    """TradingView 图表布局持久化表 (cl_tv_charts)。"""

    __tablename__ = "cl_tv_charts"
    id = Column(Integer, primary_key=True, autoincrement=True, comment="id")
    client_id = Column(String(50), comment="客户端id")
    user_id = Column(Integer, comment="用户id")
    chart_type = Column(String(20), comment="布局类型")
    symbol = Column(String(50), comment="标的")
    resolution = Column(String(20), comment="周期")
    # TradingView layouts and drawing states can legitimately exceed MySQL
    # TEXT's 64 KiB ceiling.  SQLite keeps the portable Text type while MySQL
    # and MariaDB create LONGTEXT columns.
    content = Column(TV_CHART_CONTENT_TYPE, comment="布局内容")
    timestamp = Column(Integer, comment="时间戳")
    name = Column(String(TV_CHART_NAME_MAX_LENGTH), comment="布局名称")
    __table_args__ = {"mysql_collate": "utf8mb4_general_ci"}
