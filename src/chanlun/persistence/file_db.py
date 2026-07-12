import atexit
from collections import deque
from collections.abc import Callable
import datetime
import os
import hashlib
import pathlib
import pickle
import random
import threading
import time
import uuid
from concurrent.futures import Future
from decimal import Decimal
from typing import Optional, Union

import pandas as pd
import pytz

from chanlun import fun
from chanlun.core import cl
from chanlun.market import Market
from chanlun.core.types import ICL
from chanlun.config import get_data_path
from chanlun.persistence.db import db
from chanlun.tools.daemon_executor import DaemonExecutor
from chanlun.tools.log_util import LogUtil


class _PickleWriteQueue:
    """同一路径 pickle 写入的短生命周期 FIFO 队列。"""

    def __init__(self) -> None:
        self.items: deque[tuple[Callable[[], None], Future, bool]] = deque()


# pickle 异步落盘线程池：Windows 文件锁重试约 450ms，挪到独立 daemon executor
# 后调用方立即返回 Future，web 线程不再阻塞。写盘失败仅 warning，不崩调用栈。
_PICKLE_WRITE_WORKERS = 4
_PICKLE_WRITE_MAX_PATHS = 256
_PICKLE_WRITE_MAX_PENDING_PER_PATH = 8
_PICKLE_WRITE_EXECUTOR = None
_PICKLE_WRITE_QUEUE_LOCK = threading.Lock()
_PICKLE_WRITES_CLOSED = True
_PICKLE_WRITES_ACCEPTING = True
_PICKLE_WRITE_QUEUES: dict[str, _PickleWriteQueue] = {}


def _complete_pickle_write(
    operation, result_future: Future, suppress_errors: bool, path_key: str
) -> None:
    if not result_future.set_running_or_notify_cancel():
        return
    try:
        operation()
    except Exception as exc:
        if suppress_errors:
            try:
                LogUtil.warning(
                    f"[FileCacheDB._atomic_write_pickle] async write failed "
                    f"path={path_key} err={exc}"
                )
            except BaseException as log_exc:
                result_future.set_exception(log_exc)
            else:
                result_future.set_result(None)
        else:
            result_future.set_exception(exc)
    except BaseException as exc:
        result_future.set_exception(exc)
    else:
        result_future.set_result(None)


def _drain_pickle_write_queue(path_key: str) -> None:
    """Serially execute one path queue and release its registry entry."""
    while True:
        with _PICKLE_WRITE_QUEUE_LOCK:
            state = _PICKLE_WRITE_QUEUES.get(path_key)
            if state is None:
                return
            if not state.items:
                del _PICKLE_WRITE_QUEUES[path_key]
                return
            operation, result_future, suppress_errors = state.items.popleft()
        _complete_pickle_write(
            operation,
            result_future,
            suppress_errors,
            path_key,
        )

def pickle_write_queue_status():
    with _PICKLE_WRITE_QUEUE_LOCK:
        return {
            "paths": len(_PICKLE_WRITE_QUEUES),
            "pending": sum(len(state.items) for state in _PICKLE_WRITE_QUEUES.values()),
        }


def start_pickle_writes():
    """Recreate the writer after an application lifecycle restart."""
    global _PICKLE_WRITE_EXECUTOR, _PICKLE_WRITES_ACCEPTING, _PICKLE_WRITES_CLOSED
    with _PICKLE_WRITE_QUEUE_LOCK:
        if not _PICKLE_WRITES_CLOSED:
            return
        if _PICKLE_WRITE_QUEUES:
            raise RuntimeError("cannot restart pickle writer with active path queues")
        _PICKLE_WRITE_EXECUTOR = DaemonExecutor(
            max_workers=_PICKLE_WRITE_WORKERS,
            thread_name_prefix="FileDbPickleWriter",
        )
        _PICKLE_WRITES_ACCEPTING = True
        _PICKLE_WRITES_CLOSED = False


def allow_lazy_pickle_writes():
    """Allow a newly created application to start the writer on first use."""
    global _PICKLE_WRITES_ACCEPTING
    with _PICKLE_WRITE_QUEUE_LOCK:
        if _PICKLE_WRITES_CLOSED and not _PICKLE_WRITE_QUEUES:
            _PICKLE_WRITES_ACCEPTING = True

def shutdown_pickle_writes(wait=False, cancel_pending=False):
    """Stop the writer; optionally cancel queued work without waiting forever."""
    global _PICKLE_WRITE_EXECUTOR, _PICKLE_WRITES_ACCEPTING, _PICKLE_WRITES_CLOSED
    with _PICKLE_WRITE_QUEUE_LOCK:
        _PICKLE_WRITES_ACCEPTING = False
        _PICKLE_WRITES_CLOSED = True
        executor = _PICKLE_WRITE_EXECUTOR
        _PICKLE_WRITE_EXECUTOR = None
    if cancel_pending:
        with _PICKLE_WRITE_QUEUE_LOCK:
            queued = [
                future
                for state in _PICKLE_WRITE_QUEUES.values()
                for _, future, _ in state.items
            ]
            for state in _PICKLE_WRITE_QUEUES.values():
                state.items.clear()
        for future in queued:
            future.cancel()
    if executor is not None:
        executor.shutdown(
            wait=bool(wait),
            cancel_futures=bool(cancel_pending),
        )
    return pickle_write_queue_status()


def _shutdown_pickle_write_executor() -> None:
    shutdown_pickle_writes(wait=False, cancel_pending=True)

atexit.register(_shutdown_pickle_write_executor)


# 4 个 Mixin 类与 _ChartCacheSafeUnpickler 已物理拆到 file_db_mixins/ 包内
# (generic_pkl / chart_data / kline_cache / cl_object_cache)。
# 本文件保留 FileCacheDB facade + 共享 helper，4 个 Mixin 通过多继承聚合。
# 外部 ``from chanlun.file_db import _ChartCacheSafeUnpickler`` 仍工作 (re-export)。
from chanlun.file_db_mixins import (  # noqa: E402  re-export keeps外部 import 兼容
    _ChartCacheSafeUnpickler,
    _ChartDataCacheMixin,
    _CLObjectCacheMixin,
    _GenericPklCacheMixin,
    _KlineCacheMixin,
)


class FileCacheDB(_GenericPklCacheMixin, _ChartDataCacheMixin, _KlineCacheMixin, _CLObjectCacheMixin):
    """
    文件数据对象
    """

    def __init__(self):
        """初始化各数据目录，不存在时自动创建。"""
        self.home_path = pathlib.Path.home()
        self.project_path = get_data_path()
        self.project_path.mkdir(parents=True, exist_ok=True)
        self.cl_data_path = self.project_path / "cl_data"
        self.cl_data_path.mkdir(parents=True, exist_ok=True)
        self.klines_path = self.project_path / "klines"
        self.klines_path.mkdir(parents=True, exist_ok=True)
        # 旧数据清理的并发节流：用 Lock + 时间戳保证全局最多 N 分钟一次清理，
        # 且同一时刻只有一个清理在跑（清理是机会型任务，抢不到锁直接跳过）。
        self._cleanup_lock = threading.Lock()
        self._last_cleanup_at: dict = {}  # key: 标识符 → 上次执行时间戳
        # 同一类清理任务两次执行的最小间隔（秒）：5 分钟
        self._cleanup_min_interval = 5 * 60

        self.cache_pkl_path = self.project_path / "cache_pkl"
        self.cache_pkl_path.mkdir(parents=True, exist_ok=True)

        # TV 图表缠论计算结果的落盘缓存目录。
        # 进程重启 / RAM TTL 淘汰后仍可秒命中，单文件对应一个 cache_key。
        self.chart_cache_path = self.project_path / "chart_cache"
        self.chart_cache_path.mkdir(parents=True, exist_ok=True)
        # 落盘缓存的最长保留时间（秒），默认 7 天。超过此时长的文件被随机清理任务回收。
        self.chart_cache_max_age_seconds = 7 * 24 * 60 * 60

        for market in Market:
            (self.cl_data_path / market.value).mkdir(parents=True, exist_ok=True)
            (self.klines_path / market.value).mkdir(parents=True, exist_ok=True)

        self.tz = pytz.timezone("Asia/Shanghai")
        # self.us_tz = pytz.timezone('US/Eastern')

        # 这些 key 的组合决定 _config_md5，任何一项变化都会导致缠论缓存失效并重算
        self.config_keys = [
            "kline_type",
            "kline_qk",
            "judge_zs_qs_level",
            "fx_qy",
            "fx_qj",
            "fx_bh",
            "bi_type",
            "bi_bzh",
            "bi_qj",
            "bi_fx_cgd",
            "bi_split_k_cross_nums",
            "fx_check_k_nums",
            "allow_bi_fx_strict",
            "xd_qj",
            "xd_allow_bi_pohuai",
            "xd_allow_split_no_highlow",
            "xd_allow_split_zs_kz",
            "xd_allow_split_zs_more_line",
            "xd_allow_split_zs_no_direction",
            "xd_zs_max_lines_split",
            "zs_bi_type",
            "zs_xd_type",
            "zs_qj",
            "zs_cd",
            "zs_wzgx",
            "zs_optimize",
            "cl_mmd_cal_qs_1mmd",
            "cl_mmd_cal_not_qs_3mmd_1mmd",
            "cl_mmd_cal_qs_3mmd_1mmd",
            "cl_mmd_cal_qs_not_lh_2mmd",
            "cl_mmd_cal_qs_bc_2mmd",
            "cl_mmd_cal_3mmd_not_lh_bc_2mmd",
            "cl_mmd_cal_1mmd_not_lh_2mmd",
            "cl_mmd_cal_3mmd_xgxd_not_bc_2mmd",
            "cl_mmd_cal_not_in_zs_3mmd",
            "cl_mmd_cal_not_in_zs_gt_9_3mmd",
            "idx_macd_fast",
            "idx_macd_slow",
            "idx_macd_signal",
        ]

        # 缠论算法版本号；与 DB 中存储值不一致时触发全量清缓存，强制重算。
        # 每次修改核心算法逻辑后需更新此日期。
        self.cl_update_date = "2025-06-15"
        cache_cl_update_date = db.cache_get("__cl_update_date")
        if cache_cl_update_date != self.cl_update_date:
            # 先清缓存再写版本标记: 若 clear_all_cl_data 被 kill/断电/单文件 unlink 失败中断
            # 而版本已先写, 下次启动版本匹配→跳过清理→旧算法 pkl 被新代码增量续算(静默错缠论)。
            # 反序后中断则版本保持旧值, 下次启动重试清理(恒安全)。
            self.clear_all_cl_data()
            db.cache_set("__cl_update_date", self.cl_update_date)

    def _config_md5(self, cl_config: dict) -> str:
        """
        生成稳定的配置 MD5：严格按照 self.config_keys 顺序生成，避免 dict 插入顺序差异。
        列表类型做字符串化处理以保持一致性。
        """
        parts = []
        for k in self.config_keys:
            v = cl_config.get(k, "0")
            if isinstance(v, list):
                v = ",".join(v)
            parts.append(f"{k}:{v}")
        unique_str = "|".join(parts)
        return hashlib.md5(unique_str.encode("UTF-8")).hexdigest()

    # 固定 pickle 协议为 4（Python 3.4+ 都兼容），避免不同 Python 版本之间互不兼容。
    # 之前用 pickle.HIGHEST_PROTOCOL，从 3.8 升到 3.12 后旧 pkl 文件无法读取。
    _PICKLE_PROTOCOL = 4

    @staticmethod
    def _make_unique_tmp_path(path: pathlib.Path) -> pathlib.Path:
        """生成保证唯一的 .tmp 文件路径。

        唯一性由 ``pid + 线程 id + uuid4[:8]`` 三元组保证，防止并发写同一
        cache_key 时多个 tmp 文件互相覆盖导致 FileNotFoundError。
        文件名前缀用 ``%H%M%S``（6 字符），方便人工 ls 查看时间。
        """
        suffix = (
            f".tmp-{datetime.datetime.now().strftime('%H%M%S')}"
            f"-{os.getpid()}"
            f"-{threading.get_ident()}"
            f"-{uuid.uuid4().hex[:8]}"
        )
        return path.with_suffix(path.suffix + suffix)

    @staticmethod
    def _fsync_parent_directory(path: pathlib.Path) -> None:
        """尽可能持久化目录项；Windows 不支持目录 fsync 时安全降级。"""
        flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
        try:
            directory_fd = os.open(path, flags)
        except OSError:
            if os.name == "nt":
                return
            raise
        try:
            os.fsync(directory_fd)
        except OSError:
            if os.name != "nt":
                raise
        finally:
            os.close(directory_fd)

    def _atomic_write_pickle_blocking(
        self, path: pathlib.Path, obj: object, *, durable: bool = False
    ):
        """
        原子化写入 pickle (同步阻塞)，避免并发读到半写入文件。

        Windows 上 os.replace 不能覆盖被其它进程/线程"打开"的目标文件，
        并发场景会撞 PermissionError，对 os.replace 加短指数退避重试（仅
        PermissionError），让出 CPU 给读端关闭句柄。
        本方法由 _atomic_write_pickle 包装到独立线程池中异步调用。
        """
        tmp = self._make_unique_tmp_path(path)
        try:
            with open(tmp, "wb") as fp:
                pickle.dump(obj, fp, protocol=self._PICKLE_PROTOCOL)
                if durable:
                    fp.flush()
                    os.fsync(fp.fileno())
            # Windows 文件锁兜底重试：4 次共约 30+60+120+240 = 450ms 退避。
            # 现在跑在 _PICKLE_WRITE_EXECUTOR 的 worker 上, 不再卡 web 线程。
            _delays_ms = (30, 60, 120, 240)
            for _attempt, _delay in enumerate((0, *_delays_ms)):
                if _delay:
                    time.sleep(_delay / 1000.0)
                try:
                    os.replace(tmp, path)
                    break
                except PermissionError:
                    if _attempt == len(_delays_ms):
                        raise  # 重试用完仍失败，让外层捕获 + 清理 tmp
                    continue
            if durable:
                self._fsync_parent_directory(path.parent)
        except Exception:
            # os.replace 失败 / pickle dump 失败 / 磁盘满 等场景，清掉残留 tmp。
            try:
                if tmp.exists():
                    tmp.unlink(missing_ok=True)
            except Exception as cleanup_exc:
                # 清理失败不能掩盖原异常，仅记录 debug 用于事后排查（如磁盘只读、权限等）。
                LogUtil.debug(
                    f"[FileCacheDB._atomic_write_pickle_blocking] cleanup tmp failed "
                    f"path={tmp} err={cleanup_exc}"
                )
            raise

    def _atomic_write_pickle(
        self,
        path: pathlib.Path,
        obj: object,
        *,
        suppress_errors: bool = True,
        durable: bool = False,
    ) -> Future:
        """异步 fire-and-forget 写入 pickle，立刻返回 Future，web 线程不阻塞。

        同一路径严格按提交顺序写入，不同路径仍可在线程池中并行。
        写盘失败仅记 warning，不抛到调用栈（下次重算即可）。
        如有强一致性需求可显式 await：``fut.result()``。
        executor 提交竞态失败时 fallback 为同步调用；显式关闭后拒绝新写入。
        """
        global _PICKLE_WRITE_EXECUTOR, _PICKLE_WRITES_ACCEPTING, _PICKLE_WRITES_CLOSED
        absolute_path = pathlib.Path(os.path.abspath(os.fspath(path)))
        path_key = os.path.normcase(os.fspath(absolute_path))
        result_future: Future = Future()

        def _write() -> None:
            if durable:
                self._atomic_write_pickle_blocking(
                    absolute_path, obj, durable=True
                )
            else:
                self._atomic_write_pickle_blocking(absolute_path, obj)

        run_synchronously = False
        capacity_rejected = False
        shutdown_error: RuntimeError | None = None
        with _PICKLE_WRITE_QUEUE_LOCK:
            if _PICKLE_WRITES_CLOSED or _PICKLE_WRITE_EXECUTOR is None:
                if not _PICKLE_WRITES_ACCEPTING:
                    if suppress_errors:
                        result_future.set_result(None)
                    else:
                        result_future.set_exception(
                            RuntimeError("pickle writer is shut down")
                        )
                    return result_future
                start_executor = DaemonExecutor(
                    max_workers=_PICKLE_WRITE_WORKERS,
                    thread_name_prefix="FileDbPickleWriter",
                )
                _PICKLE_WRITE_EXECUTOR = start_executor
                _PICKLE_WRITES_CLOSED = False
            state = _PICKLE_WRITE_QUEUES.get(path_key)
            if state is None:
                if len(_PICKLE_WRITE_QUEUES) >= max(1, int(_PICKLE_WRITE_MAX_PATHS)):
                    capacity_rejected = True
                    if suppress_errors:
                        result_future.set_result(None)
                    else:
                        result_future.set_exception(
                            BufferError(
                                f"pickle active path limit reached path={path_key}"
                            )
                        )
                else:
                    state = _PickleWriteQueue()
                    _PICKLE_WRITE_QUEUES[path_key] = state
                    state.items.append((_write, result_future, suppress_errors))
                    try:
                        _PICKLE_WRITE_EXECUTOR.submit(
                            _drain_pickle_write_queue, path_key
                        )
                    except RuntimeError as exc:
                        run_synchronously = True
                        shutdown_error = exc
            else:
                limit = max(1, int(_PICKLE_WRITE_MAX_PENDING_PER_PATH))
                if len(state.items) >= limit:
                    drop_index = next(
                        (
                            index
                            for index, (_, _, queued_suppresses) in enumerate(state.items)
                            if queued_suppresses
                        ),
                        None,
                    )
                    if drop_index is None:
                        if suppress_errors:
                            result_future.set_result(None)
                        else:
                            result_future.set_exception(
                                BufferError(
                                    f"pickle write queue full for path={path_key}"
                                )
                            )
                        return result_future
                    _, dropped_future, _ = state.items[drop_index]
                    del state.items[drop_index]
                    if not dropped_future.done():
                        dropped_future.set_result(None)
                state.items.append((_write, result_future, suppress_errors))

        if capacity_rejected:
            try:
                LogUtil.warning(
                    f"[FileCacheDB._atomic_write_pickle] active path limit reached, "
                    f"reject path={path_key}"
                )
            except Exception:
                pass
            return result_future
        if run_synchronously:
            import sys as _sys

            _log_fn = LogUtil.debug if _sys.is_finalizing() else LogUtil.warning
            try:
                _log_fn(
                    f"[FileCacheDB._atomic_write_pickle] executor shutdown, "
                    f"fallback sync write path={path} err={shutdown_error}"
                )
            except Exception:
                pass
            finally:
                _drain_pickle_write_queue(path_key)

        return result_future

    def _atomic_write_csv(self, path: pathlib.Path, df: pd.DataFrame):
        """
        原子化写入 CSV，先写入临时文件再替换，保证读侧一致性。失败时主动清理 .tmp 残留。
        """
        tmp = self._make_unique_tmp_path(path)
        try:
            df.to_csv(tmp, index=False)
            os.replace(tmp, path)
        except Exception:
            try:
                if tmp.exists():
                    tmp.unlink(missing_ok=True)
            except Exception as cleanup_exc:
                LogUtil.debug(
                    f"[FileCacheDB._atomic_write_csv] cleanup tmp failed "
                    f"path={tmp} err={cleanup_exc}"
                )
            raise

    def _try_run_cleanup(self, key: str, fn, on_error=None):
        """
        以「单飞行 + 时间戳节流」方式执行清理任务。

        全局至多一个清理在跑，抢不到锁直接放弃（机会型任务）。
        同一 key 在 _cleanup_min_interval 秒内只跑一次。
        异常通过 on_error 回调处理，由调用方决定语境化处理。
        """
        if not self._cleanup_lock.acquire(blocking=False):
            return
        try:
            now_ts = time.time()
            last_ts = self._last_cleanup_at.get(key, 0)
            if now_ts - last_ts < self._cleanup_min_interval:
                return
            try:
                fn()
            except Exception as exc:
                if on_error is not None:
                    try:
                        on_error(exc)
                    except Exception as on_err_exc:
                        # on_error 自己抛错时也不能影响主流程，仅 debug 留痕。
                        LogUtil.debug(
                            f"[FileCacheDB._try_run_cleanup] on_error raised key={key} "
                            f"err={on_err_exc}"
                        )
                else:
                    # 没有 on_error 时记录 debug：清理失败不影响主流程，
                    # 真实根因（磁盘满 / 权限）调用方写盘时会以 critical 级别暴露。
                    LogUtil.debug(
                        f"[FileCacheDB._try_run_cleanup] cleanup failed key={key} err={exc}"
                    )
            finally:
                # 不论成功失败都更新时间戳：避免失败任务被高频反复重试。
                self._last_cleanup_at[key] = time.time()
        finally:
            self._cleanup_lock.release()

    # KlineCache / CLObjectCache / GenericPklCache / ChartDataCache 方法
    # 均已抽到 file_db_mixins/ 对应 Mixin 类，通过多继承挂到 FileCacheDB。


fdb = FileCacheDB()

if __name__ == "__main__":
    from chanlun.cl_utils import query_cl_chart_config
    from chanlun.exchange.exchange_binance import ExchangeBinance

    # market = 'a'
    # code = 'SHSE.000001'
    # frequency = '5m'
    # cl_config = query_cl_chart_config(market, code)
    # ex = ExchangeDB(market)

    fdb = FileCacheDB()

    ex = ExchangeBinance()
    market = "currency"
    code = "APT/USDT"
    freq = "d"
    cl_config = query_cl_chart_config(market, code)
    klines = ex.klines(code, freq)

    cd = fdb.get_web_cl_data(market, code, freq, cl_config, klines)
    print(cd)
    cl_config = query_cl_chart_config(market, code)
    cd = fdb.get_web_cl_data(market, code, freq, cl_config, klines)
    print(cd)


#     currency--APT/USDT--d 726a8925bda1d6fb6ac6fbe5b146fd5a index: 541 date: 2024-04-12 08:00:00+08:00 h: 12.223 l: 8.422 o: 11.862 c:9.775 a:42964252.4 code                       APT/USDT
# date      2024-04-12 08:00:00+08:00
# open                         11.862
# high                         12.223
# low                           8.422
# close                         9.775
# volume                   42964300.0
# Name: 541, dtype: object
