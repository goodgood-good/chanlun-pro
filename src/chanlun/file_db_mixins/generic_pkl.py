"""src/chanlun/file_db_mixins/generic_pkl.py — 通用 pkl 缓存 Mixin。

P8 step 3 (2026-05-15): 从 file_db.py 物理拆出。
被 FileCacheDB 多继承聚合, 调用方 ``from chanlun.file_db import fdb`` 不变。
"""

from __future__ import annotations

import pickle


# P8 step 2.1: GenericPklCacheMixin
# ---------------------------------------------------------------------------
# 任意 Python 对象的 pickle 持久化, 与 K 线 / 缠论对象 / TV chart 三类专用
# cache 独立。供 notebook / 选股脚本 / 自定义工具使用。
# 依赖 FileCacheDB 主类提供的: ``cache_pkl_path`` 字段 + ``_atomic_write_pickle()``
# ===========================================================================
class _GenericPklCacheMixin:
    """通用 pkl 缓存方法 (P8 拆分)。

    Python Mixin: 通过多继承挂到 ``FileCacheDB`` 上, 方法体内 ``self.xxx``
    访问的字段 / 方法由主类提供, MRO 自动路由, 行为完全等价。
    """

    def cache_pkl_to_file(self, filename: str, data: object):
        """将缓存数据持久化到文件中。"""
        self._atomic_write_pickle(self.cache_pkl_path / filename, data)

    def cache_pkl_from_file(self, filename: str) -> object:
        """从文件中读取数据, 不存在返回 None。"""
        if (self.cache_pkl_path / filename).is_file() is False:
            return None
        with open(self.cache_pkl_path / filename, "rb") as fp:
            return pickle.load(fp)


