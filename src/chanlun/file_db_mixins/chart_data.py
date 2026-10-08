"""TV chart-data disk cache and its restricted unpickler."""

from __future__ import annotations

from concurrent.futures import Future
import pathlib
import pickle
import time
import uuid
from typing import Optional

from chanlun.tools.log_util import LogUtil
from chanlun.persistence.file_lock import interprocess_file_lock


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
    当作不可用缓存处理，由正常写入流程替换。
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

        读取方不删除共享路径，避免误删并发写入的新文件。
        """
        path = self._chart_cache_path_for(cache_key)
        marker = path.with_suffix(path.suffix + ".invalid")
        try:
            if marker.exists():
                return None
            if not path.is_file():
                return None
            with open(path, "rb") as fp:
                obj = _ChartCacheSafeUnpickler(fp).load()
            if not isinstance(obj, dict) or marker.exists():
                # 非预期类型（老格式或文件被篡改），当作 miss 处理
                return None
            return obj
        except Exception as e:
            LogUtil.warning(
                f"[FileCacheDB.get_chart_cache] cache read unavailable "
                f"path={path} error={type(e).__name__}"
            )
            return None

    def set_chart_cache(self, cache_key: str, entry: dict) -> Optional[Future]:
        """提交原子化写入，返回实际文件写入的完成凭据。

        提交失败记日志；异步失败保留在 Future 中，由调用方记录，RAM 仍有副本。
        调用方可等待 Future，以免删除缓存或关闭外层写入器时遗漏仍在落盘的任务。
        """
        path = self._chart_cache_path_for(cache_key)
        marker = path.with_suffix(path.suffix + ".invalid")
        try:
            try:
                invalidation = marker.read_bytes()
            except FileNotFoundError:
                invalidation = None
            written = self._atomic_write_pickle(
                path, entry, suppress_errors=False, durable=invalidation is not None,
                publication_lock=path.with_suffix(path.suffix + ".lock"),
            )
            if invalidation is None:
                # A marker created later belongs to a newer invalidation; this
                # already-started write must never clear it.
                return written
            completion = Future()

            def finish(pending):
                if pending.cancelled():
                    completion.cancel()
                    return
                if not completion.set_running_or_notify_cancel():
                    return
                try:
                    pending.result()
                    # Coordinate marker replacement/removal across processes.
                    # Regular unmarked writes do not acquire this lock.
                    with interprocess_file_lock(marker.with_suffix(".invalid.lock")):
                        try:
                            current = marker.read_bytes()
                        except FileNotFoundError:
                            current = None
                        if current == invalidation:
                            marker.unlink(missing_ok=True)
                    completion.set_result(None)
                except BaseException as exc:
                    completion.set_exception(exc)

            written.add_done_callback(finish)
            return completion
        except Exception as e:
            LogUtil.error(
                f"[FileCacheDB.set_chart_cache] 写入失败 path={path} err={e}"
            )

    def delete_chart_cache(self, cache_key: str) -> None:
        path = self._chart_cache_path_for(cache_key)
        marker = path.with_suffix(path.suffix + ".invalid")
        with interprocess_file_lock(marker.with_suffix(".invalid.lock")):
            # This rejection must survive occupied files and process restarts.
            # Failure to persist the marker is deliberately reported to the
            # caller, which keeps the current runtime's disk reader blocked.
            self._atomic_write_pickle_blocking(marker, uuid.uuid4().hex, durable=True)
            try:
                path.unlink(missing_ok=True)
            except OSError as exc:
                LogUtil.warning(
                    f"[FileCacheDB.delete_chart_cache] invalidated occupied cache "
                    f"path={path} error={type(exc).__name__}"
                )

    def clear_old_chart_cache(self) -> None:
        """删除超过 chart_cache_max_age_seconds 的 pkl 文件 (机会型清理)。"""
        cutoff = time.time() - self.chart_cache_max_age_seconds
        for fn in self.chart_cache_path.glob("*.pkl"):
            try:
                if fn.stat().st_mtime >= cutoff:
                    continue
                # The first age check only selects candidates. A writer may
                # publish a fresh file before cleanup acquires this same lock.
                # Cleanup is opportunistic: skip a busy key after 10 ms.
                with interprocess_file_lock(
                    fn.with_suffix(fn.suffix + ".lock"),
                    timeout_seconds=0.01, poll_seconds=0.01,
                ):
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


