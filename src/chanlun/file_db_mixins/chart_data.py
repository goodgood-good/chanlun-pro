"""TV chart_data 磁盘缓存 Mixin。

含 ``_ChartCacheSafeUnpickler``（pickle RCE 防御反序列化器，仅允许原生数据类型）与
``_ChartDataCacheMixin``。``_ChartCacheSafeUnpickler`` 通过 file_db.py
re-export 保持外部 ``from chanlun.file_db import _ChartCacheSafeUnpickler`` 兼容。
"""

from __future__ import annotations

import pathlib
import pickle
import time
from typing import Optional

from chanlun.tools.log_util import LogUtil


class _ChartCacheSafeUnpickler(pickle.Unpickler):
    """图表缓存 pickle 的安全反序列化器（defense-in-depth）。

    背景：chart cache 文件是本进程自己写的，但磁盘文件在多人/多进程共享部署
    或被恶意置换时存在被污染的风险。pickle.load 在反序列化时就会执行 __reduce__
    里的代码，导致任意命令执行。

    防御策略：
    chart cache 的 entry 只可能是 ``dict / list / tuple / str / int / float /
    bool / None`` 这类原生类型（参见 _build_chart_cache_entry：值都来自
    list[int|float|None] + 几个标量字段）。原生类型走专用 opcode，不经过
    ``find_class``；任何 ``find_class`` 调用都意味着 pickle 流里塞了 class /
    function 引用，这是 RCE 的入口——直接 raise 拒绝，外层 except 把文件
    当损坏删除即可。
    """

    def find_class(self, module, name):  # noqa: D401  (pickle override)
        raise pickle.UnpicklingError(
            f"chart cache pickle 拒绝外部 class/function 引用: {module}.{name}"
        )



class _ChartDataCacheMixin:
    """TradingView /tv/history 路径专用磁盘缓存，配合 _ChartCacheSafeUnpickler 防御 pickle RCE。"""

    def _chart_cache_path_for(self, cache_key: str) -> pathlib.Path:
        # cache_key 形如 "us_GDS.US_30m_<md5hex>"; 点 / 斜杠不是所有 FS 都安全, 做轻度清洗。
        safe = cache_key.replace("/", "_").replace(".", "_")
        return self.chart_cache_path / f"{safe}.pkl"

    def get_chart_cache(self, cache_key: str) -> Optional[dict]:
        """读取 cache_key 对应的图表缓存条目; 不存在或损坏返回 None。

        损坏文件会被主动删除, 避免长期占位。
        """
        path = self._chart_cache_path_for(cache_key)
        if not path.is_file():
            return None
        try:
            with open(path, "rb") as fp:
                obj = _ChartCacheSafeUnpickler(fp).load()
            if not isinstance(obj, dict):
                # 非预期类型（老格式或文件被篡改），当作 miss 处理
                path.unlink(missing_ok=True)
                return None
            return obj
        except Exception as e:
            LogUtil.warning(
                f"[FileCacheDB.get_chart_cache] pkl 损坏/不安全 path={path} err={e}, 删除"
            )
            try:
                path.unlink(missing_ok=True)
            except OSError:
                # 删除残破 pkl 失败 (权限/被占用) 不阻塞读取流程; 外层日志已记录解析失败。
                pass
            return None

    def set_chart_cache(self, cache_key: str, entry: dict) -> None:
        """将一份 chart cache entry 写入磁盘 (原子化)。

        写盘失败仅 error 级日志, 不抛——RAM 仍有副本, 下次启动重新预热即可。
        """
        path = self._chart_cache_path_for(cache_key)
        try:
            self._atomic_write_pickle(path, entry)
        except Exception as e:
            LogUtil.error(
                f"[FileCacheDB.set_chart_cache] 写入失败 path={path} err={e}"
            )

    def delete_chart_cache(self, cache_key: str) -> None:
        path = self._chart_cache_path_for(cache_key)
        try:
            path.unlink(missing_ok=True)
        except OSError:
            # 文件被占用/权限拒绝时静默: 缓存淘汰是 best-effort, 下次重写会覆盖。
            pass

    def clear_old_chart_cache(self) -> None:
        """删除超过 chart_cache_max_age_seconds 的 pkl 文件 (机会型清理)。"""
        cutoff = time.time() - self.chart_cache_max_age_seconds
        for fn in self.chart_cache_path.glob("*.pkl"):
            try:
                if fn.stat().st_mtime < cutoff:
                    fn.unlink(missing_ok=True)
            except Exception as exc:
                LogUtil.debug(
                    f"[FileCacheDB.clear_old_chart_cache] unlink failed "
                    f"file={fn} err={exc}"
                )

    def maybe_cleanup_chart_cache(self) -> None:
        """供外部调用方在低概率分支触发的清理入口 (统一节流)。"""
        self._try_run_cleanup("chart_cache", self.clear_old_chart_cache)


