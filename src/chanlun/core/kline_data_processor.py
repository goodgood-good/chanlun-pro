import datetime
from typing import List
import pandas as pd
from chanlun.core.cl_interface import Kline
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

        # _preprocess 现已优化，会利用 self.klines 剪切传入的 klines_df
        processed_df = self._preprocess(klines_df)

        if processed_df.empty:
            # 预处理后发现没有新数据
            return []

        if not processed_df.empty:
             start_date = processed_df['date'].iloc[0]
             end_date = processed_df['date'].iloc[-1]
             LogUtil.debug(f"KlineProcessor: 预处理后保留 {len(processed_df)} 条数据。范围: {start_date} -> {end_date}")

        new_klines = self._convert(processed_df)

        # 调用更新方法，并返回其返回的增量数据
        increment_klines = self._update_internal_klines(new_klines)
        if increment_klines:
            LogUtil.debug(f"KlineProcessor: 实际产生增量K线 {len(increment_klines)} 条。Last Date: {self.klines[-1].date}")
        return increment_klines

    def _preprocess(self, klines_df: pd.DataFrame) -> pd.DataFrame:
        """预处理K线数据 (排序, 类型转换, 时间过滤, **增量剪切**)。

        ★ P-002 优化：已有 self.klines 时，先按 last_date 把传入帧裁到尾段，
        再对小切片做 copy + to_numeric + sort。避免对全 N 行做无用重活
        （tick 更新场景里前 N-1 行最终都会被丢弃）。

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

            # ★ B1 跨时区比较保护（逻辑从原实现前移）
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
        """
        将DataFrame转换为Kline对象列表。
        (使用 to_dict('records') 代替 iterrows，性能更高)

        Args:
            df (pd.DataFrame): 预处理后的数据。

        Returns:
            List[Kline]: Kline对象列表。
        """
        klines = []
        # .to_dict('records') 比 iterrows 快得多
        # index 暂时设置为 0, 稍后在 _update_internal_klines 中修正
        for row in df.to_dict('records'):
            kline = Kline(
                index=0,  # 占位符，将在 _update_internal_klines 中被修正
                date=row['date'],
                h=float(row['high']),
                l=float(row['low']),
                o=float(row['open']),
                c=float(row['close']),
                # 使用 .get() 并确保 or 0.0 来处理 volume 可能不存在或为None的情况
                a=float(row.get('volume') or 0.0)
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
        # 获取最后一个K线的索引
        last_index = self.klines[-1].index

        # 检查 new_klines[0] 是更新还是追加
        if new_klines[0].date == last_date:
            # --- 更新最后一根K线 ---
            update_kline = new_klines[0]
            update_kline.index = last_index  # 修正索引
            self.klines[-1] = update_kline

            # --- 追加剩余的新K线 ---
            klines_to_append = new_klines[1:]
            for i, k in enumerate(klines_to_append):
                # 从 last_index + 1 开始设置新索引
                k.index = last_index + 1 + i
            self.klines.extend(klines_to_append)

            # 增量数据 = [被更新的K线] + [被追加的K线]
            increment_klines = new_klines

        elif new_klines[0].date > last_date:
            # --- 直接追加所有 new_klines ---
            klines_to_append = new_klines
            for i, k in enumerate(klines_to_append):
                # 从 last_index + 1 开始设置新索引
                k.index = last_index + 1 + i
            self.klines.extend(klines_to_append)

            # 增量数据 = [所有被追加的K线]
            increment_klines = new_klines

        else:
            # 理论上，由于 _preprocess 的过滤，不应该执行到这里
            # (除非传入的数据在 _preprocess 之后仍然包含比 last_date 更早的数据，这表示逻辑有误)
            LogUtil.error(f"K线更新逻辑错误：传入的K线日期 {new_klines[0].date} 早于内部最新日期 {last_date}")
            raise Exception(f"K线更新逻辑错误：传入的K线日期 {new_klines[0].date} 早于内部最新日期 {last_date}")
        # 返回增量数据，此时其 index 已经过修正
        return increment_klines
