from __future__ import absolute_import, print_function

import datetime
import time

from gm.api import (
    ADJUST_PREV,
    SEC_TYPE_STOCK,
    Context,
    get_instruments,
    history_n,
    schedule,
    stk_get_daily_mktvalue_pt,
    subscribe,
)

from chanlun import fun
from chanlun.backtesting.base import Strategy
from chanlun.cl_utils import query_cl_chart_config
from chanlun.strategy import strategy_demo
from cl_myquant.base import MyQuantData, MyQuantTrader

# 定义全局变量
market_data: MyQuantData  # 数据对象
trader: MyQuantTrader  # 交易对象
strategy: Strategy  # 策略对象
# 使用的缠论配置
cl_config = query_cl_chart_config("a", "SH.000001")


def init(context):
    """掘金策略入口（测试版），初始化对象并注册月度选股定时任务。"""
    global market_data, trader, strategy
    market_data = MyQuantData(context, ["1d"], cl_config)
    strategy = strategy_demo.StrategyDemo()
    trader = MyQuantTrader("a", context)
    trader.set_data(market_data)
    trader.set_strategy(strategy)

    # 每月开盘前（9:20）执行一次选股并重新订阅
    schedule(schedule_func=xuangu_sz, date_rule="1m", time_rule="9:20:00")


def xuangu_sz(context: Context):
    """
    月度选股：筛选上市超 1 年、市值排名前 100 的股票，
    同时保留当前持仓；重新订阅并刷新缠论数据缓存。
    """
    global market_data

    instruments = get_instruments(
        symbols=None,
        exchanges=["SHSE", "SZSE"],
        sec_types=SEC_TYPE_STOCK,
        names=None,
        skip_suspended=True,
        skip_st=True,
        fields=None,
        df=False,
    )
    # 剔除上市不足 1 年的次新股
    symbols = [
        _i["symbol"]
        for _i in instruments
        if _i["listed_date"] < context.now - datetime.timedelta(days=364)
    ]
    print(f"获取所有上市大于1年的股票标的数量{len(symbols)}")

    s = time.time()
    fundamentals = stk_get_daily_mktvalue_pt(
        symbols=symbols,
        fields="tot_mv",
        trade_date=fun.datetime_to_str(context.now, "%Y-%m-%d"),
        df=False,
    )
    print("基本面数据查询用时：", time.time() - s)

    fundamentals = sorted(fundamentals, key=lambda f: f["tot_mv"], reverse=True)
    symbols = [_f["symbol"] for _f in fundamentals[0:100]]
    print(
        f"获取 {fun.datetime_to_str(context.now, '%Y-%m-%d')} 市值前100的股票列表：{symbols}"
    )

    # 持仓标的无条件保留，避免因未被选中而无法触发平仓信号
    positions = context.account().positions()
    pos_symbols = [_p["symbol"] for _p in positions if _p["amount"] > 0]
    print(f"当前持仓股票列表：{pos_symbols}")

    symbols = symbols + pos_symbols
    symbols = list(set(symbols))

    # unsubscribe_previous=True 会取消上月订阅，切换到新标的池
    subscribe(symbols=symbols, frequency="1d", count=2000, unsubscribe_previous=True)

    market_data.init_cl_datas(symbols, ["1d"])

    return symbols


def xuangu_zf(context: Context):
    """
    TODO 涨跌幅排行选股效果不好（不管是涨幅高的还是涨幅低的），仅作为备选方案保留。
    根据近二十日涨幅最低的前 50 只（市值 > 100 亿）股票选股。
    """
    global market_data

    instruments = get_instruments(
        symbols=None,
        exchanges=["SHSE", "SZSE"],
        sec_types=SEC_TYPE_STOCK,
        names=None,
        skip_suspended=True,
        skip_st=True,
        fields=None,
        df=False,
    )
    # 剔除上市不足 1 年的次新股
    symbols = [
        _i["symbol"]
        for _i in instruments
        if _i["listed_date"] < context.now - datetime.timedelta(days=364)
    ]
    print(f"获取所有上市大于1年的股票标的数量{len(symbols)}")

    # 市值过滤：只保留 > 100 亿
    fundamentals = stk_get_daily_mktvalue_pt(
        symbols=symbols,
        trade_date=fun.datetime_to_str(context.now, "%Y-%m-%d"),
        fields="tot_mv",
        df=False,
    )

    fundamentals = sorted(fundamentals, key=lambda f: f["tot_mv"], reverse=True)
    symbols = [_f["symbol"] for _f in fundamentals if _f["tot_mv"] > 10000000000]
    print(
        f"获取 {fun.datetime_to_str(context.now, '%Y-%m-%d')} 市值大于100亿的股票列表：{len(symbols)}"
    )

    # 计算各标的 20 日涨跌幅，缺少完整 20 根 K 线的跳过
    symbol_20day_rank = []
    for symbol in symbols:
        try:
            bar = history_n(
                symbol=symbol,
                frequency="1d",
                count=20,
                end_time=context.now,
                fields="symbol,eob,open,high,low,close",
                adjust=ADJUST_PREV,
                df=True,
            )
            if len(bar) == 20:
                change = (bar["close"].iloc[-1] - bar["close"].iloc[0]) / bar[
                    "close"
                ].iloc[0]
                symbol_20day_rank.append({"symbol": symbol, "change": change})
        except KeyError:
            pass
    # 升序排列后取涨幅最低的前 50（低涨幅策略）
    symbol_20day_rank.sort(key=lambda r: r["change"], reverse=False)
    symbols = [s["symbol"] for s in symbol_20day_rank[0:50]]
    print("排行前50股票代码：", symbols)

    # 持仓标的无条件保留，避免因未被选中而无法触发平仓信号
    positions = context.account().positions()
    pos_symbols = [_p["symbol"] for _p in positions if _p["amount"] > 0]
    print(f"当前持仓股票列表：{pos_symbols}")

    symbols = symbols + pos_symbols
    symbols = list(set(symbols))

    # unsubscribe_previous=True 会取消上月订阅
    subscribe(symbols=symbols, frequency="1d", count=2000, unsubscribe_previous=True)

    market_data.init_cl_datas(symbols, ["1d"])

    return symbols


def on_bar(context, bars):
    """每根 K 线推送时增量更新缠论数据并执行策略信号判断。"""
    global market_data, trader
    market_data.update_bars(bars)
    trader.run(bars[0]["symbol"])
