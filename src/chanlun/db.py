"""facade —— DB 访问层已移至 chanlun.persistence.db。

保留此 re-export 保证已有调用方的
`from chanlun.db import db/DB/TableBy*` 及旧 pickle 继续可解析。
新代码请直接用 chanlun.persistence.db。
"""
from chanlun.persistence.db import *  # noqa: F401,F403
