"""Parquet-only K-line cache mixin."""

from __future__ import annotations

import datetime
import os
import pathlib
import random
from typing import Union

import pandas as pd

from chanlun import fun
from chanlun.tools.log_util import LogUtil


class _KlineCacheMixin:
    """Persist and retrieve the sole production K-line cache format."""

    def _kline_parquet_path(self, market: str, code: str, frequency: str) -> pathlib.Path:
        return self.klines_path / market / f"{code.replace('.', '_')}_{frequency}.parquet"

    def save_klines_parquet(
        self, market: str, code: str, frequency: str, df: pd.DataFrame
    ) -> bool:
        """Atomically persist a K-line frame as parquet (pyarrow + zstd)."""
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
        """Read parquet K-lines; missing or corrupt files are cache misses."""
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
        """Return cached K-lines from the sole parquet representation."""
        _klines = self.load_klines_parquet(market, code, frequency)
        if _klines is None:
            return None

        # Parquet 会保留来源数据类型，部分供应商仍可能给出字符串；
        # 在下游日期时间运算前先规范化。
        if _klines is not None and len(_klines) > 0 and "date" in _klines.columns:
            if not pd.api.types.is_datetime64_any_dtype(_klines["date"]):
                _klines["date"] = pd.to_datetime(_klines["date"], errors="coerce")

        if len(_klines) > 0:
            if _klines["date"].isnull().any():
                return None
            # 丢弃最后一根：缓存写入时末根通常是尚未收盘的当前 bar，不作为
            # 历史数据返回，调用方按需从实时源补全最新 bar。
            _klines = _klines.iloc[0:-1]

        # 随机概率清理历史缓存，真正的并发节流由 _try_run_cleanup 保证。
        if random.randint(0, 1000) <= 5:
            self._try_run_cleanup(
                f"tdx::{market}",
                lambda: self.clear_tdx_old_klines(market),
            )
        return _klines

    def save_tdx_klines(
        self, market: str, code: str, frequency: str, kline: pd.DataFrame
    ):
        """Persist K-lines using the production parquet cache format."""
        return self.save_klines_parquet(market, code, frequency, kline)

    def clear_tdx_old_klines(self, market):
        """Delete parquet K-line cache files older than 15 days."""
        del_lt_times = fun.datetime_to_int(datetime.datetime.now()) - (
            15 * 24 * 60 * 60
        )
        market_dir = self.klines_path / market
        for filename in market_dir.glob("*.parquet"):
            try:
                if filename.stat().st_mtime < del_lt_times:
                    filename.unlink(missing_ok=True)
            except Exception as exc:
                LogUtil.debug(
                    f"[FileCacheDB.clear_tdx_old_klines] unlink failed "
                    f"file={filename} err={exc}"
                )
        return True


