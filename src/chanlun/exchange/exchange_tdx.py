import copy
import datetime
import traceback
import warnings
from typing import Dict, List, Union

import pandas as pd
import pytz
from pytdx.errors import TdxConnectionError
from pytdx.hq import TdxHq_API
from tenacity import retry, retry_if_result, stop_after_attempt, wait_random

from chanlun import fun
from chanlun.market import Market
from chanlun.config import get_data_path
from chanlun.persistence.db import db
from chanlun.exchange.exchange import Exchange, Tick, convert_stock_kline_frequency
from chanlun.exchange.kline_precision import normalize_kline_precision
from chanlun.exchange.stocks_bkgn import StocksBKGN
from chanlun.exchange.tdx_a_codes import tdx_codes_by_bj, tdx_codes_by_error
from chanlun.persistence.file_db import FileCacheDB
from chanlun.tools import tdx_best_ip as best_ip


@fun.singleton
class ExchangeTDX(Exchange):
    """通达信沪深 A 股行情适配器（含复权处理）。"""

    g_all_stocks = []

    def __init__(self):
        try:
            # 优先从 cache 读取上次选出的最优服务器，避免每次启动都重新测速
            self.connect_info = db.cache_get("tdx_connect_ip")
            if self.connect_info is None:
                self.connect_info = self.reset_tdx_ip()
        except Exception:
            print(traceback.format_exc())
            print("通达信 沪深行情接口初始化失败，沪深行情不可用")

        self.stock_bkgn = StocksBKGN()
        self.fdb = FileCacheDB()
        self.tz = pytz.timezone("Asia/Shanghai")

    def reset_tdx_ip(self):
        """
        重新选择tdx最优ip，并返回
        """
        connect_info = best_ip.select_best_ip("stock")
        connect_info = {"ip": connect_info["ip"], "port": int(connect_info["port"])}
        db.cache_set("tdx_connect_ip", connect_info)
        self.connect_info = connect_info
        return connect_info

    def default_code(self):
        return "SH.000001"

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
        """
        使用 通达信的方式获取所有股票代码
        """
        if len(self.g_all_stocks) > 0:
            return self.g_all_stocks

        __all_stocks = []
        __codes = []
        try:
            for market in range(2):
                client = TdxHq_API(raise_exception=True, auto_retry=True)
                with client.connect(self.connect_info["ip"], self.connect_info["port"]):
                    count = client.get_security_count(market)
                    data = pd.concat(
                        [
                            client.to_df(client.get_security_list(market, i * 1000))
                            for i in range(int(count / 1000) + 1)
                        ],
                        axis=0,
                        sort=False,
                    )
                    for _d in data.iterrows():
                        code = _d[1]["code"]
                        name = _d[1]["name"]
                        sse = "SZ" if market == 0 else "SH"
                        _type = self.for_sz(code) if market == 0 else self.for_sh(code)
                        if _type in ["bond_cn", "undefined", "stockB_cn"]:
                            continue
                        code = f"{sse}.{str(code)}"
                        if code in __codes:
                            continue
                        __codes.append(code)
                        precision = 100 if _type == "stock_cn" else 1000
                        __all_stocks.append(
                            {
                                "code": code,
                                "name": name,
                                "type": _type,
                                "precision": precision,
                            }
                        )
        except TdxConnectionError:
            print("连接失败，重新选择最优服务器")
            self.reset_tdx_ip()
            return self.all_stocks()

        # 北京交易所（BJ）股票通达信 API 不直接返回，从本地维护的静态列表补充
        for _c, _n in tdx_codes_by_bj.items():
            __all_stocks.append(
                {
                    "code": _c,
                    "name": _n,
                    "type": "stock_cn",
                    "precision": 100,
                }
            )

        # 错误的代码过滤
        __all_stocks = [
            _s for _s in __all_stocks if _s["code"] not in tdx_codes_by_error
        ]

        self.g_all_stocks = __all_stocks
        return self.g_all_stocks

    def to_tdx_code(self, code):
        """
        转换为 tdx 对应的代码
        """
        # 富途代码对 tdx 代码的对照修正表
        tdx_code_maps = {"SH.000001": "SH.999999"}
        if code in tdx_code_maps:
            code = tdx_code_maps[code]

        market = code[:3]
        if market == "SH.":
            market = 1
        elif market == "SZ.":
            market = 0
        elif market == "BJ.":
            market = 2
        else:
            market = None

        if market == 2:
            _type = "stock_cn"
        else:
            all_stocks = self.all_stocks()
            stock = [_s for _s in all_stocks if _s["code"] == code]
            _type = stock[0]["type"] if stock else None
        return market, code[-6:], _type

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
        """获取 K 线数据，不支持按时间区间筛选（通达信接口限制），通过分页拉取最新 N*700 条。"""
        if args is None:
            args = {}
        if "fq" not in args.keys():
            args["fq"] = "qfq"
        if "use_cache" not in args.keys():
            args["use_cache"] = True
        if "pages" not in args.keys():
            args["pages"] = 8
        else:
            args["pages"] = int(args["pages"])

        frequency_map = {
            "y": 11,
            "m": 6,
            "w": 9,
            "d": 9,
            "120m": 3,
            "60m": 3,
            "30m": 2,
            "15m": 1,
            "10m": 0,
            "5m": 0,
            "2m": 8,
            "1m": 8,
        }
        # 周线由日线复权后合并，需要更多原始数据才能覆盖足够的周数
        if frequency == "w":
            args["pages"] = 12

        market, tdx_code, _type = self.to_tdx_code(code)
        if market is None or _type is None:
            return None

        try:
            client = TdxHq_API(raise_exception=True, auto_retry=True)
            with client.connect(self.connect_info["ip"], self.connect_info["port"]):
                if "index" in _type:
                    get_bars = client.get_index_bars
                else:
                    get_bars = client.get_security_bars

                ks: pd.DataFrame = self.fdb.get_tdx_klines(
                    Market.A.value, code, frequency
                )
                if ks is None or len(ks) == 0:
                    # 获取 8*700 = 5600 条数据
                    ks = pd.concat(
                        [
                            client.to_df(
                                get_bars(
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
                    if len(ks) == 0:
                        return pd.DataFrame([])
                    ks.loc[:, "date"] = pd.to_datetime(ks["datetime"])
                    ks.sort_values("date", inplace=True)
                else:
                    for i in range(1, args["pages"] + 1):
                        _ks = client.to_df(
                            get_bars(
                                frequency_map[frequency],
                                market,
                                tdx_code,
                                (i - 1) * 700,
                                700,
                            )
                        )
                        if len(_ks) == 0:
                            break
                        _ks.loc[:, "date"] = pd.to_datetime(_ks["datetime"])
                        _ks.sort_values("date", inplace=True)
                        new_start_dt = _ks.iloc[0]["date"]
                        old_end_dt = ks.iloc[-1]["date"]
                        ks = pd.concat([ks, _ks], ignore_index=True)
                        # 如果请求的第一个时间大于缓存的最后一个时间，退出
                        if old_end_dt >= new_start_dt:
                            break
            # 通达信分钟线午休结束后的第一根 bar 时间戳为 13:00，实际应归属上午 11:30（最后一根）
            if len(frequency) >= 2 and frequency.endswith("m"):
                def dt_1300_to_1130(_d: datetime.datetime):
                    if _d.hour == 13 and _d.minute == 0:
                        return _d.replace(hour=11, minute=30)
                    return _d

                ks["date"] = ks["date"].apply(dt_1300_to_1130)

            # 多页数据合并后去重，保留最新一条（数据源可能在更新中返回重复 bar）
            ks = ks.drop_duplicates(["date"], keep="last").sort_values("date")

            self.fdb.save_tdx_klines(Market.A.value, code, frequency, ks)

            ks.loc[:, "code"] = code
            ks.loc[:, "volume"] = ks["vol"]

            ks["date"] = ks["date"].dt.tz_localize(self.tz)
            # 日线及以上统一设为 15:00（A 股收盘），便于与分钟线时间轴对齐
            if frequency in ["d", "w", "m", "q", "y"]:
                ks["date"] = ks["date"].apply(lambda _d: _d.replace(hour=15, minute=0))

            # 月线时间戳规整到月初，年线规整到年初，保持惯例
            if frequency == "m":
                ks["date"] = ks["date"].apply(lambda _d: _d.replace(day=1))
            if frequency == "y":
                ks["date"] = ks["date"].apply(lambda _d: _d.replace(month=1, day=1))
            ks = ks.drop_duplicates(["date"], keep="last").sort_values("date")

            if args["fq"] in ["qfq", "hfq"]:
                ks = self.klines_fq(ks, self.xdxr(market, code, tdx_code), args["fq"])

            ks.reset_index(inplace=True)
            if frequency in ["w", "120m", "10m", "2m"]:
                ks = convert_stock_kline_frequency(ks, frequency)

            ks = ks[["code", "date", "open", "close", "high", "low", "volume"]]
            ks = normalize_kline_precision(ks, "a", code)
            return ks
        except TdxConnectionError:
            print("连接失败，重新选择最优服务器")
            self.reset_tdx_ip()
        except Exception as e:
            print(f"获取行情异常 {code} Exception ：{str(e)}")
            print(traceback.format_exc())
        finally:
            pass
        return None

    @staticmethod
    def get_monday(date):
        """
        获取给定日期当周的周日
        """
        weekday = date.weekday()
        if weekday == 0:
            return date
        elif 0 < weekday < 5:
            days_to_mon = weekday
        elif weekday == 5:
            days_to_mon = 5
        else:
            days_to_mon = 6
        return date - datetime.timedelta(days=days_to_mon)

    def stock_info(self, code: str) -> Union[Dict, None]:
        """
        获取股票名称
        """
        all_stock = self.all_stocks()
        stock = [_s for _s in all_stock if _s["code"] == code]
        if not stock:
            return None
        return stock[0]

    def ticks(self, codes: List[str]) -> Dict[str, Tick]:
        """批量获取实时行情快照，通过 get_security_quotes 分批拉取（每批最多 80 只）。"""
        ticks = {}
        if len(codes) == 0:
            return ticks
        query_stocks = []
        stock_types = {}
        for _c in codes:
            _m, _c, _t = self.to_tdx_code(_c)
            if _m is not None:
                if _m == 2:
                    continue
                query_stocks.append((_m, _c))
                stock_types[_c] = _t
        client = TdxHq_API(raise_exception=True, auto_retry=True)
        with client.connect(self.connect_info["ip"], self.connect_info["port"]):
            total_quotes = len(query_stocks)
            # 通达信单次 get_security_quotes 最多传 80 个标的，超出需分批
            batch_size = 80
            quotes = []
            error_codes = []
            for i in range(0, total_quotes, batch_size):
                batch_stocks = query_stocks[i : i + batch_size]
                try:
                    batch_quotes = client.get_security_quotes(batch_stocks)
                except Exception as e:
                    error_codes += batch_stocks
                    print(f"获取数据失败: {e}")
                    continue
                quotes += batch_quotes
            # ('market', 0), ('code', '000001'), ('active1', 4390), ('price', 14.29), ('last_close', 14.24), ('open', 14.35),
            # ('high', 14.37), ('low', 14.14), ('servertime', '14:59:55.939'), ('reversed_bytes0', 14998872),
            # ('reversed_bytes1', -1429), ('vol', 690954), ('cur_vol', 11982), ('amount', 985552128.0), ('s_vol', 339925),
            # ('b_vol', 351029), ('reversed_bytes2', -1), ('reversed_bytes3', 45188), ('bid1', 14.28), ('ask1', 14.29),
            # ('bid_vol1', 2617), ('ask_vol1', 2391), ('bid2', 14.27), ('ask2', 14.3), ('bid_vol2', 1853),
            # ('ask_vol2', 4075), ('bid3', 14.26), ('ask3', 14.31), ('bid_vol3', 2164), ('ask_vol3', 3421), ('bid4', 14.25),
            # ('ask4', 14.32), ('bid_vol4', 2512), ('ask_vol4', 8679), ('bid5', 14.24), ('ask5', 14.33), ('bid_vol5', 889),
            # ('ask_vol5', 5191), ('reversed_bytes4', (2518,)), ('reversed_bytes5', 0), ('reversed_bytes6', 0),
            # ('reversed_bytes7', 0), ('reversed_bytes8', 0), ('reversed_bytes9', 0.0), ('active2', 4390)])
            for _q in quotes:
                if _q["code"] == "999999":
                    _code = "SH.000001"
                else:
                    _code = [
                        _c
                        for _c in codes
                        if _c[-6:] == _q["code"]
                        and (
                            (_c[:2] == "SZ" and _q["market"] == 0)
                            or (_c[:2] == "SH" and _q["market"] == 1)
                            or (_c[:2] == "BJ" and _q["market"] == 2)
                        )
                    ]
                    if len(_code) == 0:
                        continue
                    _code = _code[0]
                # 通达信 ETF 报价单位为分（整数），需除以 10 转换为元
                if stock_types[_q["code"]] == "etf_cn":
                    _q["price"] /= 10
                    _q["bid1"] /= 10
                    _q["ask1"] /= 10
                    _q["low"] /= 10
                    _q["high"] /= 10
                    _q["open"] /= 10
                    _q["last_close"] /= 10
                ticks[_code] = Tick(
                    code=_code,
                    last=_q["price"],
                    buy1=_q["bid1"],
                    sell1=_q["ask1"],
                    low=_q["low"],
                    high=_q["high"],
                    volume=_q["vol"],
                    open=_q["open"],
                    rate=(
                        round(
                            (_q["price"] - _q["last_close"]) / _q["last_close"] * 100, 2
                        )
                        if _q["price"] != 0
                        else 0
                    ),
                )

        return ticks

    def now_trading(self):
        """
        返回当前是否是交易时间
        周一至周五，09:30-11:30 13:00-15:00
        """
        now_dt = datetime.datetime.now()
        if now_dt.weekday() in [5, 6]:  # 周六日不交易
            return False
        hour = now_dt.hour
        minute = now_dt.minute
        if hour == 9 and minute >= 30:
            return True
        if hour in [10, 13, 14]:
            return True
        if hour == 11 and minute < 30:
            return True
        return False

    @staticmethod
    def for_sz(code):
        """深市代码分类
        Arguments:
            code {[type]} -- [description]
        Returns:
            [type] -- [description]
        """

        if str(code)[:2] in ["00", "30", "02"]:
            return "stock_cn"
        elif str(code)[:2] in ["39"]:
            return "index_cn"
        elif str(code)[:2] in ["15", "16"]:
            return "etf_cn"
        elif str(code)[:3] in [
            "101",
            "104",
            "105",
            "106",
            "107",
            "108",
            "109",
            "111",
            "112",
            "114",
            "115",
            "116",
            "117",
            "118",
            "119",
            "123",
            "127",
            "128",
            "131",
            "139",
        ]:
            # 10xxxx 国债现货
            # 11xxxx 债券
            # 12xxxx 可转换债券

            # 123
            # 127
            # 12xxxx 国债回购
            return "bond_cn"

        elif str(code)[:2] in ["20"]:
            return "stockB_cn"
        else:
            return "undefined"

    @staticmethod
    def for_sh(code):
        if str(code)[0] == "6":
            return "stock_cn"
        elif str(code)[:3] in ["000", "880", "999"]:
            return "index_cn"
        elif str(code)[:2] in ["51", "58"]:
            return "etf_cn"
        # 110×××120×××企业债券；
        # 129×××100×××可转换债券；
        # 113A股对应可转债 132
        elif str(code)[:3] in [
            "102",
            "110",
            "113",
            "120",
            "122",
            "124",
            "130",
            "132",
            "133",
            "134",
            "135",
            "136",
            "140",
            "141",
            "143",
            "144",
            "147",
            "148",
        ]:
            return "bond_cn"
        else:
            return "undefined"

    def stock_owner_plate(self, code: str):
        """
        使用已经保存好的板块概念信息
        """

        code_type = ""
        if "SH." in code:
            code_type = self.for_sh(code.split(".")[1])
        elif "SZ." in code:
            code_type = self.for_sz(code.split(".")[1])
        if code_type != "stock_cn":
            return {"HY": [], "GN": []}
        bkgn = self.stock_bkgn.get_code_bkgn(code.split(".")[1])
        hys = [{"code": n, "name": n} for n in bkgn["HY"]]
        gns = [{"code": n, "name": n} for n in bkgn["GN"]]
        return {"HY": hys, "GN": gns}

    def plate_stocks(self, code: str):
        """
        使用已经保存好的板块概念信息
        """
        stock_codes = self.stock_bkgn.get_codes_by_gn(code)
        stock_codes += self.stock_bkgn.get_codes_by_hy(code)

        stock_codes = self.stock_bkgn.ths_to_tdx_codes(stock_codes)

        stocks = []
        for _c in stock_codes:
            _stock = self.stock_info(_c)
            if _stock:
                stocks.append(_stock)
        return stocks

    def balance(self):
        raise Exception("交易所不支持")

    def positions(self, code: str = ""):
        raise Exception("交易所不支持")

    def order(self, code: str, o_type: str, amount: float, args=None):
        raise Exception("交易所不支持")

    def xdxr(self, market: int, project_code: str, code: str):
        """
        读取除权除息信息
        """
        xdxr_path = get_data_path() / "xdxr"
        if xdxr_path.is_dir() is False:
            xdxr_path.mkdir()
        xdxr_file = xdxr_path / f"new_xdxr_{market}_{project_code}.pkl"
        now_day = fun.datetime_to_str(datetime.datetime.now(), "%Y-%m-%d")
        need_update = False  # 判断是否需要更新
        if (
            xdxr_file.is_file() is False
            or fun.timeint_to_str(int(xdxr_file.stat().st_mtime), "%Y-%m-%d") != now_day
        ):
            need_update = True
        if need_update:
            client = TdxHq_API(raise_exception=True, auto_retry=True)
            with client.connect(self.connect_info["ip"], self.connect_info["port"]):
                data = client.to_df(client.get_xdxr_info(market, code))
            if len(data) > 0:
                data.loc[:, "date"] = (
                    data["year"].map(str)
                    + "-"
                    + data["month"].map(str)
                    + "-"
                    + data["day"].map(str)
                )
                data["date"] = pd.to_datetime(data["date"])
            data.to_pickle(str(xdxr_file))
        else:
            data = pd.read_pickle(str(xdxr_file))

        return data

    def klines_fq(self, fq_klines: pd.DataFrame, xdxr_data, fq_type: str):
        """对 K 线进行前复权（qfq）或后复权（hfq）处理，先处理扩缩股（category=11），再处理分红配股（category=1）。"""
        if len(xdxr_data) == 0:
            return fq_klines

        suogu_info = copy.deepcopy(xdxr_data.query("category==11"))
        if len(suogu_info) > 0:
            # 扩缩股：对事件日期之前的历史价格除以 suogu 系数，成交量乘以 suogu（保持总额不变）
            suogu_info.loc[:, "idx_date"] = (
                suogu_info["date"].dt.tz_localize(self.tz).dt.tz_convert("UTC")
            )
            suogu_info.set_index("idx_date", inplace=True)

            fq_klines = fq_klines.assign(if_trade=1)
            fq_klines.loc[:, "idx_date"] = fq_klines["date"].dt.tz_convert("UTC")
            fq_klines.set_index("idx_date", inplace=True)

            # 对每个扩缩股事件，将日期之前的行情数据除以suogu值
            for idx, row in suogu_info.iterrows():
                if "suogu" in row and row["suogu"] > 0:
                    # 找到该日期之前的所有K线数据
                    before_data = fq_klines.loc[fq_klines.index < idx]
                    if len(before_data) > 0:
                        for col in ["open", "high", "low", "close"]:
                            if col in before_data.columns:
                                fq_klines.loc[fq_klines.index < idx, col] = (
                                    fq_klines.loc[fq_klines.index < idx, col]
                                    / row["suogu"]
                                )
                        if "volume" in before_data.columns:
                            fq_klines.loc[fq_klines.index < idx, "volume"] = (
                                fq_klines.loc[fq_klines.index < idx, "volume"]
                                * row["suogu"]
                            )
                        elif "vol" in before_data.columns:
                            fq_klines.loc[fq_klines.index < idx, "vol"] = (
                                fq_klines.loc[fq_klines.index < idx, "vol"]
                                * row["suogu"]
                            )

            fq_klines = fq_klines.reset_index(drop=True)
            fq_klines = fq_klines.drop(columns=["idx_date"], errors="ignore")

        # 再处理除权除息数据（分红/配股/送转股，category=1）
        info = copy.deepcopy(xdxr_data.query("category==1"))
        if len(info) == 0:
            return fq_klines
        info.loc[:, "idx_date"] = (
            info["date"].dt.tz_localize(self.tz).dt.tz_convert("UTC")
        )
        info.set_index("idx_date", inplace=True)

        # 若未经过扩缩股处理，需补建 idx_date 索引用于后续 concat 对齐
        if "idx_date" not in fq_klines.columns:
            fq_klines = fq_klines.assign(if_trade=1)
            fq_klines.loc[:, "idx_date"] = fq_klines["date"].dt.tz_convert("UTC")
            fq_klines.set_index("idx_date", inplace=True)

        if len(info) > 0:
            with warnings.catch_warnings():
                warnings.filterwarnings("ignore", category=FutureWarning)
                data = pd.concat(
                    [
                        fq_klines,
                        info.loc[
                            fq_klines.index[0] : fq_klines.index[-1], ["category"]
                        ],
                    ],
                    axis=1,
                )
                data["if_trade"].fillna(value=0, inplace=True)
                data = data.fillna(method="ffill")
                data = pd.concat(
                    [
                        data,
                        info.loc[
                            fq_klines.index[0] : fq_klines.index[-1],
                            ["fenhong", "peigu", "peigujia", "songzhuangu"],
                        ],
                    ],
                    axis=1,
                )
        else:
            data = pd.concat(
                [
                    fq_klines,
                    info.loc[
                        :, ["category", "fenhong", "peigu", "peigujia", "songzhuangu"]
                    ],
                ],
                axis=1,
            )

        data = data.fillna(0)

        # 前日收盘价 = (前收盘*10 - 分红 + 配股价*配股数) / (10 + 配股数 + 送转股数)
        data["preclose"] = (
            data["close"].shift(1) * 10
            - data["fenhong"]
            + data["peigu"] * data["peigujia"]
        ) / (10 + data["peigu"] + data["songzhuangu"])

        # 前复权：adj 从最新往历史累积，历史价格乘以 adj 使最新价保持原始价格
        if fq_type == "qfq":
            data["adj"] = (
                (data["preclose"].shift(-1) / data["close"]).fillna(1)[::-1].cumprod()
            )
            for col in ["open", "high", "low", "close"]:
                data[col] = round(data[col] * data["adj"], 3)

        # 后复权：adj 从历史往最新累积，历史价格除以 adj 还原未复权基准
        if fq_type == "hfq":
            data["adj"] = (
                (data["close"] / data["preclose"].shift(-1))
                .cumprod()
                .shift(1)
                .fillna(1)
            )
            for col in ["open", "high", "low", "close"]:
                data[col] = round(data[col] / data["adj"], 3)

        data = data.query("if_trade==1 and open != 0")

        return data[["code", "date", "open", "close", "high", "low", "volume"]]


if __name__ == "__main__":
    ex = ExchangeTDX()
    klines = ex.klines("SH.512800", "d")
    print(klines.tail(20))
