import datetime
import math
from typing import List
import numpy as np
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

    @staticmethod
    def _normalize_ohlc_geometry(open_, high, low, close):
        """保持 OHLC 蜡烛几何约束，输入必须已是有限浮点数。"""
        normalized_high = max(high, open_, close, low)
        normalized_low = min(low, open_, close, high)
        return open_, normalized_high, normalized_low, close

    @staticmethod
    def _finite_volume(volume) -> float:
        """成交量缺失、非法或非有限时归零，避免污染后续合并与指标。"""
        try:
            value = float(volume)
        except (TypeError, ValueError):
            return 0.0
        return value if math.isfinite(value) else 0.0

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
        vol = self._finite_volume(volume)
        # 审计 D4-HIGH-2: OHLC NaN/Inf 兜底,与 _convert(:177-184)的 ffill 根因防护对齐。
        # 本快路径是 live/walk-forward 主入口(cl.py:177 / live_backtest:980),原仅 volume 有
        # pd.isna 兜底, OHLC 裸 float() → 坏 bar 把 NaN 灌进 klines → bi/xd `.val` 比较静默失效
        # (nan>x 与 nan<x 皆 False)+ MACD inc≠batch。坏值前向填充上一根(无前根则用本 bar 任一
        # 有限 OHLC bfill,全非有限则丢弃该 bar,免填 0 造假跳变)。干净数据零改变。
        def _to_float(value):
            try:
                return float(value)
            except (TypeError, ValueError):
                return float("nan")

        o_, h_, l_, c_ = map(_to_float, (open_, high, low, close))
        if not (math.isfinite(o_) and math.isfinite(h_)
                and math.isfinite(l_) and math.isfinite(c_)):
            prev = self.klines[-1] if self.klines else None

            def _fill(v, prev_v):
                if math.isfinite(v):
                    return v
                if prev_v is not None and math.isfinite(prev_v):
                    return prev_v
                for cand in (c_, o_, h_, l_):
                    if cand is not None and math.isfinite(cand):
                        return cand
                return None

            o_ = _fill(o_, prev.o if prev else None)
            c_ = _fill(c_, prev.c if prev else None)
            h_ = _fill(h_, prev.h if prev else None)
            l_ = _fill(l_, prev.l if prev else None)
            if None in (o_, h_, l_, c_):
                return []  # 全非有限且无前根可顶 → 丢弃该 bar, 不灌 NaN
        o_, h_, l_, c_ = self._normalize_ohlc_geometry(o_, h_, l_, c_)
        kline = Kline(
            index=0,
            date=ts,
            h=h_,
            l=l_,
            o=o_,
            c=c_,
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
        # §2.1 根因防护:OHLC 列若含 NaN/Inf 会经 float() 进 Kline 并污染下游
        # (MACD 增量永久 NaN 中毒 / 笔段比较异常)。仅在确有非有限值时 copy:
        # 先把 Inf 统一视为缺失,仅按同列上一有效值填充；仍缺失时用本 bar 任一有限
        # OHLC 兜底。最终仍无法补齐的全坏行直接丢弃；干净数据只做向量有限性检查。
        _ohlc_cols = [c for c in ("open", "high", "low", "close") if c in df.columns]
        numeric_ohlc = df[_ohlc_cols]
        if any(
            not pd.api.types.is_numeric_dtype(numeric_ohlc[col])
            for col in _ohlc_cols
        ):
            numeric_ohlc = numeric_ohlc.apply(pd.to_numeric, errors="coerce")
        has_nonfinite = _ohlc_cols and not bool(
            np.isfinite(
                numeric_ohlc.to_numpy(
                    dtype=float, copy=False, na_value=float("nan")
                )
            ).all()
        )
        if has_nonfinite:
            df = df.copy()
            sanitized = numeric_ohlc.replace(
                [float("inf"), float("-inf")], float("nan")
            )
            # 禁止沿时间轴 bfill：首根坏数据若从未来价格反填，会造成回测前视偏差，
            # 且与逐根 process_kline_values 的“无前根则丢弃”语义不一致。
            sanitized = sanitized.ffill()
            if self.klines:
                prev = self.klines[-1]
                sanitized = sanitized.fillna(
                    {
                        "open": prev.o,
                        "high": prev.h,
                        "low": prev.l,
                        "close": prev.c,
                    }
                )
            priority = [c for c in ("close", "open", "high", "low") if c in sanitized]
            row_fallback = sanitized[priority].bfill(axis=1).iloc[:, 0]
            for col in _ohlc_cols:
                sanitized[col] = sanitized[col].fillna(row_fallback)
            valid_rows = sanitized.notna().all(axis=1)
            if not bool(valid_rows.all()):
                LogUtil.warning(
                    f"KlineProcessor: 丢弃 {int((~valid_rows).sum())} 条 OHLC 全非有限数据"
                )
                df = df.loc[valid_rows].copy()
                sanitized = sanitized.loc[valid_rows]
            if df.empty:
                return []
            df[_ohlc_cols] = sanitized
        for row in df.itertuples(index=False):
            # volume 可能不存在 / 为 None / 经 to_numeric coerce 成 NaN。
            # NaN 是 truthy，不能用 `or 0.0` 兜底（nan or 0.0 求值为 nan），
            # 否则 NaN 会进入 Kline.a 并经 cl_kline 合并 a=k1.a+k2.a 扩散。
            _vol = getattr(row, 'volume', None) if has_volume else None
            vol = self._finite_volume(_vol)
            open_, high, low, close = self._normalize_ohlc_geometry(
                float(row.open),
                float(row.high),
                float(row.low),
                float(row.close),
            )
            kline = Kline(
                index=0,  # 占位符，将在 _update_internal_klines 中被修正
                date=row.date,
                h=high,
                l=low,
                o=open_,
                c=close,
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
