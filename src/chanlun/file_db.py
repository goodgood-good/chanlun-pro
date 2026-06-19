"""facade —— 文件缓存层已移至 chanlun.persistence.file_db。

保留此 re-export 保证已有调用方的 `from chanlun.file_db import fdb/FileCacheDB/
_ChartCacheSafeUnpickler` 继续可解析。新代码请直接用 chanlun.persistence.file_db。
"""
from chanlun.persistence.file_db import *  # noqa: F401,F403
from chanlun.persistence.file_db import _ChartCacheSafeUnpickler  # noqa: F401  下划线名,import* 不带
