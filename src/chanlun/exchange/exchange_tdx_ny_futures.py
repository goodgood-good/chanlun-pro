import datetime
import traceback
from typing import Dict, List, Union

import pandas as pd
import pytz
from pytdx.errors import TdxConnectionError
from pytdx.exhq import TdxExHq_API
from tenacity import retry, retry_if_result, stop_after_attempt, wait_random

from chanlun import fun
from chanlun.market import Market
from chanlun.persistence.db import db
from chanlun.exchange.exchange import Exchange, Tick
from chanlun.exchange.kline_precision import normalize_kline_precision
from chanlun.persistence.file_db import FileCacheDB
from chanlun.tools import tdx_best_ip as best_ip


@fun.singleton
class ExchangeTDXNYFutures(Exchange):
    """通达信纽约期货行情适配器（market=16/17，category=3）。"""

    g_all_stocks = []

    # 连接超时与 ExchangeTDXFutures 保持一致；auto_retry=False 避免 pytdx 内部叠加重试导致 60s+ 卡顿
    _CONNECT_TIMEOUT = 5
    _INIT_MAX_RETRY = 2

    def __init__(self):
        self.tz = pytz.timezone("Asia/Shanghai")
        self.fdb = FileCacheDB()

        # @fun.singleton 缓存实例，init_failed 让调用方短路而不是再次触发超时
        self.init_failed = False
        self.market_maps = {}

        try:
            self.connect_info = db.cache_get("tdxex_connect_ip")
            if self.connect_info is None:
                self.connect_info = self.reset_tdx_ip()

            retry_count = 0
            last_error = None
            while retry_count < self._INIT_MAX_RETRY:
                try:
                    client = TdxExHq_API(raise_exception=True, auto_retry=False)
                    with client.connect(
                        self.connect_info["ip"],
                        self.connect_info["port"],
                        time_out=self._CONNECT_TIMEOUT,
                    ):
                        all_markets = client.get_markets()
                        for _m in all_markets:
                            if _m["category"] == 3 and _m["market"] in [16, 17]:
                                self.market_maps[_m["short_name"]] = {
                                    "market": _m["market"],
                                    "category": _m["category"],
                                    "name": _m["name"],
                                }
                    break
                except TdxConnectionError as e:
                    last_error = e
                    retry_count += 1
                    if retry_count < self._INIT_MAX_RETRY:
                        self.reset_tdx_ip()
            else:
                raise last_error if last_error else RuntimeError("TDX init retry exhausted")
        except Exception:
            self.init_failed = True
            print(traceback.format_exc())
            print("通达信 期货行情接口初始化失败，期货行情不可用")

    def reset_tdx_ip(self):
        """重新选择 TDX 最优服务器并写入缓存。"""
        connect_info = best_ip.select_best_ip("future")
        connect_info = {"ip": connect_info["ip"], "port": int(connect_info["port"])}
        db.cache_set("tdxex_connect_ip", connect_info)
        self.connect_info = connect_info
        return connect_info

    def default_code(self):
        return "CO.GC00W"

    def support_frequencys(self):
        return {
            "y": "Y",
            "m": "M",
            "w": "W",
            "d": "D",
            "60m": "60m",
            "30m": "30m",
            "15m": "15m",
            "5m": "5m",
            "1m": "1m",
        }

    def all_stocks(self):
        """获取纽约期货标的列表（category=3，market 16/17），过滤无 tick 标的。"""
        if len(self.g_all_stocks) > 0:
            return self.g_all_stocks

        __all_stocks = []
        client = TdxExHq_API(raise_exception=True, auto_retry=True)
        with client.connect(self.connect_info["ip"], self.connect_info["port"]):
            start_i = 0
            count = 1000
            market_map_short_names = {
                _m_i["market"]: _m_s for _m_s, _m_i in self.market_maps.items()
            }
            while True:
                instruments = client.get_instrument_info(start_i, count)
                for _i in instruments:
                    if (
                        _i["category"] != 3
                        or _i["market"] not in market_map_short_names.keys()
                    ):
                        continue

                    __all_stocks.append(
                        {
                            "code": f"{market_map_short_names[_i['market']]}.{_i['code']}",
                            "name": _i["name"],
                        }
                    )
                start_i += count
                if len(instruments) < count:
                    break

        # 过滤无实时 tick 的标的（已下市合约），避免无效请求
        ticks = self.all_ticks()
        tick_codes = [_c for _c, _t in ticks.items()]
        __all_stocks = [_s for _s in __all_stocks if _s["code"] in tick_codes]

        self.g_all_stocks = __all_stocks
        return self.g_all_stocks

    def to_tdx_code(self, code):
        """将 "CO.GC00W" 格式代码拆分为 (market_int, tdx_code)。"""
        code_infos = code.split(".")
        market_info = self.market_maps[code_infos[0]]
        return market_info["market"], code_infos[1]

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
        """通达信不支持按时间区间查找，分页拉取后与文件缓存合并去重；夜盘时间由 fix_yp_date 修正。"""
        if args is None:
            args = {}
        if "pages" not in args.keys():
            args["pages"] = 8
        else:
            args["pages"] = int(args["pages"])

        frequency_map = {
            "y": 11,
            "q": 10,
            "m": 6,
            "w": 5,
            "d": 9,
            "60m": 3,
            "30m": 2,
            "15m": 1,
            "5m": 0,
            "1m": 8,
        }
        market, tdx_code = self.to_tdx_code(code)
        if market is None:
            print("不支持的调用参数")
            return None
        # 通达信不支持按时间区间查询；收到 start_date/end_date 时忽略它们，
        # 仍按“分页拉最新 + 文件缓存合并”返回（tv_history 后续会按窗口切片）。

        try:
            client = TdxExHq_API(raise_exception=True, auto_retry=True)
            with client.connect(self.connect_info["ip"], self.connect_info["port"]):
                klines: pd.DataFrame = self.fdb.get_tdx_klines(
                    Market.NY_FUTURES.value, code, frequency
                )
                if klines is None:
                    klines = pd.concat(
                        [
                            client.to_df(
                                client.get_instrument_bars(
                                    frequency_map[frequency],
                                    market,
                                    tdx_code,
                                    (i - 1) * 700,
                                    700,
                                )
                            )
                            for i in range(1, args["pages"] + 1)
                        ],
                        axis=0,
                        sort=False,
                    )
                    if len(klines) == 0:
                        return pd.DataFrame([])
                    klines.loc[:, "date"] = pd.to_datetime(klines["datetime"])
                    klines["date"] = klines.apply(
                        lambda x: self.fix_yp_date(code, x["date"]), axis=1
                    )
                    klines.sort_values("date", inplace=True)
                else:
                    for i in range(1, args["pages"] + 1):
                        _ks = client.to_df(
                            client.get_instrument_bars(
                                frequency_map[frequency],
                                market,
                                tdx_code,
                                (i - 1) * 700,
                                700,
                            )
                        )
                        _ks.loc[:, "date"] = pd.to_datetime(_ks["datetime"])
                        _ks["date"] = _ks.apply(
                            lambda x: self.fix_yp_date(code, x["date"]), axis=1
                        )
                        _ks.sort_values("date", inplace=True)
                        new_start_dt = _ks.iloc[0]["date"]
                        cache_end_dt = klines.iloc[-1]["date"] if i == 1 else cache_end_dt  # B1(Round8): 仅首轮拼接前捕获缓存真实末尾, 防每轮重算致陈旧>1400根中间段留洞
                        klines = pd.concat([klines, _ks], ignore_index=True)
                        # 新一页起始时间早于缓存末尾，说明已覆盖，无需继续
                        if cache_end_dt >= new_start_dt:
                            break

            # 去重：分页重叠时保留最新一条
            klines = klines.drop_duplicates(["date"], keep="last").sort_values("date")
            self.fdb.save_tdx_klines(Market.NY_FUTURES.value, code, frequency, klines)

            klines.loc[:, "code"] = code
            klines.loc[:, "volume"] = klines["trade"]
            klines.loc[:, "date"] = pd.to_datetime(klines["date"]).dt.tz_localize(
                self.tz
            )
            klines.sort_values("date", inplace=True)

            klines[["volume"]] = klines[["volume"]].astype(float)

            klines = normalize_kline_precision(klines[["code", "date", "open", "close", "high", "low", "volume"]], "ny_futures", code)
            return klines
        except TdxConnectionError:
            self.reset_tdx_ip()
        except Exception as e:
            print(f"获取行情异常 {code} Exception ：{str(e)}")
            traceback.print_exc()
        finally:
            pass
        return None

    @staticmethod
    def fix_yp_date(code: str, dt: datetime.datetime):
        """纽约期货 00-05 点数据归属次一交易日（TDX 夜盘时间错位修正）。"""
        if dt.hour in [0, 1, 2, 3, 4, 5]:
            dt = dt + datetime.timedelta(days=1)
        return dt

    def stock_info(self, code: str) -> Union[Dict, None]:
        """获取纽约期货标的名称。"""
        all_stock = self.all_stocks()
        stock = [_s for _s in all_stock if _s["code"] == code]
        if not stock:
            return None
        return {"code": stock[0]["code"], "name": stock[0]["name"]}

    def ticks(self, codes: List[str]) -> Dict[str, Tick]:
        """获取纽约期货实时报价（通达信盘口接口）。"""
        ticks = {}
        client = TdxExHq_API(raise_exception=True, auto_retry=True)
        with client.connect(self.connect_info["ip"], self.connect_info["port"]):
            for _code in codes:
                _market, _tdx_code = self.to_tdx_code(_code)
                if _market is None:
                    continue
                _quote = client.get_instrument_quote(_market, _tdx_code)
                if len(_quote) > 0:
                    _quote = _quote[0]
                    ticks[_code] = Tick(
                        code=_code,
                        last=_quote["price"],
                        buy1=_quote["bid1"],
                        sell1=_quote["ask1"],
                        low=_quote["low"],
                        high=_quote["high"],
                        volume=_quote["zongliang"],
                        open=_quote["open"],
                        rate=(
                            round(
                                (_quote["price"] - _quote["pre_close"])
                                / _quote["price"]
                                * 100,
                                2,
                            )
                            if _quote["price"] > 0
                            else 0
                        ),
                    )
        return ticks

    def all_ticks(self) -> Dict[str, Tick]:
        ticks = {}
        client = TdxExHq_API(raise_exception=True, auto_retry=True)
        with client.connect(self.connect_info["ip"], self.connect_info["port"]):
            for _name, _mc in self.market_maps.items():
                _quotes = []
                _req_start = 0
                while True:
                    # count 固定 80(接口单次上限)。pytdx 解析期货报价时会 print 游标
                    # 位置(库漏删的调试输出)，由 app 启动时安装的 stdout 噪音过滤吞掉。
                    _qs = client.get_instrument_quote_list(
                        _mc["market"],
                        _mc["category"],
                        start=_req_start,
                        count=80,
                    )
                    _quotes.extend(_qs)
                    _req_start += 80
                    if len(_qs) < 80:
                        break
                for _quote in _quotes:
                    if _quote["MaiChu"] == 0.0 or _quote["ZongLiang"] == 0.0:
                        continue

                    ticks[f"{_name}.{_quote['code']}"] = Tick(
                        code=f"{_name}.{_quote['code']}",
                        last=_quote["MaiChu"],
                        buy1=_quote["MaiRuJia"],
                        sell1=_quote["MaiChuJia"],
                        low=_quote["ZuiDi"],
                        high=_quote["ZuiGao"],
                        volume=_quote["ZongLiang"],
                        open=_quote["JinKai"],
                        rate=(
                            round(
                                (_quote["MaiChu"] - _quote["ZuoJie"])
                                / _quote["MaiChu"]
                                * 100,
                                2,
                            )
                            if _quote["MaiChu"] > 0
                            else 0
                        ),
                    )
        return ticks

    def now_trading(self):
        """纽约期货近似 24 小时交易，始终返回 True。"""
        return True

    @staticmethod
    def __convert_date(dt: datetime.datetime):
        # 通达信日线后对齐，统一设为 00:00
        return dt.replace(hour=0, minute=0)

    def balance(self):
        raise Exception("交易所不支持")

    def positions(self, code: str = ""):
        raise Exception("交易所不支持")

    def order(self, code: str, o_type: str, amount: float, args=None):
        raise Exception("交易所不支持")

    def stock_owner_plate(self, code: str):
        raise Exception("交易所不支持")

    def plate_stocks(self, code: str):
        raise Exception("交易所不支持")


if __name__ == "__main__":
    ex = ExchangeTDXNYFutures()
    klines = ex.klines("CO.GC00W", "30m")
    print(len(klines))
    print(klines)