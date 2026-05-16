from sqlalchemy.orm import declarative_base

# 所有 ORM 模型的公共基类，由 SQLAlchemy declarative_base() 生成
Base = declarative_base()