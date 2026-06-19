"""facade —— 实际实现在 chanlun.trading.backtest_klines。

保留 re-export 保证 `from chanlun.backtesting.backtest_klines import BackTestKlines` 不破。
"""
from chanlun.trading.backtest_klines import *  # noqa: F401,F403
