import datetime as dt
import os

try:
    from alpaca.data import StockBarsRequest, StockSnapshotRequest, DataFeed
    from alpaca.data.historical import StockHistoricalDataClient
    from alpaca.data.timeframe import TimeFrame, TimeFrameUnit
except ImportError as _e:
    raise ImportError(
        "ExchangeAlpaca requires extras: pip install 'chanlun-pro[us]' "
        "(or `poetry install --extras us`)"
    ) from _e
from chanlun import config
from chanlun import fun
from chanlun.exchange.exchange import *
from chanlun.exchange.kline_precision import normalize_kline_precision

g_all_stocks = []


# 分钟级 K 线 RTH (Regular Trading Hours) 过滤窗口：美东 09:30-16:00 EDT。
# Alpaca IEX feed 默认会返回 04:00-09:30 盘前 + 16:00-20:00 盘后数据；
# 缠论项目希望图表只看正常交易时段，否则 TV 上"今日第一根"会落在 NY 08:00
# (=Shanghai 20:00) 之类的盘前 bar，与用户期望的开盘 09:30 第一根不符。
# 日 K / 周 K / 月 K 的 timestamp 是美东 00:00，不在过滤窗口内，必须跳过过滤，
# 否则会被全部清空。
_RTH_MINUTE_FREQS = frozenset({"1m", "5m", "10m", "15m", "30m", "60m", "120m"})

# N1: bar 区间长度 (分钟), 用于"bar 区间与 RTH 窗口有交集"判定。
# alpaca 60m bar 的 timestamp 是 NY 09:00 起点, 跨 09:00-10:00 覆盖了 09:30 开盘
# —— 用"bar 起点必须 in [09:30,16:00)"判定会丢弃这根关键开盘 bar。
# 改成"bar 区间与 [09:30,16:00) 有交集即保留"。
_RTH_FREQ_MINUTES = {
    "1m": 1, "5m": 5, "10m": 10, "15m": 15, "30m": 30, "60m": 60, "120m": 120,
}


def _filter_alpaca_rth_bars(bar_list, frequency):
    """对分钟级 bar 列表做 RTH 过滤。

    - frequency 不在 _RTH_MINUTE_FREQS 集合时 (如 'd' / 'w' / 'm') 原样返回。
    - bar.timestamp 必须是 datetime; 其它类型放行避免误删未识别数据。
    - 周末 bar 兜底过滤 (alpaca 一般不返回, 这里属于防御性删除)。
    - **(N1 修复)** 按"bar 区间 [start, start+freq) 与 [09:30, 16:00) 有交集"
      判定。此前用"bar 起点 in [09:30, 16:00)" 会丢弃 60m 09:00 起点的 bar
      (虽然这根 bar 覆盖了 09:30 开盘真实数据)。

    抽成模块级函数是为了: 1) 单测时不依赖 alpaca SDK / ExchangeAlpaca 单例;
    2) RTH 边界策略变更可在这里集中改。
    """
    if frequency not in _RTH_MINUTE_FREQS or not bar_list:
        return bar_list
    ny_tz = pytz.timezone("America/New_York")
    freq_minutes = _RTH_FREQ_MINUTES.get(frequency, 1)
    rth_start_min = 9 * 60 + 30  # 09:30 → 570
    rth_end_min = 16 * 60        # 16:00 → 960

    def _is_rth(bar) -> bool:
        ts = getattr(bar, "timestamp", None)
        if not isinstance(ts, dt.datetime):
            return True
        ny_dt = (
            ts.astimezone(ny_tz)
            if ts.tzinfo is not None
            else pytz.utc.localize(ts).astimezone(ny_tz)
        )
        if ny_dt.weekday() >= 5:
            return False
        bar_start_min = ny_dt.hour * 60 + ny_dt.minute
        bar_end_min = bar_start_min + freq_minutes
        # 区间相交: bar_end > rth_start AND bar_start < rth_end
        return bar_end_min > rth_start_min and bar_start_min < rth_end_min

    return [b for b in bar_list if _is_rth(b)]


@fun.singleton
class ExchangeAlpaca(Exchange):
    """Alpaca-backed US historical market-data adapter."""

    def __init__(self):
        super().__init__()

        self.client = StockHistoricalDataClient(
            api_key=config.ALPACA_APIKEY, secret_key=config.ALPACA_SECRET
        )

        self.tz = pytz.timezone("US/Eastern")

        # 免费/paper 账户只能查 15 分钟延迟的 IEX 数据；付费账户可实时获取
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
            # 字符串是 Exchange 公共接口的标准日期输入。
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
                # 回看时长从统一表读取，修改请改 _lookback.py
                from chanlun.exchange._lookback import get_lookback_timedelta

                start_date = end_date - get_lookback_timedelta(frequency)
            else:
                start_date = _to_datetime(start_date)

            # 免费 / paper 账户必须指定 feed=IEX，否则 alpaca 默认走 SIP 报：
            # "subscription does not permit querying recent SIP data"。
            # 不传 limit：StockBarsRequest.limit 是整个调用总条数上限而非每页大小，
            # 传入后会截断分页，导致长区间 bar 不完整。省略则自动分页拉全区间。
            req_kwargs = dict(
                symbol_or_symbols=alpaca_symbol,
                timeframe=timeframe,
                start=start_date,
                end=end_date,
            )
            if not self.is_vip:
                req_kwargs["feed"] = DataFeed.IEX
            req = StockBarsRequest(**req_kwargs)
            bars = self.client.get_stock_bars(req)
            # bars.data 是 dict[symbol -> List[Bar]]；alpaca 找不到 symbol 时 key 不存在
            bar_list = bars.data.get(alpaca_symbol, []) if hasattr(bars, "data") else []

            # 分钟级 K 线只保留美东 RTH (09:30-16:00 EDT)；详见模块级
            # _filter_alpaca_rth_bars 文档。日 K / 周 K / 月 K 不受影响。
            bar_list = _filter_alpaca_rth_bars(bar_list, frequency)

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
            klines = normalize_kline_precision(klines, "us", code)
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
                rate=0 if not (_t.previous_daily_bar and _t.previous_daily_bar.close) else round(  # B4(Round8): prev None/0 守零防整批崩
                    (_t.daily_bar.close - _t.previous_daily_bar.close)
                    / _t.previous_daily_bar.close
                    * 100,
                    2,
                ),
            )
        return code_ticks

    def now_trading(self, market: str):
        """
        返回当前是否是交易时间
        """
        tz = pytz.timezone("US/Eastern")
        now = dt.datetime.now(tz)
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
