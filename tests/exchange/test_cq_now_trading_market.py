"""R7-X1: cq(@fun.singleton, us/hk 共享同一实例)的 now_trading 原硬编码 Market.US → 港股
(默认 EXCHANGE_HK=cq)盘中恒被判成美股时段 → web tv_history 缓存 serve-stale 阈值误用 CLOSED
(soft 3600s vs TRADING 300s)致港股盘中可下发~1h 陈旧缠论。修复: now_trading(market) 按 market
选对应 trading_session(Market.HK/US)+ 本地时区。"""

import datetime

from longbridge.openapi import Market

from chanlun.exchange.exchange_cq import ExchangeChangQiao

_FULL_DAY = [type("TI", (), {"begin_time": datetime.time(0, 0, 0),
                             "end_time": datetime.time(23, 59, 59, 999999)})()]


class _FakeSession:
    def __init__(self, market, trade_sessions):
        self.market = market
        self.trade_sessions = trade_sessions


def _mk_cq(sessions):
    inst = object.__new__(ExchangeChangQiao.__wrapped__)
    ctx = type("Ctx", (), {"trading_session": lambda self: sessions})()
    inst._quote_ctx = lambda: ctx
    inst._quote_call = lambda fn, timeout=2.0: fn()
    return inst


def test_now_trading_hk_reads_hk_session_not_us():
    # HK 全天开(→True), US 空(→False): 证 now_trading 按 market 选对应会话
    cq = _mk_cq([_FakeSession(Market.US, []), _FakeSession(Market.HK, _FULL_DAY)])
    assert cq.now_trading("hk") is True   # 修复前: 读 US 空 → False(港股盘中误判收盘)
    assert cq.now_trading("us") is False


def test_now_trading_us_reads_us_session():
    cq = _mk_cq([_FakeSession(Market.US, _FULL_DAY), _FakeSession(Market.HK, [])])
    assert cq.now_trading("us") is True
    assert cq.now_trading("hk") is False


def test_now_trading_default_market_us_backcompat():
    # 无参调用(向后兼容其余直调方)默认按 us
    cq = _mk_cq([_FakeSession(Market.US, _FULL_DAY), _FakeSession(Market.HK, [])])
    assert cq.now_trading() is True
