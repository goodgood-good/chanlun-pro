"""chanlun.trading —— 交易框架核心地基。

包含交易抽象基类(POSITION/Operation/MarketDatas/Trader/Strategy)、被实盘交易器
继承的 BackTestTrader、回测 K 线数据源 BackTestKlines、期货合约规格 futures_contracts。
原 chanlun.backtesting.{base,backtest_trader,backtest_klines,futures_contracts} 路径
保留为 facade re-export,兼容调用方(实盘 trader_*/券商/xuangu)与旧 pickle。
"""
