"""facade —— 实际实现在 chanlun.trading.backtest_trader。

保留 re-export 保证实盘交易器继承链
`from chanlun.backtesting.backtest_trader import BackTestTrader` 不破。
新代码请直接用 chanlun.trading.backtest_trader。
"""
from chanlun.trading.backtest_trader import *  # noqa: F401,F403
