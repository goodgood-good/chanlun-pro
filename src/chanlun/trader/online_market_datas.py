"""
线上行情数据获取对象，用于实盘交易执行
"""

from typing import List, Dict

import pandas as pd

from chanlun.trading.base import MarketDatas
from chanlun.exchange.exchange import Exchange


def _freq_minutes(frequency: str):
    """级别字符串 -> 周期分钟数(秒级返回分数分钟, 如 "10s"->1/6); 非(秒/分)级(d/w/月)返回 None=不裁剪。"""
    f = str(frequency).strip().lower()
    if f.endswith("s"):
        try:
            return max(int(f[:-1]), 1) / 60.0
        except ValueError:
            return None
    if f.endswith("m"):
        try:
            return max(int(f[:-1]), 1)
        except ValueError:
            return None
    return None


def _drop_unclosed_last_bar(df: pd.DataFrame, frequency: str) -> pd.DataFrame:
    """丢弃仍在进行(未收盘)的末根 bar, 使实盘缠论信号口径与回测/paper 一致(D2-F4)。

    该函数自包含且不依赖外部时钟参数：用末两根推断间隔，再用与末根同 tz 的当前时刻判断末根
    周期是否已结束。非分钟级/不足两根时原样返回; 间隔异常(session 首根等)仅裁
    「标签在未来」的末根, 绝不误删历史收盘 bar。
    """
    minutes = _freq_minutes(frequency)
    if minutes is None or df is None or len(df) < 2:
        return df
    try:
        last_ts = pd.Timestamp(df["date"].iloc[-1])
        prev_ts = pd.Timestamp(df["date"].iloc[-2])
    except Exception:
        return df
    step = pd.Timedelta(minutes=minutes)
    if (last_ts - prev_ts) != step:
        # 间隔异常(session 首根/跳空): 仅裁「标签在未来」的末根(必为进行中bar),
        # 已收盘 bar 标签必然 <= now, 绝不误删历史收盘 bar。口径同 paper 副本。
        now = pd.Timestamp.now(tz=last_ts.tz) if last_ts.tz is not None else pd.Timestamp.now()
        if now < last_ts:
            return df.iloc[:-1]
        return df
    now = pd.Timestamp.now(tz=last_ts.tz) if last_ts.tz is not None else pd.Timestamp.now()
    if now < last_ts + step:
        return df.iloc[:-1]
    return df


class OnlineMarketDatas(MarketDatas):
    """实盘行情数据适配器，封装交易所接口并提供单次循环内的 K 线缓存。"""

    def __init__(
        self,
        market: str,
        frequencys: List[str],
        ex: Exchange,
        cl_config: dict,
        use_cache=True,
    ):
        """
        :param use_cache: 是否开启循环内 K 线缓存。
            开启时同一根 K 线在一次循环内只请求一次；每轮开始必须调用 begin_round()。
        """
        super().__init__(market, frequencys, cl_config)
        self.ex = ex

        self.use_cache = use_cache

        # key 为 "{_round_seq}_{code}_{frequency}"，每轮由 begin_round 推进。
        self.cache_klines: Dict[str, pd.DataFrame] = {}

        # 轮次序号；begin_round() 每轮自增使上轮 K 线缓存键失效。
        self._round_seq = 0

    def begin_round(self):
        """每轮循环开始调用；推进轮次序号并清除上轮 K 线缓存。"""
        self._round_seq += 1
        self.cache_klines = {}
        return True

    def klines(self, code, frequency) -> pd.DataFrame:
        """获取 K 线数据；use_cache=True 时循环内复用缓存，避免重复请求。"""
        key = f"{self._round_seq}_{code}_{frequency}"
        if self.use_cache and key in self.cache_klines.keys():
            return self.cache_klines[key]
        klines = self.ex.klines(code, frequency)
        if self.use_cache:
            self.cache_klines[key] = klines
        return klines

    def closed_klines(self, code, frequency) -> pd.DataFrame:
        """Return the cached frame with any still-open terminal bar removed.

        Strict live screening, replay and stock selection all consume this
        exact closed-bar boundary.  Keeping it public avoids each caller
        reimplementing the wall-clock rule before entering the canonical
        structure runtime.
        """

        return _drop_unclosed_last_bar(self.klines(code, frequency), frequency)

    def closed_bar_as_of(self, code, frequency):
        """Return the causal close time of the terminal row in closed_klines."""

        frame = self.closed_klines(code, frequency)
        if frame is None or frame.empty:
            raise ValueError("closed market bars are unavailable")
        return pd.Timestamp(frame["date"].iloc[-1]).to_pydatetime()

    def last_k_info(self, code) -> dict:
        klines = self.klines(code, self.frequencys[-1])
        return {
            "date": klines.iloc[-1]["date"],
            "open": float(klines.iloc[-1]["open"]),
            "close": float(klines.iloc[-1]["close"]),
            "high": float(klines.iloc[-1]["high"]),
            "low": float(klines.iloc[-1]["low"]),
        }
