from pandas.core.api import DataFrame as DataFrame
from wtpy import BaseCtaStrategy, WtBarRecords
from wtpy import CtaContext

from chanlun.backtesting.base import *
from chanlun.core.types import *
from chanlun.cl_utils import query_cl_chart_config


class WTPYMarketData(MarketDatas):
    """
    wtpy CTA 行情数据适配类，将 wtpy CtaContext 的 K 线接口桥接到缠论 MarketDatas。
    """

    def __init__(self, context: CtaContext, frequencys: List[str]):
        self.context: CtaContext = context
        # 以 RB 作为期货缠论配置模板，实际运行时可按品种调整
        cl_config = query_cl_chart_config("futures", "RB")
        super().__init__("futures", frequencys, cl_config)

    @staticmethod
    def bars_to_df_klines(code: str, bars: WtBarRecords) -> pd.DataFrame:
        """
        将 wtpy WtBarRecords 转换为缠论所需的标准 K 线 DataFrame（含 code/date/OHLCV 列）。
        """
        bars_df = bars.to_df()
        bars_df["code"] = code
        bars_df["date"] = pd.to_datetime(bars_df["bartime"])
        return bars_df[["code", "date", "open", "close", "high", "low", "volume"]]

    def klines(self, code, frequency) -> DataFrame:
        df_bars = self.context.stra_get_bars(code, frequency, 2000, isMain=True)
        return self.bars_to_df_klines(code, df_bars)

    def last_k_info(self, code) -> dict:
        kline = self.klines(code, self.frequencys[-1])
        return {
            "date": kline.iloc[-1]["date"],
            "open": float(kline.iloc[-1]["open"]),
            "close": float(kline.iloc[-1]["close"]),
            "high": float(kline.iloc[-1]["high"]),
            "low": float(kline.iloc[-1]["low"]),
        }

    def get_cl_data(self, code, frequency, cl_config: dict = None) -> ICL:
        key = f"{code}_{frequency}"
        if key not in self.cache_cl_datas.keys():
            self.cache_cl_datas[key] = cl.CL(code, frequency, self.cl_config)
        klines = self.klines(code, frequency)
        self.cache_cl_datas[key].process_klines(klines)
        return self.cache_cl_datas[key]


class BaseStrategy(BaseCtaStrategy):
    """
    缠论 wtpy 策略类
    """

    def __init__(self, name: str, strategy: Strategy, code: str, period: str):
        BaseCtaStrategy.__init__(self, name)

        self.code = code
        self.period = period

        self.STR = strategy

        # on_init 时延迟初始化，避免 CtaContext 尚未就绪
        self.datas = None

        # TODO 实盘需要将 positions 持久化，重启后从存储恢复，否则会丢失持仓记录
        self.positions: Dict[str, POSITION] = {}

    def on_init(self, context: CtaContext):
        """
        初始化策略时，初始缠论数据
        """
        if self.datas is None:
            self.datas = WTPYMarketData(context, [self.period])

        context.stra_log_text("Strategy inited")

    def get_poss(self, code) -> List[POSITION]:
        """
        获取代码的持仓记录
        """
        poss = []
        for _k in self.positions.keys():
            if code in poss:
                poss.append(self.positions[_k])
        return poss

    def open_buy(self, context: CtaContext, code: str, amount: float, opt: Operation):
        """
        开仓买入
        """
        res = context.stra_enter_long(code, amount, "enterlong")
        context.stra_log_text(opt.msg)
        pos: POSITION = POSITION(
            code=code,
            mmd=opt.mmd,
            type="long",
            balance=1,
            price=0,
            amount=amount,
            loss_price=opt.loss_price,
            open_msg=opt.msg,
            info=opt.info,
        )
        pos_key = "%s_%s" % (code, opt.mmd)
        self.positions[pos_key] = pos
        return res

    def open_sell(self, context: CtaContext, code: str, amount: float, opt: Operation):
        """
        开仓卖出
        """
        res = context.stra_enter_short(code, amount, "entershort")
        context.stra_log_text(opt.msg)
        pos: POSITION = POSITION(
            code=code,
            mmd=opt.mmd,
            type="short",
            balance=1,
            price=0,
            amount=amount,
            loss_price=opt.loss_price,
            open_msg=opt.msg,
            info=opt.info,
        )
        pos_key = "%s_%s" % (code, opt.mmd)
        self.positions[pos_key] = pos
        return res

    def close_buy(self, context: CtaContext, code, opt: Operation):
        pos_key = "%s_%s" % (code, opt.mmd)
        if pos_key not in self.positions.keys():
            context.stra_log_text("平多仓，没有查找到对应的持仓记录：%s" % pos_key)
            return None
        pos: POSITION = self.positions[pos_key]
        res = context.stra_exit_long(code, pos.amount, "exitlong")
        context.stra_log_text(opt.msg)

        del self.positions[pos_key]
        return res

    def close_sell(self, context: CtaContext, code, opt: Operation):
        pos_key = "%s_%s" % (code, opt.mmd)
        if pos_key not in self.positions.keys():
            context.stra_log_text("平空仓，没有查找到对应的持仓记录：%s" % pos_key)
            return None
        pos: POSITION = self.positions[pos_key]
        res = context.stra_exit_short(code, pos.amount, "exitshort")
        context.stra_log_text(opt.msg)

        del self.positions[pos_key]
        return res

    def on_calculate(self, context: CtaContext):
        """每根 K 线结束时由 wtpy 回调，执行缠论策略的开平仓判断。"""
        for code in [self.code]:
            # 每手固定 1 单位；实盘可根据资金动态调整
            trdUnit = 1

            cds = self.get_cl_datas(code, context)
            curPos = context.stra_get_position(code)

            if curPos == 0:
                open_opts = self.STR.open(code, self.datas)
                for opt in open_opts:
                    if "buy" in opt.mmd:
                        self.open_buy(context, code, trdUnit, opt)
                    elif "sell" in opt.mmd:
                        self.open_sell(context, code, trdUnit, opt)
            elif curPos > 0:
                poss = self.get_poss(code)
                for pos in poss:
                    opt = self.STR.close(code, pos.mmd, pos, self.datas)
                    if opt is not False:
                        self.close_buy(context, code, opt)
            elif curPos < 0:
                poss = self.get_poss(code)
                for pos in poss:
                    opt = self.STR.close(code, pos.mmd, pos, self.datas)
                    if opt is not False:
                        self.close_sell(context, code, opt)
        return

    def on_tick(self, context: CtaContext, stdCode: str, newTick: dict):
        return
