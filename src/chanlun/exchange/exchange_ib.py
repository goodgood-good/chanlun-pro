import datetime
import json
import uuid
from enum import Enum
from typing import Dict, List, Union

import pandas as pd
import pytz
from tenacity import retry, stop_after_attempt, wait_random, retry_if_result

from chanlun import fun, rd
from chanlun.exchange.exchange import Exchange, Tick, convert_us_kline_frequency
from chanlun.exchange.kline_precision import normalize_kline_precision

ib_res_hkey = "ib_data_results"


class CmdEnum(Enum):
    SEARCH_STOCKS = "ib_search_stocks"
    KLINES = "ib_klines"
    TICKS = "ib_ticks"
    STOCK_INFO = "ib_stock_info"
    BALANCE = "ib_balance"
    POSITIONS = "ib_positions"
    ORDERS = "ib_orders"


@fun.singleton
class ExchangeIB(Exchange):
    def __init__(self):
        self.tz = pytz.timezone("US/Eastern")

        # 本地内存缓存 search_stocks / stock_info，避免对同一 code 重复走 Redis 消息队列
        self.cache = {}

    @staticmethod
    def uid():
        return f"{ib_res_hkey}_{str(uuid.uuid4())}"

    def default_code(self) -> str:
        return "AAPL"

    def support_frequencys(self) -> dict:
        return {
            "m": "Month",
            "w": "Week",
            "d": "Day",
            "60m": "60m",
            "30m": "30m",
            "10m": "10m",
            "15m": "15m",
            "5m": "5m",
            "2m": "2m",
            "1m": "1m",
        }

    def all_stocks(self):
        """
        TODO IB 不提供获取全部代码的接口
        """
        return []

    def search_stocks(self, search: str):
        """
        补充 获取所有 股票的代码，IB 提供按照关键字进行搜索的接口
        """
        if f"search_stock_{search}" in self.cache.keys():
            return self.cache[f"search_stock_{search}"]

        args = {"key": self.uid(), "search": search}
        rd.Robj().lpush(CmdEnum.SEARCH_STOCKS.value, json.dumps(args))
        res = rd.Robj().brpop([args["key"]], 30)
        if res is None:
            return []
        res = json.loads(res[1])
        self.cache[f"search_stock_{search}"] = res
        return res

    def now_trading(self, market: str):
        """
        TODO 暂时还没有找到接口，直接硬编码
        周一致周五，美国东部时间，9:30 - 16:00
        """
        tz = pytz.timezone("US/Eastern")
        now = datetime.datetime.now(tz)
        weekday = now.weekday()
        hour = now.hour
        minute = now.minute
        if weekday in [0, 1, 2, 3, 4] and (
            (10 <= hour < 16) or (hour == 9 and minute >= 30)
        ):
            return True
        return False

    @retry(
        stop=stop_after_attempt(2),
        wait=wait_random(min=1, max=5),
        retry=retry_if_result(lambda _r: _r is None),
    )
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

        frequency_map = {
            "m": "1 month",
            "w": "1 week",
            "d": "1 day",
            "60m": "1 hour",
            "30m": "30 mins",
            "10m": "10 mins",
            "15m": "15 mins",
            "5m": "5 mins",
            "2m": "1 min",
            "1m": "1 min",
        }

        # IB 每次请求的回看时长上限，超出会被服务端拒绝
        duration_map = {
            "m": "30 Y",
            "w": "20 Y",
            "d": "10 Y",
            "60m": "360 D",
            "30m": "100 D",
            "10m": "30 D",
            "15m": "30 D",
            "5m": "15 D",
            "2m": "6 D",
            "1m": "3 D",
        }

        duration = (
            duration_map[frequency]
            if "duration" not in args.keys()
            else args["duration"]
        )
        timeout = 60 if "timeout" not in args.keys() else args["timeout"]

        args = {
            "key": self.uid(),
            "code": code,
            "durationStr": duration,
            "barSizeSetting": frequency_map[frequency],
            "timeout": timeout,
        }
        rd.Robj().lpush(CmdEnum.KLINES.value, json.dumps(args))
        bars = rd.Robj().brpop([args["key"]], timeout)
        if bars is None or len(bars) == 0:
            return None
        bars: dict = json.loads(bars[1])
        klines_df = pd.DataFrame(bars)
        if len(klines_df) > 0:
            klines_df["date"] = pd.to_datetime(klines_df["date"]).dt.tz_localize(
                self.tz
            )

        if len(klines_df) > 0 and frequency in ["2m"]:
            klines_df = convert_us_kline_frequency(klines_df, "2m")

        if len(klines_df) == 0:
            return None

        # 向量化 __convert_date：00:00:00 → 09:30:00
        # date 列此时为 tz-aware(US/Eastern, 有 DST)。直接对 tz-aware 列做
        # Timedelta 算术是在 UTC 上加偏移, 跨 DST 切换日会与原 .apply(replace())
        # 的"墙钟字段替换"语义偏差 1 小时。故先转 naive 墙钟时间做算术, 再 localize,
        # 保持与逐元素 replace(hour=9, minute=30) 完全等价。
        _date_naive = klines_df["date"].dt.tz_localize(None)
        _midnight_mask = (
            (_date_naive.dt.hour == 0)
            & (_date_naive.dt.minute == 0)
            & (_date_naive.dt.second == 0)
        )
        _date_naive = _date_naive.mask(
            _midnight_mask, _date_naive + pd.Timedelta(hours=9, minutes=30)
        )
        klines_df["date"] = _date_naive.dt.tz_localize(self.tz)

        klines_df = normalize_kline_precision(klines_df, "us", code)
        return klines_df

    def ticks(self, codes: List[str]) -> Dict[str, Tick]:
        ticks = {}
        args = {"key": self.uid(), "codes": codes}
        rd.Robj().lpush(CmdEnum.TICKS.value, json.dumps(args))

        tks = rd.Robj().brpop([args["key"]], timeout=0)
        if tks is None:
            return {}
        tks: dict = json.loads(tks[1])
        for tk in tks:
            if tk is None:
                continue
            ticks[tk["code"]] = Tick(
                code=tk["code"],
                last=tk["last"],
                buy1=tk["buy1"],
                sell1=tk["sell1"],
                open=tk["open"],
                high=tk["high"],
                low=tk["low"],
                volume=tk["volume"],
                rate=tk["rate"],
            )
        return ticks

    def stock_info(self, code: str) -> Union[Dict, None]:
        if f"stock_info_{code}" in self.cache.keys():
            return self.cache[f"stock_info_{code}"]

        args = {"key": self.uid(), "code": code}
        rd.Robj().lpush(CmdEnum.STOCK_INFO.value, json.dumps(args))
        res = rd.Robj().brpop([args["key"]], timeout=30)
        if res is None:
            return None
        res = json.loads(res[1])
        self.cache[f"stock_info_{code}"] = res
        return res

    def stock_owner_plate(self, code: str):
        pass

    def plate_stocks(self, code: str):
        pass

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_random(min=1, max=5),
        retry=retry_if_result(lambda _r: _r is None),
    )
    def balance(self):
        # 获取当前资产
        args = {"key": self.uid()}
        rd.Robj().lpush(CmdEnum.BALANCE.value, json.dumps(args))

        balance = rd.Robj().brpop(args["key"], timeout=30)
        if balance is None:
            return None
        return json.loads(balance[1])

    def positions(self, code: str = ""):
        """获取当前持仓。"""
        args = {"key": self.uid(), "code": code}
        rd.Robj().lpush(CmdEnum.POSITIONS.value, json.dumps(args))

        positions = rd.Robj().brpop(args["key"], timeout=30)
        if positions is None:
            return None
        return json.loads(positions[1])

    def order(self, code: str, o_type: str, amount: float, args=None):
        """
        订单操作
        """
        args = {"key": self.uid(), "code": code, "type": o_type, "amount": amount}
        rd.Robj().lpush(CmdEnum.ORDERS.value, json.dumps(args))

        res = rd.Robj().brpop([args["key"]], 0)
        if res is None:
            return False
        return json.loads(res[1])
