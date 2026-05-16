from sqlalchemy import (
    Column,
    Integer,
    String,
    Text,
)
from chanlun.db_models.base import Base


class TableByCache(Base):
    """通用键值缓存表 (cl_cache)，存储各类杂项配置或临时数据。"""

    __tablename__ = "cl_cache"
    k = Column(String(100), unique=True, primary_key=True)  # 缓存键，全局唯一
    v = Column(Text, comment="存储内容")
    expire = Column(
        Integer, default=0, comment="过期时间戳，0为永不过期"
    )
    __table_args__ = {"mysql_collate": "utf8mb4_general_ci"}