"""facade —— 见 chanlun.trading.backtest_klines(命名重构,见 dir_structure_audit)。

保留 re-export 保证 `from chanlun.backtesting.backtest_klines import BackTestKlines` 不破。
"""
from chanlun.trading.backtest_klines import *  # noqa: F401,F403
