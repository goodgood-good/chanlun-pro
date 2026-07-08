import datetime
import time
import traceback
from typing import Dict, List, Union

import akshare as ak
import pandas as pd
import pytz
from pytdx.errors import TdxConnectionError
from pytdx.exhq import TdxExHq_API
from tenacity import retry, retry_if_result, stop_after_attempt, wait_random

from chanlun import fun
from chanlun.market import Market
from chanlun.config import get_data_path
from chanlun.persistence.db import db
from chanlun.exchange.exchange import Exchange, Tick
from chanlun.persistence.file_db import FileCacheDB
from chanlun.tools import tdx_best_ip as best_ip
from chanlun.exchange.kline_precision import normalize_kline_precision


@fun.singleton
class ExchangeTDXHK(Exchange):
    """通达信香港行情适配器。"""

    g_all_stocks = []

    # 网络不稳定时 socket.timeout 等异常无法被 TdxConnectionError 捕获，有限次换 IP 重试
    # 比 while True 更安全，防止初始化卡死或进入半初始化状态。
    _INIT_MAX_RETRY = 3

    def __init__(self):
        self.tz = pytz.timezone("Asia/Shanghai")
        self.fdb = FileCacheDB()

        # @fun.singleton 缓存实例，init_failed 让上层短路，预初始化字段防止半初始化状态 AttributeError
        self.init_failed = False
        self.connect_info = None
        self.market_maps = {}

        try:
            self.connect_info = db.cache_get("tdxex_connect_ip")
            if self.connect_info is None:
                self.connect_info = self.reset_tdx_ip()

            last_error = None
            for retry_count in range(self._INIT_MAX_RETRY):
                try:
                    client = TdxExHq_API(raise_exception=True, auto_retry=True)
                    with client.connect(
                        self.connect_info["ip"], self.connect_info["port"]
                    ):
                        all_markets = client.get_markets()
                        for _m in all_markets:
                            if _m["category"] == 2:
                                self.market_maps[_m["short_name"]] = {
                                    "market": _m["market"],
                                    "category": _m["category"],
                                    "name": _m["name"],
                                }
                    last_error = None
                    break
                except Exception as e:
                    # socket.timeout 等也要消化掉，否则整个 __init__ 失败
                    last_error = e
                    if retry_count < self._INIT_MAX_RETRY - 1:
                        try:
                            self.reset_tdx_ip()
                        except Exception as ip_e:
                            last_error = ip_e
                            break
            if last_error is not None:
                raise last_error
        except Exception:
            self.init_failed = True
            print(traceback.format_exc())
            print("通达信 香港行情接口初始化失败，香港行情不可用")

    def reset_tdx_ip(self):
        """重新选择 TDX 最优服务器并写入缓存。"""
        connect_info = best_ip.select_best_ip("future")
        connect_info = {"ip": connect_info["ip"], "port": int(connect_info["port"])}
        db.cache_set("tdxex_connect_ip", connect_info)
        self.connect_info = connect_info
        return connect_info

    def default_code(self):
        return "KH.00700"

    def support_frequencys(self):
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
            "1m": "1m",
        }

    def all_stocks(self):
        """获取通达信港股标的列表（category=2）。"""
        if len(self.g_all_stocks) > 0:
            return self.g_all_stocks

        # 初始化失败时直接返回空列表，避免 client.connect 再卡 30s 或 connect_info=None 引发 KeyError
        if getattr(self, "init_failed", False) or not self.connect_info or not self.market_maps:
            return []

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
                    if _i["category"] != 2:
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

        self.g_all_stocks = __all_stocks
        return self.g_all_stocks

    def to_tdx_code(self, code):
        """将 "KH.00700" 格式代码拆分为 (market_int, tdx_code)。"""
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
        """通达信不支持按时间区间查找，分页拉取后与文件缓存合并去重，再做前复权处理。"""
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
        # 忽略 start_date/end_date 范围提示: tdx 按 pages 拉最新、由上层裁剪; 否则 web 主加载必传
        # end_date 时 klines 返 None -> @retry -> RetryError -> 美股/港股图表恒空(与 tdx_fx d25ce69e 同修)。
        if market is None:
            print("不支持的调用参数")
            return None

        try:
            client = TdxExHq_API(raise_exception=True, auto_retry=True)
            with client.connect(self.connect_info["ip"], self.connect_info["port"]):
                klines_df: pd.DataFrame = self.fdb.get_tdx_klines(
                    Market.HK.value, code, frequency
                )
                if klines_df is None:
                    klines_df = pd.concat(
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
                    klines_df.loc[:, "date"] = pd.to_datetime(klines_df["datetime"])
                    klines_df.sort_values("date", inplace=True)
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
                        _ks.sort_values("date", inplace=True)
                        new_start_dt = _ks.iloc[0]["date"]
                        cache_end_dt = klines_df.iloc[-1]["date"] if i == 1 else cache_end_dt  # B1(Round8): 仅首轮拼接前捕获缓存真实末尾, 防每轮重算致陈旧>1400根中间段留洞
                        klines_df = pd.concat([klines_df, _ks], ignore_index=True)
                        # 新一页起始时间早于缓存末尾，说明已覆盖，无需继续
                        if cache_end_dt >= new_start_dt:
                            break

            # 去重：分页重叠时保留最新一条
            klines_df = klines_df.drop_duplicates(["date"], keep="last").sort_values(
                "date"
            )
            self.fdb.save_tdx_klines(Market.HK.value, code, frequency, klines_df)

            klines_df.loc[:, "date"] = klines_df["date"].dt.tz_localize(self.tz)
            klines_df = klines_df.sort_values("date")
            klines_df.loc[:, "code"] = code
            klines_df.loc[:, "volume"] = klines_df["amount"]

            klines_df = klines_df[
                ["code", "date", "open", "close", "high", "low", "volume"]
            ]
            klines_df = self.klines_qfq(code, klines_df)
            klines_df = normalize_kline_precision(klines_df, "hk", code)
            return klines_df
        except TdxConnectionError:
            print("连接失败，重新选择最优服务器")
            self.reset_tdx_ip()

        except Exception as e:
            print(f"获取行情异常 {code} Exception ：{str(e)}")
        finally:
            pass
        return None

    def stock_info(self, code: str) -> Union[Dict, None]:
        """获取港股标的名称。"""
        all_stock = self.all_stocks()
        stock = [_s for _s in all_stock if _s["code"] == code]
        if not stock:
            return None
        return {"code": stock[0]["code"], "name": stock[0]["name"]}

    def ticks(self, codes: List[str]) -> Dict[str, Tick]:
        """获取港股实时报价（通达信盘口接口）。"""
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

    def now_trading(self):
        """返回当前是否是港股交易时间（简化判断：09:00-15:59）。"""
        hour = int(time.strftime("%H"))
        if hour in {9, 10, 11, 12, 13, 14, 15}:
            return True
        return False

    def klines_qfq(self, code: str, klines: pd.DataFrame):
        try:
            xdxr_path = get_data_path() / "xdxr"
            if xdxr_path.is_dir() is False:
                xdxr_path.mkdir()
            xdxr_file = xdxr_path / f"hk_qfq_factor_{code.replace('.', '_')}.csv"
            now_day = fun.datetime_to_str(datetime.datetime.now(), "%Y-%m-%d")
            if (
                xdxr_file.is_file() is False
                or fun.timeint_to_str(int(xdxr_file.stat().st_mtime), "%Y-%m-%d")
                != now_day
            ):
                qfq_factor_df = ak.stock_hk_daily(
                    symbol=code.split(".")[1], adjust="qfq-factor"
                )
                if qfq_factor_df is not None and len(qfq_factor_df) > 0:
                    qfq_factor_df.to_csv(xdxr_file, index=False)
            else:
                qfq_factor_df = pd.read_csv(xdxr_file)

            if qfq_factor_df is None or len(qfq_factor_df) == 0:
                return klines

            qfq_factor_df["qfq_date"] = pd.to_datetime(
                qfq_factor_df["date"]
            ).dt.tz_localize(self.tz)
            qfq_factor_df["qfq_factor"] = qfq_factor_df["qfq_factor"].astype(float)
            qfq_factor_df = qfq_factor_df.drop(columns=["date"])

            # 合并 K 线与复权因子，向前填充 qfq_factor 后相乘
            df = pd.concat([klines, qfq_factor_df], axis=0)
            df["qfq_date"].fillna(df["date"], inplace=True)
            df.sort_values(by="qfq_date", inplace=True)
            df["qfq_factor"].fillna(method="ffill", inplace=True)
            df.dropna(inplace=True)
            df.reset_index(drop=True, inplace=True)

            df["open"] = df["open"] * df["qfq_factor"]
            df["high"] = df["high"] * df["qfq_factor"]
            df["low"] = df["low"] * df["qfq_factor"]
            df["close"] = df["close"] * df["qfq_factor"]
            return df[["code", "date", "open", "high", "low", "close", "volume"]]
        except Exception as e:
            print(f"计算 {code} 复权异常： {e}")
            return klines

    @staticmethod
    def __convert_date(dt: datetime.datetime):
        # 通达信日线后对齐，统一设为 16:00 与分钟线区分
        return dt.replace(hour=16, minute=0)

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
    ex = ExchangeTDXHK()
    klines = ex.klines("KH.09618", "d")
    print(klines.tail(20))
