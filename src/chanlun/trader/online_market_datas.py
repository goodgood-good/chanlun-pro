"""
线上行情数据获取对象，用于实盘交易执行
"""

from typing import List, Dict

import pandas as pd

from chanlun.backtesting.base import MarketDatas
from chanlun.core.cl_interface import ICL
from chanlun.exchange.exchange import Exchange
from chanlun.file_db import FileCacheDB


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
            开启时同一根 K 线在一次循环内只请求一次，循环结束后须调用 clear_cache() 清除，
            否则下次循环仍会读到旧数据。
        """
        super().__init__(market, frequencys, cl_config)
        self.ex = ex
        self.fdb = FileCacheDB()

        self.use_cache = use_cache

        # key 为 "{code}_{frequency}"，循环结束需显式清除
        self.cache_klines: Dict[str, pd.DataFrame] = {}

    def clear_cache(self):
        """每次实盘循环结束后调用，清空 K 线缓存以便下次取到最新行情。"""
        self.cache_klines = {}
        return True

    def klines(self, code, frequency) -> pd.DataFrame:
        """获取 K 线数据；use_cache=True 时循环内复用缓存，避免重复请求。"""
        key = f"{code}_{frequency}"
        if self.use_cache and key in self.cache_klines.keys():
            return self.cache_klines[key]
        klines = self.ex.klines(code, frequency)
        if self.use_cache:
            self.cache_klines[key] = klines
        return klines

    def last_k_info(self, code) -> dict:
        klines = self.klines(code, self.frequencys[-1])
        return {
            "date": klines.iloc[-1]["date"],
            "open": float(klines.iloc[-1]["open"]),
            "close": float(klines.iloc[-1]["close"]),
            "high": float(klines.iloc[-1]["high"]),
            "low": float(klines.iloc[-1]["low"]),
        }

    def get_cl_data(self, code, frequency, cl_config: dict = None) -> ICL:
        """返回指定标的+周期的缠论计算结果；cl_config 优先级：code > frequency > 'default' > 全局。"""
        # 支持按标的或周期单独配置缠论参数，优先级依次降低
        if code in self.cl_config.keys():
            cl_config = self.cl_config[code]
        elif frequency in self.cl_config.keys():
            cl_config = self.cl_config[frequency]
        elif "default" in self.cl_config.keys():
            cl_config = self.cl_config["default"]
        else:
            cl_config = self.cl_config

        klines = self.klines(code, frequency)

        cd = self.fdb.get_web_cl_data(self.market, code, frequency, cl_config, klines)
        return cd
