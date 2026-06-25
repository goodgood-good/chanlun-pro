import datetime
from typing import List
import pandas as pd
from chanlun.core.types import Kline
from chanlun.tools.log_util import LogUtil


class KlineDataProcessor:
    """
    K线数据处理器
    封装了K线数据的存储、预处理和增量更新的全部逻辑。
    """

    def __init__(self, start_datetime: datetime.datetime = None):
        """
        初始化K线数据处理器

        Args:
            start_datetime (datetime.datetime, optional): 过滤此时间之前的数据. Defaults to None.
        """
        self.klines: List[Kline] = []
        self.start_datetime = start_datetime

    def process_kline(self, klines_df: pd.DataFrame) -> List[Kline]:
        """
        接收DataFrame并更新内部的K线数据列表。
        这是该类唯一的公共入口点。

        此函数现在可以高效处理“全量”或“增量”的 klines_df。

        Args:
            klines_df (pd.DataFrame): 包含新K线数据的DataFrame。

        Returns:
            List[Kline]: 返回增量更新或新增的K线数据列表。
        """
        if klines_df is None or klines_df.empty:
            LogUtil.warning("输入的K线数据为空，不进行处理。")
            return []

        # _preprocess 会利用 self.klines 把传入的 klines_df 剪切到尾段
        processed_df = self._preprocess(klines_df)

        if processed_df.empty:
            # 预处理后发现没有新数据
            return []

        if not processed_df.empty:
             start_date = processed_df['date'].iloc[0]
             end_date = processed_df['date'].iloc[-1]
             LogUtil.debug(lambda:f"KlineProcessor: 预处理后保留 {len(processed_df)} 条数据。范围: {start_date} -> {end_date}")

        new_klines = self._convert(processed_df)

        increment_klines = self._update_internal_klines(new_klines)
        if increment_klines:
            LogUtil.debug(lambda:f"KlineProcessor: 实际产生增量K线 {len(increment_klines)} 条。Last Date: {self.klines[-1].date}")
        return increment_klines

    def process_kline_values(
        self,
        date,
        open_,
        high,
        low,
        close,
        volume=0.0,
    ) -> List[Kline]:
        """Fast path for one already-normalized bar.

        Walk-forward replay feeds one visible bar at a time.  Building a
        one-row DataFrame for every bar spends most of its time in pandas, so
        this path creates the same ``Kline`` object directly and then reuses the
        normal internal append/update logic.
        """
        if date is None:
            return []
        ts = pd.Timestamp(date)
        if self.start_datetime is not None:
            start_ts = pd.Timestamp(self.start_datetime)
            if ts.tzinfo is not None and start_ts.tzinfo is None:
                start_ts = start_ts.tz_localize(ts.tzinfo)
            elif ts.tzinfo is None and start_ts.tzinfo is not None:
                ts = ts.tz_localize(start_ts.tzinfo)
            if ts < start_ts:
                return []
        vol = 0.0 if volume is None or pd.isna(volume) else float(volume)
        kline = Kline(
            index=0,
            date=ts,
            h=float(high),
            l=float(low),
            o=float(open_),
            c=float(close),
            a=vol,
        )
        return self._update_internal_klines([kline])

    def _preprocess(self, klines_df: pd.DataFrame) -> pd.DataFrame:
        """预处理K线数据 (排序, 类型转换, 时间过滤, 增量剪切)。

        已有 self.klines 时先按 last_date 把传入帧裁到尾段，再对小切片做
        copy + to_numeric + sort，避免对全 N 行做无用重活（tick 更新场景里
        前 N-1 行最终都会被丢弃）。

        Args:
            klines_df (pd.DataFrame): 原始K线数据 (可能是全量或增量)。

        Returns:
            pd.DataFrame: 经过预处理和剪切的K线数据 (仅包含增量部分)。
        """
        # ---- 快速路径：已有数据时先粗筛到尾段 ----
        if self.klines:
            last_date = self.klines[-1].date
            date_col = klines_df['date']
            # 仅为比较把 date 列规范成 datetime（不动数值列，不 copy 整帧）；
            # 下方裁到尾段后的 copy 路径会对 klines['date'] 再正式转换一次。
            if not pd.api.types.is_datetime64_any_dtype(date_col):
                date_col = pd.to_datetime(date_col)

            # 跨时区比较保护
            last_ts = pd.Timestamp(last_date)
            df_is_tz_aware = isinstance(date_col.dtype, pd.DatetimeTZDtype)
            if df_is_tz_aware:
                if last_ts.tzinfo is None:
                    last_ts = last_ts.tz_localize(date_col.dt.tz)
                else:
                    last_ts = last_ts.tz_convert(date_col.dt.tz)
            else:
                if last_ts.tzinfo is not None:
                    last_ts = last_ts.tz_convert('UTC').tz_localize(None)

            # 有序用 searchsorted O(log N)，无序退回 boolean mask
            if date_col.is_monotonic_increasing:
                idx = date_col.searchsorted(last_ts, side='left')
                klines_df = klines_df.iloc[idx:]
            else:
                # date_col 与 klines_df 同 index，boolean mask 自动对齐
                klines_df = klines_df[date_col >= last_ts]

            if klines_df.empty:
                return pd.DataFrame()

        # ---- 此时帧已裁到尾段（或首次加载的全量），再做完整预处理 ----
        klines = klines_df.copy()

        if 'date' in klines.columns and not pd.api.types.is_datetime64_any_dtype(klines['date']):
            klines['date'] = pd.to_datetime(klines['date'])

        numeric_cols = ['high', 'low', 'open', 'close', 'volume']
        for col in numeric_cols:
            if col in klines.columns:
                klines[col] = pd.to_numeric(klines[col], errors='coerce')

        if not klines['date'].is_monotonic_increasing:
            klines = klines.sort_values('date').reset_index(drop=True)

        # 按设定的起始时间过滤（首次加载路径仍正常生效；尾段路径里尾段行
        # 必然晚于 last_date >= start_datetime，此过滤不会误删，仅为语义完备）
        if self.start_datetime:
            klines = klines[klines['date'] >= self.start_datetime]
            if klines.empty:
                return pd.DataFrame()

        return klines

    def _convert(self, df: pd.DataFrame) -> List[Kline]:
        """将DataFrame转换为Kline对象列表。index 暂置 0，由 _update_internal_klines 修正。

        用 itertuples(C 实现)替代 to_dict('records')(per-row dict + maybe_box_native
        逐值装箱):batch 摄入约 -33%(12072 bar 28→19ms,占 prep ~47% 的最大单项)。
        列名 date/high/low/open/close/volume 均为合法标识符,itertuples 属性访问安全;
        volume 可选列由 has_volume 预判,date/OHLC 类型与 to_dict 路径一致(golden 守护)。
        """
        klines = []
        has_volume = 'volume' in df.columns
        # §2.1 根因防护:OHLC 列若含 NaN(数据源坏行经 to_numeric coerce)会经 float() 进 Kline
        # 并污染下游(MACD 增量永久 NaN 中毒 / 笔段比较异常);原仅 volume 有兜底,OHLC 无。NaN 为
        # 坏数据,前向填充(取上一根有效值 + bfill 兜首根,避免填 0 造假跳变);仅在确含 NaN 时
        # copy+ffill,干净数据零开销零改变(golden 路径不受影响)。
        _ohlc_cols = [c for c in ("open", "high", "low", "close") if c in df.columns]
        if _ohlc_cols and bool(df[_ohlc_cols].isna().to_numpy().any()):
            df = df.copy()
            df[_ohlc_cols] = df[_ohlc_cols].ffill().bfill()
        for row in df.itertuples(index=False):
            # volume 可能不存在 / 为 None / 经 to_numeric coerce 成 NaN。
            # NaN 是 truthy，不能用 `or 0.0` 兜底（nan or 0.0 求值为 nan），
            # 否则 NaN 会进入 Kline.a 并经 cl_kline 合并 a=k1.a+k2.a 扩散。
            _vol = getattr(row, 'volume', None) if has_volume else None
            vol = 0.0 if _vol is None or pd.isna(_vol) else float(_vol)
            kline = Kline(
                index=0,  # 占位符，将在 _update_internal_klines 中被修正
                date=row.date,
                h=float(row.high),
                l=float(row.low),
                o=float(row.open),
                c=float(row.close),
                a=vol,
            )
            klines.append(kline)
        return klines

    def _update_internal_klines(self, new_klines: List[Kline]) -> List[Kline]:
        """
        执行K线数据的核心增量更新逻辑。

        注意：此函数现在假定传入的 new_klines 已经是 "预剪切" 过的，
        即 new_klines[0].date >= self.klines[-1].date (如果 self.klines 不为空)。

        Returns:
            List[Kline]: 返回增量更新或新增的K线数据列表。
        """
        if not new_klines:
            return []

        if not self.klines:
            # --- 首次加载 ---
            # 这是唯一需要全量设置索引的地方
            for i, k in enumerate(new_klines):
                k.index = i
            self.klines = new_klines
            return self.klines

        # --- 增量更新逻辑 ---
        last_date = self.klines[-1].date
        last_index = self.klines[-1].index

        if new_klines[0].date == last_date:
            # --- 更新最后一根K线 ---
            update_kline = new_klines[0]
            update_kline.index = last_index
            self.klines[-1] = update_kline

            # --- 追加剩余的新K线 ---
            klines_to_append = new_klines[1:]
            for i, k in enumerate(klines_to_append):
                k.index = last_index + 1 + i
            self.klines.extend(klines_to_append)

            # 增量数据 = [被更新的K线] + [被追加的K线]
            increment_klines = new_klines

        elif new_klines[0].date > last_date:
            # --- 直接追加所有 new_klines ---
            klines_to_append = new_klines
            for i, k in enumerate(klines_to_append):
                k.index = last_index + 1 + i
            self.klines.extend(klines_to_append)

            increment_klines = new_klines

        else:
            # _preprocess 已过滤早于 last_date 的数据，走到此处说明上游逻辑有误
            LogUtil.error(f"K线更新逻辑错误：传入的K线日期 {new_klines[0].date} 早于内部最新日期 {last_date}")
            raise Exception(f"K线更新逻辑错误：传入的K线日期 {new_klines[0].date} 早于内部最新日期 {last_date}")
        return increment_klines
