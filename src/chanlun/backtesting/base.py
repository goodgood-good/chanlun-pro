"""facade —— 交易地基已移至 chanlun.trading.base。

保留此 re-export 保证调用方(实盘 trader_*/券商 cl_myquant·vnpy·wtpy/xuangu/
strategy/monitor/kcharts)的 `from chanlun.backtesting.base import ...` 及旧 pickle
(chanlun.backtesting.base.POSITION 等)继续可解析。新代码请直接用 chanlun.trading.base。
"""
from chanlun.trading.base import *  # noqa: F401,F403
