"""facade —— DB 访问层已移至 chanlun.persistence.db(命名重构,见 dir_structure_audit)。

保留此 re-export 保证 ~27 处调用方(web 后端/exchange/trader/cl_utils/zixuan)的
`from chanlun.db import db/DB/TableBy*` 及旧 pickle 继续可解析。
新代码请直接用 chanlun.persistence.db。
"""
from chanlun.persistence.db import *  # noqa: F401,F403
