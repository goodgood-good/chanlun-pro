import time
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
from chanlun.exchange.exchange import (
    Exchange,
    Tick,
    convert_overseas_tdx_kline_frequency,
)
from chanlun.exchange.kline_precision import normalize_kline_precision
from chanlun.persistence.file_db import FileCacheDB
from chanlun.tools import tdx_best_ip as best_ip


@fun.singleton
class ExchangeTDXFX(Exchange):
    """
    通达信外汇行情接口
    """

    g_all_stocks = []

    def __init__(self):
        self.tz = pytz.timezone("Asia/Shanghai")

        self.fdb = FileCacheDB()

        try:
            # 优先复用缓存中的最优服务器，无缓存则重新选择
            self.connect_info = db.cache_get("tdxex_connect_ip")
            if self.connect_info is None:
                self.connect_info = self.reset_tdx_ip()

            # 映射外汇交易所代码（category==4 为外汇）
            self.market_maps = {}
            while True:
                try:
                    client = TdxExHq_API(
                        multithread=True, raise_exception=True, auto_retry=True
                    )
                    with client.connect(
                        self.connect_info["ip"], self.connect_info["port"]
                    ):
                        all_markets = client.get_markets()
                        for _m in all_markets:
                            if _m["category"] == 4:
                                self.market_maps[_m["short_name"]] = {
                                    "market": _m["market"],
                                    "category": _m["category"],
                                    "name": _m["name"],
                                }
                    break
                except TdxConnectionError:
                    self.reset_tdx_ip()
        except Exception:
            print(traceback.format_exc())
            print("通达信 外汇行情接口初始化失败，外汇行情不可用")

    def reset_tdx_ip(self):
        """
        重新选择tdx最优服务器
        """
        connect_info = best_ip.select_best_ip("future")
        connect_info = {"ip": connect_info["ip"], "port": int(connect_info["port"])}
        db.cache_set("tdxex_connect_ip", connect_info)
        self.connect_info = connect_info
        return connect_info

    def default_code(self):
        """返回默认外汇代码"""
        return "FX.USDEUR"

    def support_frequencys(self):
        """返回支持的周期及其展示名映射"""
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
            "1m": "1m",
        }

    def all_stocks(self):
        """
        使用 通达信的方式获取所有外汇代码
        """
        if len(self.g_all_stocks) > 0:
            return self.g_all_stocks

        __all_stocks = []
        client = TdxExHq_API(multithread=True, raise_exception=True, auto_retry=True)
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
                        _i["category"] != 4
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

        self.g_all_stocks = __all_stocks

        return self.g_all_stocks

    def to_tdx_code(self, code):
        """
        转换为 tdx 对应的代码
        """
        code_str = str(code)
        code_infos = code_str.split(".")
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
        """
        通达信，不支持按照时间查找
        """
        if args is None:
            args = {}
        if "pages" not in args.keys():
            args["pages"] = 10
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
            "10m": 0,
            "5m": 0,
            "1m": 8,
        }
        market, tdx_code = self.to_tdx_code(code)
        # start_date/end_date 是上层(web)的范围提示:end_date=now 表示"拉到现在",增量请求会带
        # start_date 窄窗口。但 tdx_fx 按 pages 从最新往前拉固定根数(本就含 end_date=now 的最新),
        # 由上层(prepend/getBars)按 from-to 裁剪。故忽略范围参数而非 return None——否则外汇
        # 5m/30m/60m 在 cache_miss(web 必传 end_date)时全部失败:tdx_fx 返回 None → @retry →
        # RetryError(实测根因;1m/日线"正常"仅因碰巧 cache hit 绕开 ex.klines)。qmt/cq 支持范围
        # 参数,tdx_fx 不支持但忽略安全(本方法 line 178 之后从不读取 start_date/end_date)。
        if market is None:
            print("不支持的调用参数")
            return None

        _s_time = time.time()
        try:
            client = TdxExHq_API(
                multithread=True, raise_exception=True, auto_retry=True
            )
            with client.connect(self.connect_info["ip"], self.connect_info["port"]):
                klines: pd.DataFrame = self.fdb.get_tdx_klines(
                    Market.FX.value, code, frequency
                )
                if klines is None or len(klines) == 0:  # 空数据帧也走全量拉取
                    # 无缓存：按页拉取（每页 700 条），pages 决定总量
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
                    klines.sort_values("date", inplace=True)
                else:
                    # 有缓存：逐页向前增量拉取，直到与缓存末尾衔接
                    cache_end_dt = klines.iloc[-1]["date"]
                    _fresh_pages = []
                    _bridged = False
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
                        if len(_ks) == 0:
                            break
                        _ks.loc[:, "date"] = pd.to_datetime(_ks["datetime"])
                        _ks.sort_values("date", inplace=True)
                        new_start_dt = _ks.iloc[0]["date"]
                        _fresh_pages.append(_ks)
                        # 新页起始时间已被缓存覆盖，说明数据已衔接，停止拉取
                        if cache_end_dt >= new_start_dt:
                            _bridged = True
                            break
                    if _fresh_pages:
                        if _bridged:
                            # 衔接成功: 缓存 + 新页合并(与原逻辑等价, happy-path 字节不变)
                            klines = pd.concat([klines] + _fresh_pages, ignore_index=True)
                        else:
                            # pages 耗尽仍未衔接(缓存陈旧超 pages*700 根可达范围): concat(缓存+新页)
                            # 中间 (cache_end, 最旧新页) 必留洞, save 落盘则永久污染缓存、增量只衔接
                            # 新端洞永不修复。故弃陈旧缓存, 仅保留本次连续新页(等价冷启动全拉近段)。
                            print(
                                f"⚠ tdx_fx 增量分页耗尽未衔接缓存(cache_end={cache_end_dt} < "
                                f"最旧新页={new_start_dt}), 弃陈旧缓存防留洞: {code} {frequency}"
                            )
                            klines = pd.concat(_fresh_pages, ignore_index=True)

            klines = klines.drop_duplicates(["date"], keep="last").sort_values("date")
            # ⚠ frequency=="10m" 时这里 save 的 klines 内容实为 5 分钟原始 K 线(10m 的 resample
            # 在下方对 result 做、不回写缓存)。缓存键虽写 "10m" 但语义是 5min,严禁哪天"优化"成命中
            # 缓存直接 return 跳过 resample——会产出 5min 当 10m 的脏数据(审查 H3)。
            self.fdb.save_tdx_klines(Market.FX.value, code, frequency, klines)

            klines.loc[:, "code"] = code
            klines.loc[:, "volume"] = klines["trade"]
            # date 从原始 datetime 列(pytdx 返回 naive 字符串)重建再 localize,故恒 naive、localize
            # 安全;Asia/Shanghai 无 DST 也不产生 ambiguous/nonexistent(审查 L3 已核安全)。
            klines.loc[:, "date"] = pd.to_datetime(klines["datetime"]).dt.tz_localize(
                self.tz
            )

            klines[["volume"]] = klines[["volume"]].astype(float)

            result = klines[["code", "date", "open", "close", "high", "low", "volume"]]
            # 通达信扩展行情无原生 10 分钟周期(frequency_map['10m']=category 0=5分钟),拿到的是
            # 5 分钟 K 线需重采样合成真正的 10 分钟周期。
            # kline_frequency)。共用前提是"date 列已带 tz":本函数上方已 localize 到 +8;us 经
            # _convert_dt 已转为美东时区（并非 UTC+8，因此不能声称美股也按 UTC+8 存储）。
            # convert 内部统一 tz_convert(UTC) 后按 UTC 整 10 分切 bin,故两源通用。
            if frequency == "10m":
                result = convert_overseas_tdx_kline_frequency(result, "10m")
            result = normalize_kline_precision(result, "fx", code)
            return result
        except TdxConnectionError:
            self.reset_tdx_ip()
        except Exception as e:
            print(f"获取行情异常 {code} Exception ：{str(e)}")
            traceback.print_exc()
        finally:
            pass
        return None

    def stock_info(self, code: str) -> Union[Dict, None]:
        """
        获取标的名称
        """
        all_stock = self.all_stocks()
        stock = [_s for _s in all_stock if _s["code"] == code]
        if not stock:
            return None
        return {"code": stock[0]["code"], "name": stock[0]["name"]}

    def ticks(self, codes: List[str]) -> Dict[str, Tick]:
        """
        如果可以使用 富途 的接口，就用 富途的，否则就用 日线的 K线计算
        使用 富途 的接口会很快，日线则很慢
        获取日线的k线，并返回最后一根k线的数据
        """
        ticks = {}
        client = TdxExHq_API(multithread=True, raise_exception=True, auto_retry=True)
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

    def now_trading(self, market: str):
        """返回当前是否是交易时间（外汇视为始终可交易）"""
        return True

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
