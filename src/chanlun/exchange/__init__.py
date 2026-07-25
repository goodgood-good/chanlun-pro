import inspect
import threading

from chanlun import config
from chanlun.market import Market
from chanlun.exchange.exchange import Exchange

# 进程级单例缓存，避免每次调用重新初始化（TDX/QMT 初始化耗时且有状态）
g_exchange_obj = {}
# 构造期互斥锁:防止启动期多线程并发首访同一 market 各自构建实例(审查 B-1)
_get_exchange_lock = threading.Lock()


def market_now_trading(ex: Exchange, market) -> bool | None:
    """按市场调用交易时段判断，同时兼容既有无参适配器。

    ``ExchangeChangQiao`` 同一实例承载 HK/US，必须显式传入 market；其余适配器
    仍沿用无参 ``now_trading()``。通过签名分派，避免用 ``except TypeError`` 把
    方法内部的真实类型错误误判为“不支持 market 参数”后再次调用。
    """
    method = ex.now_trading
    try:
        accepts_market = "market" in inspect.signature(method).parameters
    except (TypeError, ValueError):
        accepts_market = False
    if accepts_market:
        market_value = market.value if isinstance(market, Market) else str(market)
        return method(market_value)
    # ExchangeDB 用 None 表示“未知”；保留三态，让原先使用 ``is False`` 的
    # 调度器继续执行，而不是把未知状态误判成明确休市。
    return method()


def get_exchange(market: Market) -> Exchange:
    """根据 config 配置返回指定市场的交易所适配器单例（线程安全 DCL）。"""
    global g_exchange_obj
    if market.value in g_exchange_obj.keys():
        return g_exchange_obj[market.value]
    # g_exchange_obj 是裸 dict,无锁 check-then-act 在启动期多线程并发首访同一 market 时会各自
    # 构建实例(重复 native 连接 / cq 双建泄漏 16-32 worker 线程池)。构造期加进程锁 +
    # double-check,已构建则直接返回。
    with _get_exchange_lock:
        if market.value in g_exchange_obj.keys():
            return g_exchange_obj[market.value]
        _build_exchange(market)
        return g_exchange_obj[market.value]


def _changqiao_market_view(market: Market):
    """复用长桥底层连接，但为每个 registry key 返回稳定的市场视图。"""
    from chanlun.exchange.exchange_cq import (
        ExchangeChangQiao,
        ExchangeChangQiaoMarketView,
    )

    return ExchangeChangQiaoMarketView(ExchangeChangQiao(), market.value)


def _build_exchange(market: Market) -> None:
    """实际构建交易所适配器并写入 g_exchange_obj。必须在 _get_exchange_lock 持锁下调用。"""
    if market == Market.A:
        if config.EXCHANGE_A == "tdx":
            from chanlun.exchange.exchange_tdx import ExchangeTDX

            g_exchange_obj[market.value] = ExchangeTDX()
        elif config.EXCHANGE_A == "futu":
            from chanlun.exchange.exchange_futu import ExchangeFutu

            g_exchange_obj[market.value] = ExchangeFutu()
        elif config.EXCHANGE_A == "baostock":
            from chanlun.exchange.exchange_baostock import ExchangeBaostock

            g_exchange_obj[market.value] = ExchangeBaostock()
        elif config.EXCHANGE_A == "db":
            from chanlun.exchange.exchange_db import ExchangeDB

            g_exchange_obj[market.value] = ExchangeDB(Market.A.value)
        elif config.EXCHANGE_A == "qmt":
            from chanlun.exchange.exchange_qmt import ExchangeQMT

            g_exchange_obj[market.value] = ExchangeQMT()
        elif config.EXCHANGE_A == "cq":
            g_exchange_obj[market.value] = _changqiao_market_view(market)
        elif config.EXCHANGE_A == "usmart":
            from chanlun.exchange.exchange_usmart import ExchangeUSmart

            g_exchange_obj[market.value] = ExchangeUSmart(Market.A.value)
        else:
            raise Exception(f"不支持的沪深交易所 {config.EXCHANGE_A}")

    elif market == Market.HK:
        if config.EXCHANGE_HK == "tdx_hk":
            from chanlun.exchange.exchange_tdx_hk import ExchangeTDXHK

            g_exchange_obj[market.value] = ExchangeTDXHK()
        elif config.EXCHANGE_HK == "futu":
            from chanlun.exchange.exchange_futu import ExchangeFutu

            g_exchange_obj[market.value] = ExchangeFutu()
        elif config.EXCHANGE_HK == "db":
            from chanlun.exchange.exchange_db import ExchangeDB

            g_exchange_obj[market.value] = ExchangeDB(Market.HK.value)
        elif config.EXCHANGE_HK == "cq":
            g_exchange_obj[market.value] = _changqiao_market_view(market)
        elif config.EXCHANGE_HK == "usmart":
            from chanlun.exchange.exchange_usmart import ExchangeUSmart

            g_exchange_obj[market.value] = ExchangeUSmart(Market.HK.value)
        else:
            raise Exception(f"不支持的香港交易所 {config.EXCHANGE_HK}")

    elif market == Market.FUTURES:
        if config.EXCHANGE_FUTURES == "tq":
            from chanlun.exchange.exchange_tq import ExchangeTq

            g_exchange_obj[market.value] = ExchangeTq()
        elif config.EXCHANGE_FUTURES == "tdx_futures":
            from chanlun.exchange.exchange_tdx_futures import ExchangeTDXFutures

            g_exchange_obj[market.value] = ExchangeTDXFutures()
        elif config.EXCHANGE_FUTURES == "db":
            from chanlun.exchange.exchange_db import ExchangeDB

            g_exchange_obj[market.value] = ExchangeDB(Market.FUTURES.value)
        else:
            raise Exception(f"不支持的期货交易所 {config.EXCHANGE_FUTURES}")
    elif market == Market.NY_FUTURES:
        if config.EXCHANGE_NY_FUTURES == "tdx_ny_futures":
            from chanlun.exchange.exchange_tdx_ny_futures import ExchangeTDXNYFutures

            g_exchange_obj[market.value] = ExchangeTDXNYFutures()
        elif config.EXCHANGE_NY_FUTURES == "db":
            from chanlun.exchange.exchange_db import ExchangeDB

            g_exchange_obj[market.value] = ExchangeDB(Market.NY_FUTURES.value)
        else:
            raise Exception(f"不支持的纽约期货交易所 {config.EXCHANGE_NY_FUTURES}")
    elif market == Market.FX:
        if config.EXCHANGE_FX == "tdx_fx":
            from chanlun.exchange.exchange_tdx_fx import ExchangeTDXFX

            g_exchange_obj[market.value] = ExchangeTDXFX()
        elif config.EXCHANGE_FX == "db":
            from chanlun.exchange.exchange_db import ExchangeDB

            g_exchange_obj[market.value] = ExchangeDB(Market.FX.value)
        elif config.EXCHANGE_FX == "cq":
            g_exchange_obj[market.value] = _changqiao_market_view(market)
        else:
            raise Exception(f"不支持的外汇交易所 {config.EXCHANGE_FX}")

    elif market == Market.CURRENCY:
        if config.EXCHANGE_CURRENCY == "binance":
            from chanlun.exchange.exchange_binance import ExchangeBinance

            g_exchange_obj[market.value] = ExchangeBinance()
        elif config.EXCHANGE_CURRENCY == "db":
            from chanlun.exchange.exchange_db import ExchangeDB

            g_exchange_obj[market.value] = ExchangeDB(Market.CURRENCY.value)
        else:
            raise Exception(f"不支持的数字货币交易所 {config.EXCHANGE_CURRENCY}")
    elif market == Market.CURRENCY_SPOT:
        if config.EXCHANGE_CURRENCY_SPOT == "binance_spot":
            from chanlun.exchange.exchange_binance_spot import ExchangeBinanceSpot

            g_exchange_obj[market.value] = ExchangeBinanceSpot()
        elif config.EXCHANGE_CURRENCY_SPOT == "db":
            from chanlun.exchange.exchange_db import ExchangeDB

            g_exchange_obj[market.value] = ExchangeDB(Market.CURRENCY_SPOT.value)
        else:
            raise Exception(f"不支持的数字货币交易所 {config.EXCHANGE_CURRENCY_SPOT}")
    elif market == Market.US:
        if config.EXCHANGE_US == "alpaca":
            from chanlun.exchange.exchange_alpaca import ExchangeAlpaca

            g_exchange_obj[market.value] = ExchangeAlpaca()
        elif config.EXCHANGE_US == "polygon":
            from chanlun.exchange.exchange_polygon import ExchangePolygon

            g_exchange_obj[market.value] = ExchangePolygon()
        elif config.EXCHANGE_US == "ib":
            from chanlun.exchange.exchange_ib import ExchangeIB

            g_exchange_obj[market.value] = ExchangeIB()
        elif config.EXCHANGE_US == "tdx_us":
            from chanlun.exchange.exchange_tdx_us import ExchangeTDXUS

            g_exchange_obj[market.value] = ExchangeTDXUS()
        elif config.EXCHANGE_US == "db":
            from chanlun.exchange.exchange_db import ExchangeDB

            g_exchange_obj[market.value] = ExchangeDB(Market.US.value)
        elif config.EXCHANGE_US == "cq":
            g_exchange_obj[market.value] = _changqiao_market_view(market)
        elif config.EXCHANGE_US == "usmart":
            from chanlun.exchange.exchange_usmart import ExchangeUSmart

            g_exchange_obj[market.value] = ExchangeUSmart(Market.US.value)
        else:
            raise Exception(f"不支持的美股交易所 {config.EXCHANGE_US}")

    return g_exchange_obj[market.value]
