"""facade —— 见 chanlun.trading.backtest_trader(命名重构,见 dir_structure_audit)。

保留 re-export 保证实盘交易器继承链
`from chanlun.backtesting.backtest_trader import BackTestTrader` 不破。
新代码请直接用 chanlun.trading.backtest_trader。
"""
from chanlun.trading.backtest_trader import *  # noqa: F401,F403
