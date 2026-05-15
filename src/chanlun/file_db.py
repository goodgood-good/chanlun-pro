import atexit
import datetime
import os
import hashlib
import pathlib
import pickle
import random
import threading
import time
import uuid
from concurrent.futures import Future, ThreadPoolExecutor
from decimal import Decimal
from typing import Optional, Union

import pandas as pd
import pytz

from chanlun import fun
from chanlun.core import cl
from chanlun.base import Market
from chanlun.core.cl_interface import ICL
from chanlun.config import get_data_path
from chanlun.db import db
from chanlun.exchange import Exchange
from chanlun.tools.log_util import LogUtil


# US-007: pickle 异步落盘线程池。
# 历史: _atomic_write_pickle 含 Windows 文件锁 450ms 同步退避, 直接卡在 web 请求线程,
# 配合上层 per-key 锁会自我放大成"秒级请求"。这里挪到独立 ThreadPoolExecutor,
# 调用方 (cl 对象 save、cache_pkl、TV chart cache) 立即返回 Future, web 线程不再卡。
# 失败在 worker 内 LogUtil.warning, 不向外传 (写盘失败 = 下次重算, 不应崩调用栈)。
_PICKLE_WRITE_WORKERS = 4
_PICKLE_WRITE_EXECUTOR = ThreadPoolExecutor(
    max_workers=_PICKLE_WRITE_WORKERS,
    thread_name_prefix="FileDbPickleWriter",
)


def _shutdown_pickle_write_executor() -> None:
    # 进程退出时优雅关闭, 等所有 pending 写盘完成 (避免最后一次落盘丢)。
    _PICKLE_WRITE_EXECUTOR.shutdown(wait=True)


atexit.register(_shutdown_pickle_write_executor)


# ===========================================================================
# P8 step 3 (2026-05-15): 4 个 Mixin 类与 _ChartCacheSafeUnpickler 已物理拆到
# ``src/chanlun/file_db_mixins/`` 包内 (按职责分文件: generic_pkl / chart_data /
# kline_cache / cl_object_cache)。本文件保留 FileCacheDB facade + 共享 helper
# (__init__ / _config_md5 / _make_unique_tmp_path / _atomic_write_pickle / 
# _atomic_write_csv / _try_run_cleanup), 4 个 Mixin 通过多继承聚合。
# 外部 ``from chanlun.file_db import _ChartCacheSafeUnpickler`` 仍工作 (re-export)。
# ===========================================================================
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
        """
        初始化，判断文件并进行创建
        """
        self.home_path = pathlib.Path.home()
        self.project_path = get_data_path()
        # 更健壮的目录创建方式（支持多级）
        self.project_path.mkdir(parents=True, exist_ok=True)
        self.cl_data_path = self.project_path / "cl_data"
        self.cl_data_path.mkdir(parents=True, exist_ok=True)
        self.klines_path = self.project_path / "klines"
        self.klines_path.mkdir(parents=True, exist_ok=True)
        # H6：旧数据清理的并发节流。
        # - clear_old_web_cl_data / clear_tdx_old_klines 会遍历 glob+stat+unlink，
        #   高并发下 random.randint(0,1000)<=5 即使概率只有 0.6%，N 个请求同时触发
        #   时仍会形成 race（stat 后被另一线程删掉再 unlink → except pass 掉但浪费 IO）。
        # - 用 Lock + 上次清理时间戳节流，保证全局最多 N 分钟一次，
        #   且同一时刻只有一个清理在跑。tryLock 失败直接跳过本次（清理是机会型任务，
        #   不需要等待）。
        self._cleanup_lock = threading.Lock()
        self._last_cleanup_at: dict = {}  # key: 标识符 → 上次执行时间戳
        # 同一类清理任务两次执行的最小间隔（秒）：5 分钟
        self._cleanup_min_interval = 5 * 60

        self.cache_pkl_path = self.project_path / "cache_pkl"
        self.cache_pkl_path.mkdir(parents=True, exist_ok=True)

        # TV 图表缠论计算结果的落盘缓存目录（2026-04 新增）。
        # 目的：让 web/chanlun_chart 的 `chart_data_cache` 拥有一层持久化兜底，
        # 进程重启 / RAM TTL/maxsize 淘汰后仍可秒命中，全量预热结果不会浪费。
        # 单文件 = 一个 cache_key（market_code_freq_cfgmd5），内容为 _build_chart_cache_entry 返回的 dict。
        self.chart_cache_path = self.project_path / "chart_cache"
        self.chart_cache_path.mkdir(parents=True, exist_ok=True)
        # 落盘缓存的最长保留时间（秒），默认 7 天。超过此时长的文件被随机清理任务回收。
        self.chart_cache_max_age_seconds = 7 * 24 * 60 * 60

        # 遍历 enum 中的值
        for market in Market:
            (self.cl_data_path / market.value).mkdir(parents=True, exist_ok=True)
            (self.klines_path / market.value).mkdir(parents=True, exist_ok=True)

        # 设置时区
        self.tz = pytz.timezone("Asia/Shanghai")
        # self.us_tz = pytz.timezone('US/Eastern')

        # 如果内部有值变动，则会重新计算
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

        # 缠论的更新时间，如果与当前保存不一致，需要清空缓存的计算结果，重新计算
        self.cl_update_date = "2025-06-15"
        cache_cl_update_date = db.cache_get("__cl_update_date")
        if cache_cl_update_date != self.cl_update_date:
            db.cache_set("__cl_update_date", self.cl_update_date)
            self.clear_all_cl_data()

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

        唯一性由 ``pid + 线程 id + uuid4[:8]`` 三元组保证 (任意两个线程拿到的
        tmp 路径不可能相同, 解决 2026-04 并发 polling + 全市场预热同写一个
        cache_key 的 race: 线程 A 调 os.replace 把 tmp move 走后, B 的同名
        tmp 已不存在 → FileNotFoundError 写盘失败)。

        N3 (P8 follow-up): 文件名前缀从毫秒时间戳改 ``%H%M%S`` (6 字符), 保留人
        工 ls 查看时间的便利, 同时缩短文件名。
        """
        suffix = (
            f".tmp-{datetime.datetime.now().strftime('%H%M%S')}"
            f"-{os.getpid()}"
            f"-{threading.get_ident()}"
            f"-{uuid.uuid4().hex[:8]}"
        )
        return path.with_suffix(path.suffix + suffix)

    def _atomic_write_pickle_blocking(self, path: pathlib.Path, obj: object):
        """
        原子化写入 pickle (同步阻塞)，避免并发读到半写入文件。

        2026-05 修复：Windows 上 ``os.replace`` 不能覆盖被其它进程 / 线程"打开"
        的目标文件（即使只是读），实测在并发 polling + 缓存校验场景下会撞
        ``PermissionError [WinError 5] 拒绝访问``，导致缠论 .pkl 写不进去、下次
        请求又得全量重算。
        Linux 的 rename 是原子覆盖，没这个问题。
        这里对 ``os.replace`` 加短指数退避重试（仅 PermissionError），让出 CPU
        给读端关闭句柄；超过最大重试次数才真正放弃。

        US-007: 本方法被 ``_atomic_write_pickle`` 包装到独立 ThreadPoolExecutor
        中异步调用, 不再阻塞 web 线程; web 调用方拿到 Future 后立即返回。
        失败在 worker 内 LogUtil.warning, 不向外传播。
        """
        tmp = self._make_unique_tmp_path(path)
        try:
            with open(tmp, "wb") as fp:
                pickle.dump(obj, fp, protocol=self._PICKLE_PROTOCOL)
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

    def _atomic_write_pickle(self, path: pathlib.Path, obj: object) -> Future:
        """异步 fire-and-forget 写入 pickle (US-007)。

        立刻返回 Future, web 调用线程不再阻塞 450ms 重试退避。worker 内调用
        ``_atomic_write_pickle_blocking`` 真正落盘; 失败仅记 LogUtil.warning,
        不抛到调用栈 (写盘失败 = 下次重算, 不应崩 web 链路)。

        调用方语义变化:
        - 此前: 同步, 异常会向上传播, save_web_cl_data 可据此知道"写盘失败"。
        - 现在: 异步, 失败被 worker 吞掉 + log; 调用方不再有写盘错误反馈通道。
          如有强一致性需求, 显式 await 返回的 Future 即可: ``fut.result()``。

        executor 已关闭等极端场景下, fallback 为同步调用 (与 chart_cache 异步落盘
        路径同款兜底), 保证不丢写。
        """
        def _worker():
            try:
                self._atomic_write_pickle_blocking(path, obj)
            except Exception as e:
                LogUtil.warning(
                    f"[FileCacheDB._atomic_write_pickle] async write failed "
                    f"path={path} err={e}"
                )

        try:
            return _PICKLE_WRITE_EXECUTOR.submit(_worker)
        except RuntimeError as e:
            # executor 已 shutdown (atexit 后) — fallback 同步, 至少不丢写
            # 进程正在 finalize 时降级到 debug, 避免 stderr 已关闭后再写造成
            # "I/O operation on closed file" 噪音 (architect review R2)
            import sys as _sys
            _log_fn = LogUtil.debug if _sys.is_finalizing() else LogUtil.warning
            _log_fn(
                f"[FileCacheDB._atomic_write_pickle] executor shutdown, "
                f"fallback sync write path={path} err={e}"
            )
            fut: Future = Future()
            try:
                self._atomic_write_pickle_blocking(path, obj)
                fut.set_result(None)
            except Exception as inner:
                fut.set_exception(inner)
            return fut

    def _atomic_write_csv(self, path: pathlib.Path, df: pd.DataFrame):
        """
        原子化写入 CSV，先写入临时文件再替换，保证读侧一致性。

        H2：失败时主动清理 .tmp 残留。
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
        H6：尝试以「单飞行 + 时间戳节流」方式执行清理任务。

        - 用 _cleanup_lock 的 acquire(blocking=False) 实现"全局至多一个清理在跑"，
          抢不到锁的请求直接放弃（清理是机会型任务，没必要排队等）。
        - 同一 key 在 _cleanup_min_interval 秒内只跑一次，避免高并发概率叠加导致
          同一类清理几秒内连续触发。
        - 异常通过 on_error 回调上抛或日志记录，由调用方决定语境化处理。
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
                    # 真实根因（磁盘满 / 权限）调用方写盘时会通过 H2 的 critical 暴露。
                    LogUtil.debug(
                        f"[FileCacheDB._try_run_cleanup] cleanup failed key={key} err={exc}"
                    )
            finally:
                # 不论成功失败都更新时间戳：避免失败任务被高频反复重试。
                self._last_cleanup_at[key] = time.time()
        finally:
            self._cleanup_lock.release()

    # =====================================================================
    # P8 区段 1/4: KlineCache 方法已抽到 _KlineCacheMixin (见文件顶部)
    # _kline_parquet_path / _kline_csv_path / save_klines_parquet /
    # load_klines_parquet / get_tdx_klines / save_tdx_klines /
    # clear_tdx_old_klines 通过 Mixin 继承挂到 FileCacheDB。
    # =====================================================================

    # =====================================================================
    # P8 区段 2/4: CLObjectCache 方法已抽到 _CLObjectCacheMixin (见文件顶部)
    # get_web_cl_data / clear_web_cl_data / clear_old_web_cl_data /
    # clear_all_cl_data / get_low_to_high_cl_data 通过 Mixin 继承挂到 FileCacheDB。
    # =====================================================================

    # =====================================================================
    # P8 区段 3/4: GenericPklCache 方法已抽到 _GenericPklCacheMixin (见文件顶部)
    # cache_pkl_to_file / cache_pkl_from_file 通过 Mixin 继承挂到 FileCacheDB。
    # =====================================================================

    # ------------------------------------------------------------------
    # TV 图表缓存（chart_data_cache）的磁盘持久化层（2026-04 新增）
    # ------------------------------------------------------------------
    # 设计要点：
    # - 由 web/chanlun_chart/cl_app/blueprints/tv.py 的 _get/_set_chart_cache_entry 调用；
    # - 一文件一 cache_key，内容是 dict（含 data / min_time / max_time / validated_at /
    #   is_full_snapshot 字段），与 RAM 中 TTLCache 存的对象同构；
    # - 文件名做安全字符替换（/ . → _），不再二次 hash，方便人工排错；
    # - 写入走 _atomic_write_pickle，崩溃也不会留下半写文件；
    # - 不在这里维护 in-memory 索引，调用方自己用 RAM 做热层。

    # =====================================================================
    # P8 区段 4/4: ChartDataCache 方法已抽到 _ChartDataCacheMixin (见文件顶部)
    # _chart_cache_path_for / get/set/delete_chart_cache / clear_old / maybe_cleanup
    # 通过 Mixin 继承挂到 FileCacheDB。
    # =====================================================================


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
    # cd = fdb.get_low_to_high_cl_data(ex, market, code, frequency, cl_config)
    # print(len(cd.get_klines()))
    # print(cd)

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