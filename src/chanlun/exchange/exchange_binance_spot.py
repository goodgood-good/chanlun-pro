import datetime
from typing import Dict, List, Union

import ccxt
import pandas as pd
import pytz
from tenacity import retry, retry_if_result, stop_after_attempt, wait_random
from tzlocal import get_localzone

from chanlun import fun
from chanlun.market import Market
from chanlun.exchange.exchange import Exchange, Tick, convert_currency_kline_frequency
from chanlun.exchange.exchange_binance_common import (
    BINANCE_KLINE_TIMEFRAMES,
    BINANCE_SUPPORTED_FREQUENCIES,
    BINANCE_SYNTHETIC_FREQUENCIES,
    configure_spot_public_market_data,
    normalize_binance_kline_frame,
)
from chanlun.exchange.exchange_db import ExchangeDB
from chanlun.utils import config_get_proxy


@fun.singleton
class ExchangeBinanceSpot(Exchange):
    """
    数字货币交易所接口(现货交易)
    """

    g_all_stocks = []

    def __init__(self):
        # This adapter is market-data only.  Limiting CCXT to Spot is important:
        # ccxt.binance otherwise loads Spot + USD-M + COIN-M exchange metadata,
        # so a blocked futures endpoint can make the working public Spot host fail.
        params = {
            "options": {
                "defaultType": "spot",
                "fetchCurrencies": False,
                "fetchMarkets": {"types": ["spot"]},
            }
        }

        proxy = config_get_proxy()

        if proxy["host"] != "":
            params["proxies"] = {
                "https": f"http://{proxy['host']}:{proxy['port']}",
                "http": f"http://{proxy['host']}:{proxy['port']}",
            }

        # Public Spot quotes/K-lines need no account credentials.  Do not load
        # API keys into this process; the adapter has no private trading methods.
        self.exchange = configure_spot_public_market_data(ccxt.binance(params))

        self.db_exchange = ExchangeDB(Market.CURRENCY_SPOT.value)

        # 使用本机时区，与 ExchangeBinance(合约) 保持一致
        self.tz = pytz.timezone(str(get_localzone()))

    def default_code(self):
        return "BTC/USDT"

    def support_frequencys(self):
        return dict(BINANCE_SUPPORTED_FREQUENCIES)

    def now_trading(self, market: str):
        """
        返回交易时间，数字货币 24 小时可交易
        """
        return True

    def stock_info(self, code: str) -> Union[Dict, None]:
        """
        数字货币全部返回 code 值
        """
        all_stocks = self.all_stocks()
        for _s in all_stocks:
            if _s["code"] == code:
                return _s

    def all_stocks(self):
        """
        返回所有交易对儿
        """
        if len(self.g_all_stocks) > 0:
            return self.g_all_stocks

        markets = self.exchange.load_markets(reload=True)
        __all_stocks = []
        for _, s in markets.items():
            if s["active"] and s["quote"] == "USDT":
                __all_stocks.append(
                    {
                        "code": s["base"] + "/" + s["quote"],
                        "name": s["base"] + "/" + s["quote"],
                        "precision": fun.reverse_decimal_to_power_of_ten(
                            s["precision"]["price"]
                        ),
                    }
                )
        self.g_all_stocks = __all_stocks
        return self.g_all_stocks

    @retry(
        stop=stop_after_attempt(3),
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
        """
        返回 k 线数据
        优先从数据库中获取，在进行 api 请求，合并数据，并更新数据库，之后返回k线行情
        可以减少网络请求，优化 vpn 使用流量
        """
        if args is None:
            args = {}

        if "use_online" in args.keys() and args["use_online"]:
            return self.online_klines(code, frequency, start_date, end_date, args)

        try:
            db_klines = self.db_exchange.klines(code, frequency, args={"limit": 10000})
            if len(db_klines) == 0:
                online_klines = self.increment_klines_by_online(
                    code, frequency, start_date=None
                )
                # 退市/下架/全新无数据交易对: 在线零 bar 返回 None, 不能喂 insert_klines
                # (None.empty AttributeError→except吞→RetryError), 如实返回空(对齐 tdx 家族)
                if online_klines is None or len(online_klines) == 0:
                    return normalize_binance_kline_frame(
                        pd.DataFrame(), market="currency_spot", code=code
                    )
                self.db_exchange.insert_klines(code, frequency, online_klines)
                online_klines = normalize_binance_kline_frame(
                    online_klines, market="currency_spot", code=code
                )
                return online_klines
            else:
                # 取倒数第二条作为增量起点，让最后一根未收盘 bar 也能被覆盖更新
                # 库中恰1根时退用末根: iloc[-2]会IndexError被except吞->重试3次->永久RetryError
                _inc_from = db_klines.iloc[-2] if len(db_klines) >= 2 else db_klines.iloc[-1]
                last_datetime = _inc_from["date"].strftime("%Y-%m-%d %H:%M:%S")
                online_klines = self.increment_klines_by_online(
                    code, frequency, start_date=last_datetime
                )
                # 退市/下架: 增量零 bar 返回 None, 用库中现有数据兜底, 不喂 None 给 insert
                if online_klines is None or len(online_klines) == 0:
                    # Avoid concatenating an empty/all-NA frame.  Besides doing
                    # no useful work, pandas is deprecating the dtype inference
                    # used by that path.  A missing increment means the cached
                    # history itself is the complete result for this request.
                    klines = db_klines.copy()
                else:
                    self.db_exchange.insert_klines(code, frequency, online_klines)
                    klines = pd.concat(
                        [db_klines, online_klines], ignore_index=True
                    )
            klines.drop_duplicates(subset=["date"], keep="last", inplace=True)
            klines = klines.sort_values(by="date", ascending=True)
            klines = normalize_binance_kline_frame(
                klines[-10000::], market="currency_spot", code=code
            )
            return klines
        except Exception as e:
            print(f"{code} - {frequency} Error : {e}")

        return None

    def increment_klines_by_online(
        self,
        code: str,
        frequency: str,
        start_date: str = None,
        args=None,
    ) -> Union[pd.DataFrame, None]:
        """
        增量 API 接口请求行情数据

        Args:
            code: 交易对代码
            frequency: K线周期
            start_date: 开始日期，格式为 "YYYY-MM-DD HH:MM:SS"
            end_date: 结束日期，格式为 "YYYY-MM-DD HH:MM:SS"
            args: 额外参数

        Returns:
            pd.DataFrame: 包含K线数据的DataFrame，如果出错则返回None

        说明:
            - 如果start_date为空，则从最新数据往前获取，直到获取10000根或返回不足1000根
            - 如果start_date有值，则从该时间点开始往后获取，直到获取到最新数据
        """
        # 币安原生周期直接读取；10m/2m/3h 用基础周期拉取后在本地合成。
        if args is None:
            args = {}
        if frequency not in BINANCE_KLINE_TIMEFRAMES:
            raise Exception(f"不支持的周期: {frequency}")

        start_timestamp = None

        if start_date is not None:
            start_timestamp = (
                int(
                    datetime.datetime.timestamp(
                        datetime.datetime.strptime(start_date, "%Y-%m-%d %H:%M:%S")
                    )
                )
                * 1000
            )

        all_klines = []
        target_count = 10000

        if start_date is None:
            # 无起点时从最新数据向前翻页，累积至 target_count 根或历史耗尽
            current_end = None
            while len(all_klines) < target_count:
                params = {}
                if current_end is not None:
                    params["endTime"] = current_end
                kline = self.exchange.fetch_ohlcv(
                    symbol=code,
                    timeframe=BINANCE_KLINE_TIMEFRAMES[frequency],
                    limit=1000,
                    params=params,
                )
                if len(kline) < 1000:
                    all_klines = kline + all_klines
                    break

                # 下一页的结束时间 = 当前页最早一条的时间戳
                current_end = kline[0][0]

                all_klines = kline + all_klines

                if len(all_klines) >= target_count:
                    break
        else:
            # 有起点时从指定时间向后翻页，直到数据量不足一页（已到最新）
            current_start = start_timestamp

            while True:
                params = {"startTime": current_start}

                kline = self.exchange.fetch_ohlcv(
                    symbol=code,
                    timeframe=BINANCE_KLINE_TIMEFRAMES[frequency],
                    limit=1000,
                    params=params,
                )

                if len(kline) < 1000:
                    all_klines.extend(kline)
                    break

                all_klines.extend(kline)

                # 下一页的起始时间 = 当前页最后一条的时间戳
                current_start = kline[-1][0]

        if len(all_klines) == 0:
            return None

        kline_pd = pd.DataFrame(
            all_klines, columns=["date", "open", "high", "low", "close", "volume"]
        )
        kline_pd["code"] = code
        kline_pd["date"] = pd.to_datetime(kline_pd["date"], unit="ms", utc=True).dt.tz_convert(self.tz)
        kline_pd = kline_pd[["code", "date", "open", "close", "high", "low", "volume"]]
        kline_pd.drop_duplicates(subset=["date"], keep="last", inplace=True)

        if frequency in BINANCE_SYNTHETIC_FREQUENCIES and len(kline_pd) > 0:
            kline_pd = convert_currency_kline_frequency(kline_pd, frequency)

        return kline_pd

    def online_klines(
        self,
        code: str,
        frequency: str,
        start_date: str = None,
        end_date: str = None,
        args=None,
    ) -> Union[pd.DataFrame, None]:
        """直接请求币安现货 API 获取 K 线，不走本地数据库缓存，单次最多 1000 根。"""
        if args is None:
            args = {}
        if frequency not in BINANCE_KLINE_TIMEFRAMES:
            raise Exception(f"不支持的周期: {frequency}")

        if start_date is not None:
            start_date = (
                int(
                    datetime.datetime.timestamp(
                        datetime.datetime.strptime(start_date, "%Y-%m-%d %H:%M:%S")
                    )
                )
                * 1000
            )
        if end_date is not None:
            end_date = (
                int(
                    datetime.datetime.timestamp(
                        datetime.datetime.strptime(end_date, "%Y-%m-%d %H:%M:%S")
                    )
                )
                * 1000
            )
        params = {}
        if start_date is not None:
            params["startTime"] = start_date
        if end_date is not None:
            params["endTime"] = end_date

        kline = self.exchange.fetch_ohlcv(
            symbol=code,
            timeframe=BINANCE_KLINE_TIMEFRAMES[frequency],
            limit=1000,
            params=params,
        )
        kline_pd = pd.DataFrame(
            kline, columns=["date", "open", "high", "low", "close", "volume"]
        )
        kline_pd["code"] = code
        kline_pd["date"] = pd.to_datetime(kline_pd["date"], unit="ms", utc=True).dt.tz_convert(self.tz)
        kline_pd = kline_pd[["code", "date", "open", "close", "high", "low", "volume"]]
        if frequency in BINANCE_SYNTHETIC_FREQUENCIES and len(kline_pd) > 0:
            kline_pd = convert_currency_kline_frequency(kline_pd, frequency)
        kline_pd = normalize_binance_kline_frame(
            kline_pd, market="currency_spot", code=code
        )
        return kline_pd

    def ticks(self, codes: List[str]) -> Dict[str, Tick]:
        res_ticks = {}
        _ts = self.exchange.fetch_tickers(codes)
        for _s, _t in _ts.items():
            if _t["last"] is None or _t["bid"] is None or _t["ask"] is None:
                continue
            res_ticks[_s] = Tick(
                code=_s,
                last=_t["last"],
                buy1=_t["bid"],
                sell1=_t["ask"],
                high=_t["high"],
                low=_t["low"],
                open=_t["open"],
                volume=_t["quoteVolume"],
                rate=_t["percentage"],
            )

        return res_ticks

    def balance(self):
        raise RuntimeWarning("交易接口未实现")

    def positions(self, code: str = ""):
        raise RuntimeWarning("交易接口未实现")

    def order(self, code: str, o_type: str, amount: float, args=None):
        raise RuntimeWarning("交易接口未实现")

    def stock_owner_plate(self, code: str):
        raise Exception("交易所不支持")

    def plate_stocks(self, code: str):
        raise Exception("交易所不支持")
