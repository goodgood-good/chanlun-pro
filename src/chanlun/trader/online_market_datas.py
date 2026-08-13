"""
线上行情数据获取对象，用于实盘交易执行
"""

from typing import List, Dict

import pandas as pd

from chanlun.trading.base import MarketDatas
from chanlun.exchange.exchange import Exchange
from chanlun.exchange.kline_completion import (
    drop_unclosed_last_bar as _drop_unclosed_last_bar,
)


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
        """返回循环内缓存行情的已收盘连续前缀。

        实时筛选、回放与选股共同使用这一边界，调用方不得在进入唯一结构核心前
        再实现另一套墙钟判断。
        """

        return _drop_unclosed_last_bar(
            self.klines(code, frequency),
            frequency,
            time_label=getattr(self.ex, "kline_time_label", "start"),
        )

    def closed_bar_as_of(self, code, frequency):
        """返回已收盘前缀末行的因果收盘时刻。"""

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
