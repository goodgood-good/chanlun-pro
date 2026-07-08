"""R1-C4: tdx_hk / tdx_ny_futures 涨跌幅分母须为昨收/昨结(审查 L2 漏网 3 处)。

rate=(price-pre_close)/price 分母用现价: 涨10%显示9.09%, 跌10%显示-11.11%,
涨得越多低估越狠。已修参照=tdx_fx/tdx_us(审查 L2 注释), 本测试钉死三处兄弟
(hk ticks / ny_futures ticks / ny_futures all_ticks)的分母与零守卫。
"""

import chanlun.exchange.exchange_tdx_hk as hk_mod
import chanlun.exchange.exchange_tdx_ny_futures as ny_mod
from chanlun.exchange.exchange_tdx_hk import ExchangeTDXHK
from chanlun.exchange.exchange_tdx_ny_futures import ExchangeTDXNYFutures


class _FakeConn:
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _FakeExHqApi:
    quote = {}
    quotes_list = []

    def __init__(self, *a, **kw):
        pass

    def connect(self, ip, port):
        return _FakeConn()

    def get_instrument_quote(self, market, code):
        return [dict(type(self).quote)]

    def get_instrument_quote_list(self, market, category, start=0, count=80):
        if start == 0:
            return [dict(q) for q in type(self).quotes_list]
        return []


def _quote_dict(price, pre_close):
    return {
        "price": price, "pre_close": pre_close, "bid1": price, "ask1": price,
        "low": price, "high": price, "zongliang": 1000.0, "open": price,
    }


def _ny_list_quote(mai_chu, zuo_jie):
    return {
        "code": "CL2508", "MaiChu": mai_chu, "ZuoJie": zuo_jie,
        "MaiRuJia": mai_chu, "MaiChuJia": mai_chu, "ZuiDi": mai_chu,
        "ZuiGao": mai_chu, "ZongLiang": 1000.0, "JinKai": mai_chu,
    }


def _real_cls(maybe_wrapped):
    # @fun.singleton 用 @wraps(cls) 包装, 真类挂在 __wrapped__
    return getattr(maybe_wrapped, "__wrapped__", maybe_wrapped)


def _hk_ex():
    ex = object.__new__(_real_cls(ExchangeTDXHK))
    ex.connect_info = {"ip": "127.0.0.1", "port": 7727}
    ex.to_tdx_code = lambda code: (31, "00700")
    return ex


def _ny_ex():
    ex = object.__new__(_real_cls(ExchangeTDXNYFutures))
    ex.connect_info = {"ip": "127.0.0.1", "port": 7727}
    ex.to_tdx_code = lambda code: (47, "CL2508")
    ex.market_maps = {"NYF": {"market": 47, "category": 3}}
    return ex


def test_hk_ticks_rate_uses_pre_close_denominator(monkeypatch):
    monkeypatch.setattr(hk_mod, "TdxExHq_API", _FakeExHqApi)
    _FakeExHqApi.quote = _quote_dict(price=110.0, pre_close=100.0)
    t = _hk_ex().ticks(["KP.00700"])["KP.00700"]
    assert t.rate == 10.0  # 旧分母现价会算成 9.09


def test_hk_ticks_rate_zero_guard_on_pre_close(monkeypatch):
    monkeypatch.setattr(hk_mod, "TdxExHq_API", _FakeExHqApi)
    _FakeExHqApi.quote = _quote_dict(price=110.0, pre_close=0.0)
    t = _hk_ex().ticks(["KP.00700"])["KP.00700"]
    assert t.rate == 0  # 旧实现算出 100.0

def test_ny_ticks_rate_uses_pre_close_denominator(monkeypatch):
    monkeypatch.setattr(ny_mod, "TdxExHq_API", _FakeExHqApi)
    _FakeExHqApi.quote = _quote_dict(price=90.0, pre_close=100.0)
    t = _ny_ex().ticks(["NYF.CL2508"])["NYF.CL2508"]
    assert t.rate == -10.0  # 旧分母现价会算成 -11.11


def test_ny_all_ticks_rate_uses_zuojie_denominator(monkeypatch):
    monkeypatch.setattr(ny_mod, "TdxExHq_API", _FakeExHqApi)
    _FakeExHqApi.quotes_list = [_ny_list_quote(mai_chu=110.0, zuo_jie=100.0)]
    t = _ny_ex().all_ticks()["NYF.CL2508"]
    assert t.rate == 10.0  # 旧分母 MaiChu 会算成 9.09


def test_ny_all_ticks_rate_zero_guard_on_zuojie(monkeypatch):
    monkeypatch.setattr(ny_mod, "TdxExHq_API", _FakeExHqApi)
    _FakeExHqApi.quotes_list = [_ny_list_quote(mai_chu=110.0, zuo_jie=0.0)]
    t = _ny_ex().all_ticks()["NYF.CL2508"]
    assert t.rate == 0  # 旧实现算出 100.0