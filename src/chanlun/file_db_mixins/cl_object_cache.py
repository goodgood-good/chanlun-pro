"""缠论对象 .pkl 缓存 Mixin。

负责 cd 对象的 pickle 持久化与 4 重一致性校验（连续性/OHLC/密度/数据量），
挂载到 FileCacheDB 主类通过多继承提供方法。
"""

from __future__ import annotations

import datetime
import pickle
import random
from decimal import Decimal

import pandas as pd

from chanlun import fun
from chanlun.market import Market
from chanlun.core import cl
from chanlun.core.types import ICL
from chanlun.exchange import Exchange
from chanlun.tools.log_util import LogUtil


class _CLObjectCacheMixin:
    """缠论对象 .pkl 缓存方法，含 4 重一致性校验（连续性/OHLC/密度/数据量）。"""

    def get_web_cl_data(
            self,
            market: str,
            code: str,
            frequency: str,
            cl_config: dict,
            klines: pd.DataFrame,
    ) -> 'ICL':
        """获取 web 缓存的缠论数据对象。"""
        logger = LogUtil.get_logger()
        key = self._config_md5(cl_config)
        log_id = f"[{market}-{code}-{frequency}-{key}]"

        file_pathname = (
                self.cl_data_path
                / market
                / f"{market}_{code.replace('/', '_').replace('.', '_')}_{frequency}_{key}.pkl"
        )

        cd: 'ICL' = cl.CL(code, frequency, cl_config)
        need_recompute = False

        if file_pathname.is_file():
            try:
                with open(file_pathname, "rb") as fp:
                    cd = pickle.load(fp)

                # --- 校验逻辑 ---
                cached_klines = cd.get_src_klines()

                if len(cached_klines) > 0 and len(klines) > 0:
                    # 1. 连续性校验: 判断缓存末尾是否在给定数据时间范围之外
                    if cached_klines[-1].date < klines.iloc[0]["date"] or cached_klines[0].date > klines.iloc[-1]["date"]:
                        logger.warning(f"{log_id} 历史数据错位/不连续, 将全量重算")
                        need_recompute = True

                    # 2. 数据一致性校验 (防止复权导致的历史数据变更)
                    # 参考点取稳定中段 bar（避免实时源最新几根因 tape 修正反复微调），
                    # volume 用相对容差而非严格相等。
                    if not need_recompute and len(cached_klines) >= 12 and len(klines) >= 12:
                        # 跳到稳定中段 bar: 至少 10 根之前, 不超过总长 1/4。
                        ref_idx = -max(10, min(len(cached_klines) // 4, 100))
                        cd_pre_kline = cached_klines[ref_idx]
                        target_rows = klines[klines["date"] == cd_pre_kline.date]

                        if len(target_rows) == 0:
                            logger.warning(f"{log_id} 缓存参考点日期在输入数据中不存在, 重算")
                            need_recompute = True
                        else:
                            row = target_rows.iloc[0]
                            ohlc_diff = (
                                    Decimal(str(row["close"])) != Decimal(str(cd_pre_kline.c)) or
                                    Decimal(str(row["high"])) != Decimal(str(cd_pre_kline.h)) or
                                    Decimal(str(row["low"])) != Decimal(str(cd_pre_kline.l)) or
                                    Decimal(str(row["open"])) != Decimal(str(cd_pre_kline.o))
                            )
                            src_vol = float(row["volume"])
                            cached_vol = float(cd_pre_kline.a)
                            if cached_vol > 0:
                                vol_diff_ratio = abs(src_vol - cached_vol) / cached_vol
                                vol_diff = vol_diff_ratio > 0.05
                            else:
                                vol_diff = src_vol > 0
                            if ohlc_diff or vol_diff:
                                logger.warning(f"{log_id} 检测到历史数据差异 (可能发生复权), 重算")
                                need_recompute = True

                    # 3. 密度校验: 检查最近 100 根 K 线数量是否对得上
                    if not need_recompute and len(cached_klines) >= 100 and len(klines) >= 100:
                        _v_cd = cached_klines[-100:]
                        _v_src = klines[(klines["date"] >= _v_cd[0].date) & (klines["date"] <= _v_cd[-1].date)]
                        if len(_v_cd) != len(_v_src):
                            logger.warning(f"{log_id} 局部数据缺失 [Cache:{len(_v_cd)} vs Src:{len(_v_src)}], 重算")
                            need_recompute = True

                    # 4. 数据量校验: 仅当输入左侧扩展超过缓存量一半时才全量重算。
                    if (
                        not need_recompute
                        and len(cached_klines) > 0
                        and klines.iloc[0]["date"] < cached_klines[0].date
                    ):
                        left_extend_count = int(
                            (klines["date"] < cached_klines[0].date).sum()
                        )
                        if left_extend_count > len(cached_klines) // 2:
                            logger.warning(
                                f"{log_id} 输入左侧扩展 {left_extend_count} 根早于缓存头"
                                f"(缓存量 {len(cached_klines)}), 全量重算"
                            )
                            need_recompute = True

                if need_recompute:
                    cd = cl.CL(code, frequency, cl_config)

            except Exception as e:
                logger.error(f"{log_id} 读取缓存或校验过程异常: {str(e)}", exc_info=True)
                try:
                    if file_pathname.exists():
                        file_pathname.unlink()
                except Exception as un_e:
                    logger.error(f"{log_id} 尝试删除损坏缓存失败: {str(un_e)}")
                cd = cl.CL(code, frequency, cl_config)

        try:
            cd.process_klines(klines)
        except Exception as e:
            # process_klines 抛错时 cd 处于半 applied 状态，返回空白 CL 让调用方下次自然重算
            logger.error(
                f"{log_id} 执行缠论计算 process_klines 失败: {str(e)}", exc_info=True
            )
            return cl.CL(code, frequency, cl_config)

        try:
            self._atomic_write_pickle(file_pathname, cd)
        except Exception as e:
            # 写盘失败会导致下次请求从空缓存重算，影响放大，故用 critical 级
            logger.critical(
                f"{log_id} 写入缓存失败 path={file_pathname} err={str(e)}",
                exc_info=True,
            )

        # 低概率触发清理，由 _try_run_cleanup 负责节流与互斥
        if random.randint(0, 1000) <= 5:
            self._try_run_cleanup(
                "web_cl",
                self.clear_old_web_cl_data,
                on_error=lambda exc: logger.error(f"清理旧缓存数据异常: {exc}"),
            )

        return cd

    def clear_web_cl_data(self, market: str, code: str):
        """清除指定市场下标的缠论缓存对象。"""
        for filename in (self.cl_data_path / market).glob("*.pkl"):
            try:
                if f"{market}_{code.replace('/', '_').replace('.', '_')}" in str(filename):
                    filename.unlink(missing_ok=True)
            except Exception as exc:
                LogUtil.debug(
                    f"[FileCacheDB.clear_web_cl_data] unlink failed "
                    f"file={filename} err={exc}"
                )
        return True

    def clear_old_web_cl_data(self):
        """清除时间超过 15 天的缓存数据。"""
        del_lt_times = fun.datetime_to_int(datetime.datetime.now()) - (
            15 * 24 * 60 * 60
        )
        for _market in Market:
            for filename in (self.cl_data_path / _market.value).glob("*.pkl"):
                try:
                    if filename.stat().st_mtime < del_lt_times:
                        filename.unlink(missing_ok=True)
                except Exception as exc:
                    LogUtil.debug(
                        f"[FileCacheDB.clear_old_web_cl_data] unlink failed "
                        f"file={filename} err={exc}"
                    )
        return True

    def clear_all_cl_data(self):
        """删除所有缓存的计算结果文件。"""
        for _market in Market:
            for filename in (self.cl_data_path / _market.value).glob("*.pkl"):
                try:
                    filename.unlink(missing_ok=True)
                except Exception as exc:
                    LogUtil.debug(
                        f"[FileCacheDB.clear_all_cl_data] unlink failed "
                        f"file={filename} err={exc}"
                    )
        return True

    def get_low_to_high_cl_data(
        self, db_ex: Exchange, market: str, code: str, frequency: str, cl_config: dict
    ) -> ICL:
        """专门为递归到高级别图表写的方法, 初始数据量较多, 从数据库获取后落盘。

        建议定时频繁读取保持更新, 避免太多时间不读取造成数据缺失。
        """
        key = self._config_md5(cl_config)
        # 与 get_web_cl_data 一致：落到 cl_data_path/<market>/ 子目录。
        # 否则 clear_old_web_cl_data 只 glob <market> 子目录，清不到平铺在
        # cl_data_path 根目录的文件 → 这些 .pkl 永不被清理（M7 磁盘泄漏）。
        filename = (
            self.cl_data_path
            / market
            / f'{market}_{code.replace("/", "_").replace(".", "_")}_{frequency}_{key}.pkl'
        )
        filename.parent.mkdir(parents=True, exist_ok=True)
        cd: ICL = None
        if filename.is_file():
            try:
                with open(filename, "rb") as fp:
                    cd = pickle.load(fp)
            except Exception as e:
                LogUtil.warning(
                    f"[FileCacheDB.get_low_to_high_cl_data] pkl 损坏 file={filename} err={e}, 将重新计算"
                )
                try:
                    filename.unlink(missing_ok=True)
                except Exception as unlink_exc:
                    LogUtil.debug(
                        f"[FileCacheDB.get_low_to_high_cl_data] unlink corrupted pkl failed "
                        f"file={filename} err={unlink_exc}"
                    )
                cd = None
        if cd is None:
            cd = cl.CL(code, frequency, cl_config)
        limit = 200000
        if len(cd.get_klines()) > 10000:
            limit = 1000
        klines = db_ex.klines(code, frequency, args={"limit": limit})
        cd.process_klines(klines)
        self._atomic_write_pickle(filename, cd)
        return cd

