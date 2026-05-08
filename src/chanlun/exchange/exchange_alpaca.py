import datetime as dt
import os

from alpaca.data import StockBarsRequest, StockSnapshotRequest, DataFeed
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.timeframe import TimeFrame, TimeFrameUnit

import datetime as dt
from chanlun import config
from chanlun import fun
from chanlun.exchange.exchange import *

g_all_stocks = []


@fun.singleton
class ExchangeAlpaca(Exchange):
    """
    TODO 年久失修，使用前请自行修改测试
    """

    def __init__(self):
        super().__init__()

        self.client = StockHistoricalDataClient(
            api_key=config.ALPACA_APIKEY, secret_key=config.ALPACA_SECRET
        )

        # 设置时区
        self.tz = pytz.timezone("US/Eastern")

        # is vip 如果是付费的，可以查询最新的数据，否则只能查询历史
        self.is_vip = False

    def default_code(self):
        return "AAPL"

    def support_frequencys(self):
        return {
            "m": "Month",
            "w": "Week",
            "d": "Day",
            "60m": "1H",
            "30m": "30m",
            "10m": "10m",
            "15m": "15m",
            "5m": "5m",
            "1m": "1m",
        }

    def all_stocks(self):
        """
        获取所有股票代码
        """
        global g_all_stocks
        if len(g_all_stocks) > 0:
            return g_all_stocks
        stocks = pd.read_csv(
            os.path.split(os.path.realpath(__file__))[0] + "/us_symbols.csv"
        )
        for s in stocks.iterrows():
            g_all_stocks.append({"code": s[1]["code"], "name": s[1]["name"]})
        return g_all_stocks

        # 以下是从网络获取
        # if len(g_all_stocks) > 0:
        #     return g_all_stocks
        # g_all_stocks = rd.get_ex('us_stocks_all')
        # if g_all_stocks is not None:
        #     return g_all_stocks
        # g_all_stocks = []
        #
        # g_all_stocks = [el.symbol for el in self.api.list_assets(status='active', asset_class='us_equity')]
        # if len(g_all_stocks) > 0:
        #     rd.save_ex('us_stocks_all', 24 * 60 * 60, g_all_stocks)
        #
        # return g_all_stocks

    def klines(
        self,
        code: str,
        frequency: str,
        start_date: str = None,
        end_date: str = None,
        args=None,
    ) -> [pd.DataFrame, None]:
        if args is None:
            args = {}
        # chanlun 项目美股 symbol 形态是 "{TICKER}.US"（如 "QQQ.US"），
        # alpaca-py 仅接受裸 ticker（"QQQ"）；这里剥后缀后发请求，
        # 但保存到结果 dict 时仍用外部传入的 code（保持下游 code 形态一致）。
        alpaca_symbol = code[:-3].upper() if code.upper().endswith(".US") else code.upper()
        frequency_map = {
            "m": TimeFrame.Month,
            "w": TimeFrame.Week,
            "d": TimeFrame.Day,
            "60m": TimeFrame.Hour,
            "30m": TimeFrame(30, TimeFrameUnit.Minute),
            "10m": TimeFrame(10, TimeFrameUnit.Minute),
            "15m": TimeFrame(15, TimeFrameUnit.Minute),
            "5m": TimeFrame(5, TimeFrameUnit.Minute),
            "1m": TimeFrame(1, TimeFrameUnit.Minute),
        }
        timeframe = frequency_map[frequency]

        def _to_datetime(v):
            """统一把 str/datetime 输入转成 datetime；None 时返回 None。"""
            if v is None:
                return None
            if isinstance(v, dt.datetime):
                return v
            if isinstance(v, dt.date):
                return dt.datetime.combine(v, dt.time())
            # str 兜底
            if len(v) == 10:
                return fun.str_to_datetime(v, "%Y-%m-%d")
            return fun.str_to_datetime(v)

        try:
            if end_date is None:
                end_date = dt.datetime.now()
                # 免费 / paper 账号不能拿"最近"数据（SIP），保守往前推一天再截到日界
                end_date = (
                    end_date + dt.timedelta(days=1)
                    if self.is_vip
                    else end_date - dt.timedelta(days=1)
                )
                end_date = fun.str_to_datetime(
                    fun.datetime_to_str(end_date, "%Y-%m-%d"), "%Y-%m-%d"
                )
            else:
                end_date = _to_datetime(end_date)

            if start_date is None:
                if frequency == "1m":
                    start_date = end_date - dt.timedelta(days=60)
                elif frequency == "5m":
                    start_date = end_date - dt.timedelta(days=365)
                elif frequency == "15m":
                    start_date = end_date - dt.timedelta(days=730)
                elif frequency == "30m":
                    start_date = end_date - dt.timedelta(days=1095)
                elif frequency == "60m":
                    start_date = end_date - dt.timedelta(days=1825)
                elif frequency == "120m":
                    start_date = end_date - dt.timedelta(days=1825)
                elif frequency == "d":
                    start_date = end_date - dt.timedelta(days=7300)
                elif frequency == "w":
                    start_date = end_date - dt.timedelta(days=10950)
                elif frequency == "y":
                    start_date = end_date - dt.timedelta(days=18250)
            else:
                start_date = _to_datetime(start_date)

            # 免费 / paper 账户必须指定 feed=IEX，否则 alpaca 默认走 SIP 报：
            # "subscription does not permit querying recent SIP data"。
            req_kwargs = dict(
                symbol_or_symbols=alpaca_symbol,
                timeframe=timeframe,
                start=start_date,
                end=end_date,
                limit=5000,
            )
            if not self.is_vip:
                req_kwargs["feed"] = DataFeed.IEX
            req = StockBarsRequest(**req_kwargs)
            bars = self.client.get_stock_bars(req)
            # bars.data 是 dict[symbol -> List[Bar]]；alpaca 找不到 symbol 时 key 不存在
            bar_list = bars.data.get(alpaca_symbol, []) if hasattr(bars, "data") else []
            klines = []
            for _b in bar_list:
                klines.append(
                    {
                        "code": code,
                        "date": _b.timestamp,
                        "open": _b.open,
                        "close": _b.close,
                        "high": _b.high,
                        "low": _b.low,
                        "volume": _b.volume,
                    }
                )
            klines = pd.DataFrame(klines)
            return klines
        except Exception as e:
            print(f"alpaca 获取行情异常 code={code} alpaca_symbol={alpaca_symbol} Exception ：{str(e)}")
        return None

    def stock_info(self, code: str) -> [Dict, None]:
        """
        获取股票名称，避免网络 api 请求，从 all_stocks 中获取
        """
        stocks = self.all_stocks()
        return next((s for s in stocks if s["code"] == code.upper()), None)

    def ticks(self, codes: List[str]) -> Dict[str, Tick]:
        """
        获取行情Tick数据（IEX feed，非交易时段返回最近一笔）
        """
        code_ticks = {}
        # chanlun 项目美股 symbol 形态是 "{TICKER}.US"，alpaca 仅接受裸 ticker；
        # 这里维护双向映射：发请求用裸 ticker，回填字典 key 用外部 code 形态。
        alpaca_to_orig = {}
        alpaca_codes = []
        for c in codes:
            ac = c[:-3].upper() if c.upper().endswith(".US") else c.upper()
            alpaca_codes.append(ac)
            alpaca_to_orig[ac] = c
        req = StockSnapshotRequest(symbol_or_symbols=alpaca_codes, feed=DataFeed.IEX)
        res = self.client.get_stock_snapshot(req)
        for _ac, _t in res.items():
            _c = alpaca_to_orig.get(_ac, _ac)
            code_ticks[_c] = Tick(
                code=_c,
                last=_t.latest_trade.price,
                buy1=_t.latest_quote.bid_price,
                sell1=_t.latest_quote.ask_price,
                high=_t.daily_bar.high,
                low=_t.daily_bar.low,
                open=_t.daily_bar.open,
                volume=_t.daily_bar.volume,
                rate=round(
                    (_t.daily_bar.close - _t.previous_daily_bar.close)
                    / _t.previous_daily_bar.close
                    * 100,
                    2,
                ),
            )
        return code_ticks

    def now_trading(self):
        """
        返回当前是否是交易时间
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

    @staticmethod
    def __convert_date(_dt):
        _dt = fun.datetime_to_str(_dt, "%Y-%m-%d")
        return fun.str_to_datetime(_dt, "%Y-%m-%d")

    def stock_owner_plate(self, code: str):
        raise Exception("交易所不支持")

    def plate_stocks(self, code: str):
        raise Exception("交易所不支持")

    def balance(self):
        raise Exception("交易所不支持")

    def positions(self, code: str = ""):
        raise Exception("交易所不支持")

    def order(self, code: str, o_type: str, amount: float, args=None):
        raise Exception("交易所不支持")


if __name__ == "__main__":
    ex = ExchangeAlpaca()

    # klines = ex.klines(ex.default_code(), '30m')
    # print(klines.tail())

    ticks = ex.ticks([ex.default_code()])
    print(ticks)
