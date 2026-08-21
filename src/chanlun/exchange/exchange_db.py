import datetime
from typing import Dict, List, Union

import pandas as pd
import pytz
from tzlocal import get_localzone

from chanlun import fun
from chanlun.market import Market
from chanlun.persistence.db import db
from chanlun.exchange.exchange import Exchange, Tick
from chanlun.exchange.exchange_binance_common import BINANCE_SUPPORTED_FREQUENCIES
from chanlun.exchange.kline_precision import normalize_kline_precision


class ExchangeDB(Exchange):
    """从本地数据库读取 K 线的行情适配器，不支持实时 Tick 和交易接口。"""

    def __init__(self, market):
        """
        :param market: 市场标识，如 'a'/'hk'/'us'/'currency'/'currency_spot'/'futures'
        """
        self.market = market
        # 美股使用东部时间，数字货币使用本机本地时区，其余默认上海时区
        self.tz = pytz.timezone("Asia/Shanghai")
        if self.market == "us":
            self.tz = pytz.timezone("US/Eastern")
        if self.market in ["currency", "currency_spot"]:
            self.tz = pytz.timezone(str(get_localzone()))

    def default_code(self):
        if self.market == Market.A.value:
            return "SH.000001"
        elif self.market == Market.HK.value:
            return "HK.00700"
        elif self.market == Market.FUTURES.value:
            return "KQ.m@SHFE.rb"
        elif self.market == Market.NY_FUTURES.value:
            return "CO.GC00W"
        elif self.market == Market.US.value:
            return "AAPL"
        elif self.market == Market.CURRENCY.value:
            return "BTC/USDT"
        elif self.market == Market.CURRENCY_SPOT.value:
            return "BTC/USDT"
        return ""

    def support_frequencys(self):
        if self.market == Market.A.value:
            return {
                "y": "Y",
                "m": "M",
                "w": "W",
                "d": "D",
                "120m": "120m",
                "60m": "60m",
                "30m": "30m",
                "15m": "15m",
                "10m": "10m",
                "5m": "5m",
            }
        elif self.market == Market.HK.value:
            return {
                "y": "Y",
                "q": "Q",
                "m": "M",
                "w": "W",
                "d": "D",
                "60m": "60m",
                "30m": "30m",
                "15m": "15m",
                "5m": "5m",
            }
        elif self.market == Market.FUTURES.value:
            return {
                "w": "W",
                "d": "D",
                "120m": "2H",
                "60m": "1H",
                "30m": "30m",
                "15m": "15m",
                "10m": "10m",
                "5m": "5m",
                "1m": "1m",
            }
        elif self.market == Market.NY_FUTURES.value:
            return {
                "w": "W",
                "d": "D",
                "120m": "2H",
                "60m": "1H",
                "30m": "30m",
                "15m": "15m",
                "10m": "10m",
                "5m": "5m",
                "1m": "1m",
            }
        elif self.market == Market.US.value:
            return {
                "w": "Week",
                "d": "Day",
                "60m": "60m",
                "30m": "30m",
                "10m": "10m",
                "15m": "15m",
                "5m": "5m",
            }
        elif self.market in (
            Market.CURRENCY.value,
            Market.CURRENCY_SPOT.value,
        ):
            return dict(BINANCE_SUPPORTED_FREQUENCIES)
        return {"d": "D", "30m": "30m"}

    def query_last_datetime(self, code, frequency) -> Union[None, str]:
        """查询指定标的和周期在数据库中的最后一条 K 线时间，用于增量同步判断。"""
        return db.klines_last_datetime(self.market, code, frequency)

    def insert_klines(self, code, frequency, klines):
        """批量写入 K 线数据到数据库。"""
        db.klines_insert(self.market, code, frequency, klines)
        return True

    def del_klines_by_code_freq(self, code, freq):
        db.klines_delete(self.market, code, frequency=freq)
        return

    def klines(
        self,
        code: str,
        frequency: str,
        start_date: str = None,
        end_date: str = None,
        args=None,
    ) -> Union[pd.DataFrame, None]:
        if args is None:
            args = {}

        limit = 5000
        if "limit" in args.keys():
            limit = args["limit"]
        order = "desc"
        if "order" in args.keys():
            order = args["order"]

        # 同时指定起止时间时不限制条数，否则可能截断历史区间
        if start_date is not None and end_date is not None and "limit" not in args:
            limit = None
        if start_date is not None:
            start_date = fun.str_to_datetime(start_date)
        if end_date is not None:
            end_date = fun.str_to_datetime(end_date)
        klines = db.klines_query(
            self.market, code, frequency, start_date, end_date, limit, order
        )
        kline_pd = []
        for _k in klines:
            _kline = {
                "code": _k.code,
                "date": _k.dt,
                "open": _k.o,
                "high": _k.h,
                "low": _k.l,
                "close": _k.c,
                "volume": _k.v,
            }
            if self.market == Market.FUTURES.value:
                _kline["position"] = _k.p
            kline_pd.append(_kline)
        if len(kline_pd) == 0:
            kline_pd = pd.DataFrame(
                [], columns=["date", "code", "high", "low", "open", "close", "volume"]
            )
            return kline_pd

        kline_pd = pd.DataFrame(kline_pd)
        kline_pd["code"] = code
        kline_pd["date"] = pd.to_datetime(kline_pd["date"]).dt.tz_localize(
            self.tz, ambiguous=True, nonexistent="shift_forward"
        )  # nonexistent: 货币7x24在DST服务器上春令时不存在墙钟(如US/Eastern 02:00-03:00)前移防崩
        if frequency in {"y", "q", "m", "w", "d"}:
            # 仅日级及以上做 00:00 到收盘时刻的规整，沿用期货适配器的相同处理。
            # 分钟级放行:期货夜盘跨零点品种(AU/AG/SC 至 02:30 等)的 00:00 bar 是真实数据,
            # 无差别改写会与 09:00 日盘 bar 重复时间戳/夜盘 OHLC 错插早盘时间轴。
            kline_pd["date"] = kline_pd["date"].apply(self.__convert_date)
        kline_pd.sort_values(by="date", inplace=True)
        kline_pd = kline_pd.reset_index(drop=True)

        kline_pd = normalize_kline_precision(kline_pd, self.market, code)
        return kline_pd

    def __convert_date(self, dt: datetime.datetime):
        """将数据库中 00:00 的日级时间戳规整为各市场的收盘时刻，便于与分钟级数据时间轴对齐。"""
        if self.market == Market.A.value:
            if dt.hour == 0 and dt.minute == 0:
                return dt.replace(hour=15, minute=0)
        if self.market == Market.HK.value:
            if dt.hour == 0 and dt.minute == 0:
                return dt.replace(hour=16, minute=0)
        if self.market == Market.FUTURES.value:
            if dt.hour == 0 and dt.minute == 0:
                return dt.replace(hour=9, minute=0)
        if self.market == Market.US.value:
            if dt.hour == 0 and dt.minute == 0:
                return dt.replace(hour=9, minute=30)
        return dt

    def all_stocks(self):
        return []

    def now_trading(self, market: str):
        pass

    def ticks(self, codes: List[str]) -> Dict[str, Tick]:
        """用最新一根日线 K 线模拟 Tick，ExchangeDB 无实时行情接口。"""
        ticks = {}
        for _code in codes:
            klines = self.klines(_code, "d", args={"limit": 1})
            if len(klines) == 0:
                continue
            ticks[_code] = Tick(
                _code,
                klines.iloc[-1]["close"],
                klines.iloc[-1]["close"],
                klines.iloc[-1]["close"],
                klines.iloc[-1]["high"],
                klines.iloc[-1]["low"],
                klines.iloc[-1]["open"],
                klines.iloc[-1]["volume"],
                0,
            )
        return ticks

    def stock_info(self, code: str) -> Dict:
        return {
            "code": code,
            "name": code,
        }

    def stock_owner_plate(self, code: str):
        pass

    def plate_stocks(self, code: str):
        pass

    def balance(self):
        pass

    def positions(self, code: str = ""):
        pass

    def order(self, code: str, o_type: str, amount: float, args=None):
        pass
