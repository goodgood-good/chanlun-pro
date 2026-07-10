"""通用 pkl 缓存 Mixin。

任意 Python 对象的 pickle 持久化，与 K 线/缠论对象/TV chart 三类专用缓存独立，
供 notebook / 选股脚本 / 自定义工具使用。
"""

from __future__ import annotations

import pickle

from chanlun.tools.log_util import LogUtil


class _GenericPklCacheMixin:
    """通用任意对象 pickle 持久化，供 notebook / 选股脚本等自定义工具使用。"""

    def cache_pkl_to_file(self, filename: str, data: object):
        """将缓存数据持久化到文件中。"""
        self._atomic_write_pickle(self.cache_pkl_path / filename, data)

    def cache_pkl_from_file(self, filename: str) -> object:
        """从文件中读取数据, 不存在或腐坏返回 None。"""
        file_path = self.cache_pkl_path / filename
        if file_path.is_file() is False:
            return None
        try:
            with open(file_path, "rb") as fp:
                return pickle.load(fp)
        except Exception as e:
            # 腐坏/版本不兼容/模块搬迁致反序列化失败时与"文件不存在"同语义返回 None
            # (与 chart_data/kline_cache 及 test_pkl_load_corrupt_guard 已确立的"腐坏返回
            # None"契约一致),避免裸 pickle.load 异常冒泡阻断 reboot_trader 启动装载 trader
            # 状态并重启复崩。坏文件保留,下次 cache_pkl_to_file 原子写覆盖自愈;仅告警。
            LogUtil.warning(f"[cache_pkl_from_file] pkl 腐坏, 按 miss 处理 file={filename} err={e}")
            return None


