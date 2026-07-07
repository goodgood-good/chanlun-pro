import datetime
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
from chanlun.exchange.exchange import Exchange, Tick, convert_us_tdx_kline_frequency
from chanlun.persistence.file_db import FileCacheDB
from chanlun.tools import tdx_best_ip as best_ip
from chanlun.exchange.kline_precision import normalize_kline_precision


@fun.singleton
class ExchangeTDXUS(Exchange):
    """通达信美股行情适配器（market=74，category=13）。"""

    g_all_stocks = []

    def __init__(self):
        try:
            self.connect_info = db.cache_get("tdxex_connect_ip")
            if self.connect_info is None:
                self.connect_info = self.reset_tdx_ip()
        except Exception:
            print(traceback.format_exc())
            print("通达信 美股行情接口初始化失败，美股行情不可用")

        self.tz = pytz.timezone("US/Eastern")
        self.fdb = FileCacheDB()

    def reset_tdx_ip(self):
        """重新选择 TDX 最优服务器并写入缓存。"""
        connect_info = best_ip.select_best_ip("future")
        connect_info = {"ip": connect_info["ip"], "port": int(connect_info["port"])}
        db.cache_set("tdxex_connect_ip", connect_info)
        self.connect_info = connect_info
        return connect_info

    def default_code(self):
        return "AAPL"

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
            "10m": "10m",
            "5m": "5m",
            "2m": "2m",
            "1m": "1m",
        }

    def all_stocks(self):
        """获取通达信美股标的列表（market=74，category=13），过滤含 +/=/-  的衍生品代码。"""
        if len(self.g_all_stocks) > 0:
            return self.g_all_stocks
        client = TdxExHq_API(raise_exception=True, auto_retry=True)
        __all_stocks = []
        with client.connect(self.connect_info["ip"], self.connect_info["port"]):
            start_i = 0
            count = 1000
            while True:
                instruments = client.get_instrument_info(start_i, count)
                for _i in instruments:
                    if _i["category"] == 13 and _i["market"] == 74:
                        if "+" in _i["code"] or "=" in _i["code"] or "-" in _i["code"]:
                            continue
                        __all_stocks.append(
                            {
                                "code": _i["code"],
                                "name": _i["name"],
                            }
                        )
                start_i += count
                if len(instruments) < count:
                    break

        self.g_all_stocks = __all_stocks
        return self.g_all_stocks

    def to_tdx_code(self, code):
        """美股代码即 TDX 代码，market 固定为 74。"""
        return 74, code

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
        """通达信不支持按时间区间查找，分页拉取后与文件缓存合并去重；时区由 _convert_dt 转为美东时间。"""
        if args is None:
            args = {}
        if "pages" not in args.keys():
            args["pages"] = 5
        else:
            args["pages"] = int(args["pages"])

        if "fq_type" not in args.keys():
            args["fq_type"] = "qfq"

        frequency_map = {
            "y": 11,
            "q": 10,
            "m": 6,
            "w": 5,
            "d": 9,
            "60m": 3,
            "30m": 2,
            "15m": 1,
            "10m": 0,
            "5m": 0,
            "2m": 8,
            "1m": 8,
        }
        market, tdx_code = self.to_tdx_code(code)
        if market is None or start_date is not None or end_date is not None:
            print("不支持的调用参数")
            return None

        try:
            client = TdxExHq_API(raise_exception=True, auto_retry=True)
            with client.connect(self.connect_info["ip"], self.connect_info["port"]):
                klines_df: pd.DataFrame = self.fdb.get_tdx_klines(
                    Market.US.value, code, frequency
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
                        old_end_dt = klines_df.iloc[-1]["date"]
                        klines_df = pd.concat([klines_df, _ks], ignore_index=True)
                        # 新一页起始时间早于缓存末尾，说明已覆盖，无需继续
                        if old_end_dt >= new_start_dt:
                            break

            klines_df["date"] = pd.to_datetime(klines_df["datetime"])
            # 去重：分页重叠时保留最新一条
            klines_df = klines_df.drop_duplicates(["date"], keep="last").sort_values(
                "date"
            )
            self.fdb.save_tdx_klines(Market.US.value, code, frequency, klines_df)

            klines_df.loc[:, "date"] = klines_df["date"].apply(self._convert_dt)
            klines_df = klines_df.sort_values("date")
            klines_df.loc[:, "code"] = code
            klines_df.loc[:, "volume"] = klines_df["amount"]

            klines_df = klines_df[
                ["code", "date", "open", "close", "high", "low", "volume"]
            ]

            if frequency in ["10m", "2m"]:
                klines_df = convert_us_tdx_kline_frequency(klines_df, frequency)

            if args["fq_type"] == "qfq":
                result = self.klines_qfq(code, klines_df)
                result = normalize_kline_precision(result, "us", code)
                return result
            else:
                klines_df = normalize_kline_precision(klines_df, "us", code)
                return klines_df
        except TdxConnectionError:
            self.reset_tdx_ip()
        except Exception as e:
            print(f"获取行情异常 {code} - {frequency} Exception ：{str(e)}")
            traceback.print_exc()

        return None

    def _convert_dt(self, _dt: datetime.datetime):
        """将通达信 CST 时间戳转换为美东时区；日线 15:00 特殊对齐为当日 16:00 收盘。"""
        if _dt.hour == 15 and _dt.minute == 0:
            # 日线及以上周期收盘时刻:16:00 美东。用 self.tz.localize() 而非
            # replace(tzinfo=self.tz)——pytz DstTzInfo 直接塞 tzinfo 取历史 LMT(-4:56)非 EST/EDT。
            return self.tz.localize(_dt.replace(hour=16, minute=0))

        # 通达信返回北京时间 naive 时间戳;用 localize 得正确 +8:00
        # (replace(tzinfo=pytz.timezone(...)) 会误取 LMT +8:06,致换算早 6 分钟)。
        _dt = pytz.timezone("Asia/Shanghai").localize(_dt)

        if _dt.hour in [0, 1, 2, 3, 4, 5]:
            _dt = _dt + datetime.timedelta(days=1)
        return _dt.astimezone(self.tz)

    def stock_info(self, code: str) -> Union[Dict, None]:
        """获取美股标的名称。"""
        all_stock = self.all_stocks()
        stock = [_s for _s in all_stock if _s["code"] == code]
        if not stock:
            return None
        return {"code": stock[0]["code"], "name": stock[0]["name"]}

    def ticks(self, codes: List[str]) -> Dict[str, Tick]:
        """获取美股实时报价（通达信盘口接口）。"""
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
                            # 涨跌幅 =(现价-昨收)/昨收;原分母用现价 price 是错的(涨 10% 会显示
                            # 成 ~9.09%),改除以 pre_close 与 cq/QMT 口径一致(审查 L2)。
                            round(
                                (_quote["price"] - _quote["pre_close"])
                                / _quote["pre_close"]
                                * 100,
                                2,
                            )
                            if _quote["pre_close"] > 0
                            else 0
                        ),
                    )
        return ticks

    def now_trading(self):
        """返回当前是否处于美股交易时间（美东时间周一至周五 09:30-16:00）。"""
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

    def klines_qfq(self, code: str, klines: pd.DataFrame):
        try:
            xdxr_path = get_data_path() / "xdxr"
            if xdxr_path.is_dir() is False:
                xdxr_path.mkdir()
            xdxr_file = xdxr_path / f"us_qfq_factor_{code}.csv"
            now_day = fun.datetime_to_str(datetime.datetime.now(), "%Y-%m-%d")
            if (
                xdxr_file.is_file() is False
                or fun.timeint_to_str(int(xdxr_file.stat().st_mtime), "%Y-%m-%d")
                != now_day
            ):
                qfq_factor_df = ak.stock_us_daily(symbol=code, adjust="qfq-factor")
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
            qfq_factor_df = qfq_factor_df.drop(columns=["date", "adjust"])

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
            print(f"计算 {code} 复权数据异常：{e}")
            return klines

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
    ex = ExchangeTDXUS()
    klines = ex.klines("AAPL", "30m")
    print(klines.tail(20))
