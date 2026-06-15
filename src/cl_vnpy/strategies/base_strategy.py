from vnpy.trader.constant import Interval
from vnpy_ctastrategy import (
    CtaTemplate,
    StopOrder,
    TickData,
    BarData,
    TradeData,
    OrderData,
    BarGenerator,
)

from chanlun.trading.backtest_trader import BackTestTrader
from chanlun.trading.base import *
from chanlun.strategy.strategy_demo import StrategyDemo


class VNPYTrader(BackTestTrader):
    """
    vnpy CTA 交易适配类，将缠论信号转换为 vnpy 买卖/平仓指令。
    """

    def __init__(self, name, cta):
        super().__init__(name, "online", market="futures")
        self.cta = cta

        # 固定每次下单手数；实盘时可根据资金动态计算
        self.fixed_amount = 1

    def open_buy(self, code, opt: Operation):
        """
        买入开仓
        """
        price_info = self.datas.last_k_info(code)
        price = price_info["close"]
        self.cta.write_log(
            "买入开仓做多，价格 %s 数量 %s 交易信号 %s"
            % (price, self.fixed_amount, opt.msg)
        )
        self.cta.buy(price, self.fixed_amount)
        return {"price": price, "amount": self.fixed_amount}

    def open_sell(self, code, opt: Operation):
        """
        卖出开仓
        """
        price_info = self.datas.last_k_info(code)
        price = price_info["close"]

        self.cta.write_log(
            "卖出开仓做空，价格 %s 数量 %s 交易信号 %s"
            % (price, self.fixed_amount, opt.msg)
        )
        self.cta.short(price, self.fixed_amount)
        return {"price": price, "amount": self.fixed_amount}

    def close_buy(self, code, pos: POSITION, opt: Operation):
        """
        平多仓
        """
        price_info = self.datas.last_k_info(code)
        price = price_info["close"]

        self.cta.write_log(
            "卖出平仓做多，价格 %s 数量 %s 交易信号 %s" % (price, pos.amount, opt.msg)
        )
        self.cta.sell(price, pos.amount)
        return {"price": price, "amount": pos.amount}

    def close_sell(self, code, pos: POSITION, opt: Operation):
        """
        平空仓
        """
        price_info = self.datas.last_k_info(code)
        price = price_info["close"]
        self.cta.write_log(
            "买入平仓做空，价格 %s 数量 %s 交易信号 %s" % (price, pos.amount, opt.msg)
        )
        self.cta.cover(price, pos.amount)
        return {"price": price, "amount": pos.amount}


class VNPYDatas(MarketDatas):
    """
    vnpy CTA 行情数据适配类；vnpy 单标的策略中 code 参数无实际筛选意义，
    以 symbol 固定标识当前合约。
    """

    def __init__(self, symbol, frequencys: List[str], cl_config: dict):
        super().__init__("futures", frequencys, cl_config)

        self.symbol = symbol
        self.frequencys = frequencys
        self.now_date = None
        # 按周期键缓存 K 线数据，由各 on_Xm_bar 回调追加
        self.cache_klines: Dict[str, pd.DataFrame] = {}
        for f in self.frequencys:
            self.cache_klines[f] = pd.DataFrame(
                [], columns=["code", "date", "open", "close", "high", "low", "volume"]
            )

    def on_30m_bar(self, bar: BarData):
        """
        30M 周期 bar 生成后的回调
        """
        key = "30_1m"
        k = {
            "code": self.symbol,
            "date": bar.datetime,
            "open": bar.open_price,
            "close": bar.close_price,
            "high": bar.high_price,
            "low": bar.low_price,
            "volume": bar.volume,
        }
        self.now_date = bar.datetime
        self.cache_klines[key] = self.cache_klines[key].append(k, ignore_index=True)
        return True

    def on_15m_bar(self, bar: BarData):
        """
        15m 周期 bar 生成后的回调
        """
        key = "15_1m"
        k = {
            "code": self.symbol,
            "date": bar.datetime,
            "open": bar.open_price,
            "close": bar.close_price,
            "high": bar.high_price,
            "low": bar.low_price,
            "volume": bar.volume,
        }
        self.now_date = bar.datetime
        self.cache_klines[key] = self.cache_klines[key].append(k, ignore_index=True)
        return True

    def on_10m_bar(self, bar: BarData):
        """
        10m 周期 bar 生成后的回调
        """
        key = "10_1m"
        k = {
            "code": self.symbol,
            "date": bar.datetime,
            "open": bar.open_price,
            "close": bar.close_price,
            "high": bar.high_price,
            "low": bar.low_price,
            "volume": bar.volume,
        }
        self.now_date = bar.datetime
        self.cache_klines[key] = self.cache_klines[key].append(k, ignore_index=True)
        return True

    def on_5m_bar(self, bar: BarData):
        """
        5m 周期 bar 生成后的回调
        """
        key = "5_1m"
        k = {
            "code": self.symbol,
            "date": bar.datetime,
            "open": bar.open_price,
            "close": bar.close_price,
            "high": bar.high_price,
            "low": bar.low_price,
            "volume": bar.volume,
        }
        self.now_date = bar.datetime
        self.cache_klines[key] = self.cache_klines[key].append(k, ignore_index=True)
        return True

    def on_1m_bar(self, bar: BarData):
        """
        1m 周期 bar 生成后的回调
        """
        key = "1_1m"
        k = {
            "code": self.symbol,
            "date": bar.datetime,
            "open": bar.open_price,
            "close": bar.close_price,
            "high": bar.high_price,
            "low": bar.low_price,
            "volume": bar.volume,
        }
        self.now_date = bar.datetime
        self.cache_klines[key] = self.cache_klines[key].append(k, ignore_index=True)
        return True

    def klines(self, code, frequency) -> pd.DataFrame:
        return self.cache_klines[frequency]

    def last_k_info(self, code) -> dict:
        f = self.frequencys[-1]
        return {
            "date": self.cache_klines[f].iloc[-1]["date"],
            "open": float(self.cache_klines[f].iloc[-1]["open"]),
            "close": float(self.cache_klines[f].iloc[-1]["close"]),
            "high": float(self.cache_klines[f].iloc[-1]["high"]),
            "low": float(self.cache_klines[f].iloc[-1]["low"]),
        }

    def get_cl_data(self, code, frequency, cl_config: dict = None) -> ICL:
        """按周期获取缠论数据对象，首次调用时创建，后续增量更新。"""
        klines = self.klines(code, frequency)
        if frequency not in self.cl_datas.keys():
            self.cl_datas[frequency] = cl.CL(
                code, frequency, self.cl_config
            ).process_klines(klines)
        else:
            self.cl_datas[frequency].process_klines(klines)
        return self.cl_datas[frequency]


class BaseStrategy(CtaTemplate):
    """
    缠论 vnpy 单标的多周期 CTA 策略基类；子类通过替换 STR 注入不同缠论策略。
    """

    author = "WX"
    parameters = []
    variables = []

    def __init__(self, cta_engine, strategy_name, vt_symbol, setting):
        super().__init__(cta_engine, strategy_name, vt_symbol, setting)
        # 缠论计算配置；xd_bzh_no 表示线段不做笔标准化处理
        self.cl_config = {"xd_bzh": "xd_bzh_no"}
        self.frequencys = ["5_1m", "1_1m"]

        self.TR = VNPYTrader("backtest", self)
        self.Data = VNPYDatas(self.vt_symbol, self.frequencys, self.cl_config)

        # 子类可覆盖 STR 以切换不同缠论策略
        self.STR: Strategy = StrategyDemo()
        self.TR.set_strategy(self.STR)
        self.TR.set_data(self.Data)

        self.bgs: Dict[str, BarGenerator] = {}

        # intervals 列表决定多周期合成顺序，大周期在前以保证回调先于小周期触发
        self.intervals = [
            {
                "windows": 5,
                "interval": Interval.MINUTE,
                "callback": self.Data.on_5m_bar,
            },
            {
                "windows": 1,
                "interval": Interval.MINUTE,
                "callback": self.Data.on_1m_bar,
            },
        ]

        for interval in self.intervals:
            _key = "%s_%s" % (interval["windows"], interval["interval"].value)
            self.bgs[_key] = BarGenerator(
                self.on_bar,
                window=interval["windows"],
                on_window_bar=interval["callback"],
                interval=interval["interval"],
            )

    def on_init(self):
        """策略初始化回调：预加载历史数据以填充各周期 BarGenerator 缓冲区。"""
        self.write_log("策略初始化")

        def update_bar(bar: BarData):
            for _, bg in self.bgs.items():
                bg.update_bar(bar)

        # 预加载 5 天历史数据用于填充 BarGenerator，这部分数据不参与策略信号计算
        self.load_bar(5, callback=update_bar)

    def on_start(self):
        """策略启动回调。"""
        self.write_log("策略启动")

    def on_stop(self):
        """策略停止回调。"""
        self.write_log("策略停止")

    def on_tick(self, tick: TickData):
        """
        实盘 tick 回调，仅用于驱动 BarGenerator 生成 1m bar；
        只需喂给第一个 bg，后续周期由 on_bar 统一推进。
        """
        for _key, _bg in self.bgs.items():
            _bg.update_tick(tick)
            break

    def on_bar(self, bar: BarData):
        """1m bar 推送时驱动所有周期 BarGenerator，并触发缠论策略计算。"""
        for _, bg in self.bgs.items():
            bg.update_bar(bar)

        self.TR.run(self.vt_symbol)

        self.put_event()

    def on_order(self, order: OrderData):
        pass

    def on_trade(self, trade: TradeData):
        self.put_event()

    def on_stop_order(self, stop_order: StopOrder):
        pass
