"""通用 pkl 缓存 Mixin。

任意 Python 对象的 pickle 持久化，与 K 线/缠论对象/TV chart 三类专用缓存独立，
供 notebook / 选股脚本 / 自定义工具使用。
"""

from __future__ import annotations

import pickle


class _GenericPklCacheMixin:
    """通用任意对象 pickle 持久化，供 notebook / 选股脚本等自定义工具使用。"""

    def cache_pkl_to_file(self, filename: str, data: object):
        """将缓存数据持久化到文件中。"""
        self._atomic_write_pickle(self.cache_pkl_path / filename, data)

    def cache_pkl_from_file(self, filename: str) -> object:
        """从文件中读取数据, 不存在返回 None。"""
        if (self.cache_pkl_path / filename).is_file() is False:
            return None
        with open(self.cache_pkl_path / filename, "rb") as fp:
            return pickle.load(fp)


