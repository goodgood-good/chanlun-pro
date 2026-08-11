"""R3-C2/C3: exchange_tdx_futures 涨跌幅分母 + K线缓存 key 对称。

C2: ticks/all_ticks 的 rate 分母原用现价(price)/卖出价(MaiChu), 应为昨收(pre_close)/
    昨结(ZuoJie)。与 tdx_fx/tdx_us/tdx_hk/ny_futures 审查 L2 修复口径一致。
C3: klines() 读写缓存必须使用同一个 code 键，否则缓存永不命中、每次重拉全量。
    修复后读写 key 一致(均 code, 与其它 tdx 交易所约定一致)。
"""

import pandas as pd
import pytz

import chanlun.exchange.exchange_tdx_futures as fut_mod
from chanlun.exchange.exchange_tdx_futures import ExchangeTDXFutures


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


def _list_quote(mai_chu, zuo_jie):
    return {
        "code": "MA2509", "MaiChu": mai_chu, "ZuoJie": zuo_jie,
        "MaiRuJia": mai_chu, "MaiChuJia": mai_chu, "ZuiDi": mai_chu,
        "ZuiGao": mai_chu, "ZongLiang": 1000.0, "JinKai": mai_chu,
    }


def _real_cls(maybe_wrapped):
    # @fun.singleton 用 @wraps(cls) 包装, 真类挂在 __wrapped__
    return getattr(maybe_wrapped, "__wrapped__", maybe_wrapped)


def _fut_ex():
    ex = object.__new__(_real_cls(ExchangeTDXFutures))
    ex.connect_info = {"ip": "127.0.0.1", "port": 7727}
    ex.to_tdx_code = lambda code: (28, "MA2509")
    ex.market_maps = {"MA": {"market": 28, "category": 3}}
    ex.tz = pytz.timezone("Asia/Shanghai")
    return ex


# ---- C2: rate 分母 ----

def test_futures_ticks_rate_uses_pre_close_denominator(monkeypatch):
    monkeypatch.setattr(fut_mod, "TdxExHq_API", _FakeExHqApi)
    _FakeExHqApi.quote = _quote_dict(price=110.0, pre_close=100.0)
    t = _fut_ex().ticks(["MA.MA2509"])["MA.MA2509"]
    assert t.rate == 10.0  # 旧分母现价会算成 9.09


def test_futures_ticks_rate_zero_guard_on_pre_close(monkeypatch):
    monkeypatch.setattr(fut_mod, "TdxExHq_API", _FakeExHqApi)
    _FakeExHqApi.quote = _quote_dict(price=110.0, pre_close=0.0)
    t = _fut_ex().ticks(["MA.MA2509"])["MA.MA2509"]
    assert t.rate == 0  # pre_close=0 触发零守卫


def test_futures_all_ticks_rate_uses_zuojie_denominator(monkeypatch):
    monkeypatch.setattr(fut_mod, "TdxExHq_API", _FakeExHqApi)
    _FakeExHqApi.quotes_list = [_list_quote(mai_chu=110.0, zuo_jie=100.0)]
    t = _fut_ex().all_ticks()["MA.MA2509"]
    assert t.rate == 10.0  # 旧分母 MaiChu 会算成 9.09


def test_futures_all_ticks_rate_zero_guard_on_zuojie(monkeypatch):
    monkeypatch.setattr(fut_mod, "TdxExHq_API", _FakeExHqApi)
    _FakeExHqApi.quotes_list = [_list_quote(mai_chu=110.0, zuo_jie=0.0)]
    t = _fut_ex().all_ticks()["MA.MA2509"]
    assert t.rate == 0  # MaiChu>0 不早退, 分母 ZuoJie=0 触发零守卫→0


# ---- C3: 缓存 key 读写对称 ----

class _SpyFdb:
    def __init__(self):
        self.read_key = None
        self.save_key = None

    def get_tdx_klines(self, market, code, freq):
        self.read_key = code
        return None  # 强制走拉取+回写路径

    def save_tdx_klines(self, market, code, freq, klines):
        self.save_key = code


class _FakeKlineApi:
    def __init__(self, *a, **kw):
        pass

    def connect(self, ip, port):
        return _FakeConn()

    def get_instrument_bars(self, fmap, market, tdx_code, start, count):
        return []

    def to_df(self, raw):
        return pd.DataFrame(
            {
                "datetime": ["2024-01-02 09:00", "2024-01-02 09:05"],
                "open": [1.0, 2.0], "close": [1.0, 2.0],
                "high": [1.0, 2.0], "low": [1.0, 2.0], "trade": [10.0, 20.0],
            }
        )


def test_futures_klines_cache_key_read_write_symmetric(monkeypatch):
    monkeypatch.setattr(fut_mod, "TdxExHq_API", _FakeKlineApi)
    ex = _fut_ex()
    spy = _SpyFdb()
    ex.fdb = spy
    ex.fix_yp_date = lambda code, dt: dt
    ex.klines("MA.MA2509", "5m", args={"pages": 1})
    assert spy.read_key == "MA.MA2509"
    assert spy.save_key == "MA.MA2509"
    assert spy.read_key == spy.save_key
