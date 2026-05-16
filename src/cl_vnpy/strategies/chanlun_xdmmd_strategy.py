from cl_vnpy.strategies.base_strategy import *

from chanlun.strategy.strategy_xd_mmd import StrategyXDMMD


class ChanlunXdmmdStrategy(BaseStrategy):
    """
    缠论线段买卖点策略

    按照 1M K线执行 线段的买卖点策略
    """
    author = "WX"
    parameters = []
    variables = []

    def __init__(self, cta_engine, strategy_name, vt_symbol, setting):
        super().__init__(cta_engine, strategy_name, vt_symbol, setting)

        self.cl_config = {'xd_bzh': 'xd_bzh_no'}
        self.frequencys = ['5_1m', '1_1m']

        self.TR = VNPYTrader('backtest', self)
        self.Data = VNPYDatas(self.vt_symbol, self.frequencys, self.cl_config)

        # 注入线段买卖点策略；替换此处即可切换缠论策略
        self.STR: Strategy = StrategyXDMMD()
        self.TR.set_strategy(self.STR)
        self.TR.set_data(self.Data)

        self.bgs: Dict[str, BarGenerator] = {}

        # 大周期在前，保证合成顺序正确
        self.intervals = [
            {'windows': 5, 'interval': Interval.MINUTE, 'callback': self.Data.on_5m_bar},
            {'windows': 1, 'interval': Interval.MINUTE, 'callback': self.Data.on_1m_bar},
        ]

        for interval in self.intervals:
            _key = '%s_%s' % (interval['windows'], interval['interval'].value)
            self.bgs[_key] = BarGenerator(
                self.on_bar,
                window=interval['windows'],
                on_window_bar=interval['callback'],
                interval=interval['interval']
            )
