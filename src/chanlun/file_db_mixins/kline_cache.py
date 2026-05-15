"""src/chanlun/file_db_mixins/kline_cache.py — K 线 CSV/parquet 缓存 Mixin。

P8 step 3 (2026-05-15): 从 file_db.py 物理拆出。
"""

from __future__ import annotations

import datetime
import os
import pathlib
import random
from typing import Union

import pandas as pd

from chanlun import fun
from chanlun.tools.log_util import LogUtil


# P8 step 2.3: KlineCacheMixin
# ---------------------------------------------------------------------------
# 负责 tdx K 线 CSV + parquet 双写过渡 (US-008)。
# 依赖 FileCacheDB 主类提供的:
#   字段 ``klines_path``
#   方法 ``_make_unique_tmp_path()`` / ``_atomic_write_csv()`` / ``_try_run_cleanup()``
# ===========================================================================
class _KlineCacheMixin:
    """K 线 CSV/parquet 缓存方法 (P8 拆分)。"""

    def _kline_parquet_path(self, market: str, code: str, frequency: str) -> pathlib.Path:
        return self.klines_path / market / f"{code.replace('.', '_')}_{frequency}.parquet"

    def _kline_csv_path(self, market: str, code: str, frequency: str) -> pathlib.Path:
        return self.klines_path / market / f"{code.replace('.', '_')}_{frequency}.csv"

    def save_klines_parquet(
        self, market: str, code: str, frequency: str, df: pd.DataFrame
    ) -> bool:
        """K 线 DataFrame 落盘为 parquet (pyarrow + zstd)。

        相比 CSV: 体积小 60-80%, 读写快 5x, 原生保留 datetime / tz / dtype。
        与 save_tdx_klines 的关系: 后者会同时调本方法 (双写过渡)。
        """
        path = self._kline_parquet_path(market, code, frequency)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._make_unique_tmp_path(path)
        try:
            df.to_parquet(tmp, engine="pyarrow", compression="zstd", index=False)
            os.replace(tmp, path)
            return True
        except Exception as exc:
            try:
                if tmp.exists():
                    tmp.unlink(missing_ok=True)
            except Exception as cleanup_exc:
                LogUtil.debug(
                    f"[FileCacheDB.save_klines_parquet] cleanup tmp failed "
                    f"path={tmp} err={cleanup_exc}"
                )
            LogUtil.warning(
                f"[FileCacheDB.save_klines_parquet] write failed "
                f"market={market} code={code} freq={frequency} err={exc}"
            )
            return False

    def load_klines_parquet(
        self, market: str, code: str, frequency: str
    ) -> Union[None, pd.DataFrame]:
        """读 parquet K 线, 不存在或损坏返回 None (调用方可走 CSV fallback)。"""
        path = self._kline_parquet_path(market, code, frequency)
        if not path.is_file():
            return None
        try:
            return pd.read_parquet(path, engine="pyarrow")
        except Exception as exc:
            LogUtil.debug(
                f"[FileCacheDB.load_klines_parquet] read failed (file corrupt?), "
                f"unlinking path={path} err={exc}"
            )
            path.unlink(missing_ok=True)
            return None

    def get_tdx_klines(
        self, market: str, code: str, frequency: str
    ) -> Union[None, pd.DataFrame]:
        """
        获取缓存在文件中的股票数据。

        US-008: 优先读 parquet (体积小 + 读快), 失败 fallback CSV (兼容老数据)。
        新数据由 save_tdx_klines 同时写 parquet + CSV (双写过渡), 灰度期满后
        可拆掉 CSV 路径只留 parquet。
        """
        _klines = self.load_klines_parquet(market, code, frequency)
        if _klines is None:
            file_pathname = self._kline_csv_path(market, code, frequency)
            if file_pathname.is_file() is False:
                return None
            try:
                _klines = pd.read_csv(file_pathname, parse_dates=["date"])
            except Exception:
                # H3:用 missing_ok 防止并发清理线程已经删过该文件时再次抛错。
                file_pathname.unlink(missing_ok=True)
                return None

        if len(_klines) > 0:
            if _klines["date"].isnull().any():
                return None
            _klines = _klines.iloc[0:-1:]

        # H6: 随机概率清理历史缓存, 真正的并发节流由 _try_run_cleanup 保证。
        if random.randint(0, 1000) <= 5:
            self._try_run_cleanup(
                f"tdx::{market}",
                lambda: self.clear_tdx_old_klines(market),
            )
        return _klines

    def save_tdx_klines(
        self, market: str, code: str, frequency: str, kline: pd.DataFrame
    ):
        """保存通达信 k 线数据对象到文件中。

        US-008: 同时写 parquet (主) + CSV (兜底, 灰度期); 灰度期满后只删
        _atomic_write_csv 这一行 + clear 里的 csv glob 即可。
        """
        self.save_klines_parquet(market, code, frequency, kline)
        csv_path = self._kline_csv_path(market, code, frequency)
        self._atomic_write_csv(csv_path, kline)
        return True

    def clear_tdx_old_klines(self, market):
        """删除 15 天前的 k 线数据 (parquet + CSV 都清), 减少占用空间。"""
        del_lt_times = fun.datetime_to_int(datetime.datetime.now()) - (
            15 * 24 * 60 * 60
        )
        market_dir = self.klines_path / market
        # US-008: parquet 和 CSV 一起清; 二者文件名同源, mtime 同步更新
        for pattern in ("*.csv", "*.parquet"):
            for filename in market_dir.glob(pattern):
                try:
                    if filename.stat().st_mtime < del_lt_times:
                        # missing_ok=True: 防御 glob 后 unlink 前的并发清理
                        filename.unlink(missing_ok=True)
                except Exception as exc:
                    LogUtil.debug(
                        f"[FileCacheDB.clear_tdx_old_klines] unlink failed "
                        f"file={filename} err={exc}"
                    )
        return True


