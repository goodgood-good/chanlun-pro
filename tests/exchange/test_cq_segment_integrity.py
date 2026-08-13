"""锁定长桥 cq 分段拉取源级完整性（C1 修复 阶段 B）。

C1：`_fetch_segment_data` 原返回裸 list,翻页中途 API 失败 break 后返回**部分** candles,
调用方无法区分「到达历史原点(301600,正常)」vs「中途真失败(洞)」;收集层任一段成功即拼接返回
带洞但格式合法的 DataFrame → 写缓存当权威 → 缠论静默算错。

修复:段返回三态 (candles, status ∈ {complete, reached_origin, failed}) + 收集层完整性闸门
(失败段串行补拉一次,仍失败则返回空 DataFrame 且 attrs['fetch_incomplete']=True,绝不返回带洞)。

测试用 ExchangeChangQiao.__new__ 绕过 __init__(会连长桥/建线程池),只测纯逻辑。
"""
import pathlib
import sys
import types
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

_root = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_root / "src"))

import pytz  # noqa: E402
from longbridge.openapi import OpenApiException, Period, AdjustType  # noqa: E402

from chanlun.exchange.exchange_cq import ExchangeChangQiao  # noqa: E402
from chanlun.cl_utils.strict_chart_runtime import build_strict_chart_cd  # noqa: E402

_TZ = pytz.timezone("Asia/Shanghai")


# @fun.singleton 把类包装成工厂 function；__wrapped__(functools.wraps 设置)取回原始类。
_CQ_CLS = ExchangeChangQiao.__wrapped__


def _mk_ex():
    """绕过 __init__，拿一个未初始化实例只调纯逻辑方法。"""
    return _CQ_CLS.__new__(_CQ_CLS)


def _candle(ts_unix):
    return types.SimpleNamespace(
        timestamp=ts_unix, open=1.0, high=1.0, low=1.0, close=1.0, volume=1.0
    )


def _oae(code):
    e = OpenApiException.__new__(OpenApiException)
    e.code = code
    e.message = "test error"  # 供 else 分支 getattr(inner,'message') 短路,避免调 __str__ 访问未设的 kind
    return e


def _seg(ex, api):
    """用给定的 _fetch_candlesticks_api 桩跑一段拉取。"""
    ex._fetch_candlesticks_api = api
    end = _TZ.localize(datetime(2023, 11, 15))
    start = _TZ.localize(datetime(2023, 11, 1))
    return ex._fetch_segment_data("X", Period.Min_5, AdjustType.ForwardAdjust, end, start)


def test_segment_normal_finish_is_complete():
    # 单页 <1000 根 → 自然结束 → complete。
    candles, status = _seg(_mk_ex(), lambda **kw: [_candle(1_700_000_000)])
    assert status == "complete"
    assert len(candles) == 1


def test_segment_empty_return_is_complete():
    # 空返回 = 到该 symbol 历史原点(无更多数据),视为正常 complete,非洞。
    candles, status = _seg(_mk_ex(), lambda **kw: [])
    assert status == "complete"
    assert candles == []


def test_segment_301600_is_reached_origin():
    # 301600 = 合法到历史起点,非洞。
    def _boom(**kw):
        raise _oae(301600)
    candles, status = _seg(_mk_ex(), _boom)
    assert status == "reached_origin"


def test_segment_other_api_error_is_failed():
    # 其他错误码 = 真失败,该段不可信。
    def _boom(**kw):
        raise _oae(500001)
    candles, status = _seg(_mk_ex(), _boom)
    assert status == "failed"


def test_segment_generic_exception_is_failed():
    # 非 OpenApiException 的任意异常(网络/超时) = failed。
    def _boom(**kw):
        raise RuntimeError("network down")
    candles, status = _seg(_mk_ex(), _boom)
    assert status == "failed"


# ---------------------------------------------------------------------------
# 收集层完整性闸门：任一段 failed(补拉后仍失败) → 空 DataFrame + attrs['fetch_incomplete']
# ---------------------------------------------------------------------------

def _klines_ex(monkeypatch, seg_fn):
    """构造能跑 klines 收集层的 ex：真 executor + monkeypatch 段拉取与不依赖 __init__ 的辅助。"""
    ex = _mk_ex()
    ex.executor = ThreadPoolExecutor(2)
    monkeypatch.setattr(ex, "_fetch_segment_data", seg_fn)
    monkeypatch.setattr(ex, "_to_lb_symbol", lambda code: code)
    monkeypatch.setattr(ex, "_market_of_code", lambda code: "a")
    import chanlun.exchange.exchange_cq as cq_mod
    monkeypatch.setattr(cq_mod, "normalize_kline_precision", lambda df, m, c: df)
    return ex


def test_klines_incomplete_segment_returns_empty_with_attr(monkeypatch):
    # 所有段 failed → 拉取不可信 → 空 df 且标记 fetch_incomplete(不返回带洞/不进真空负缓存)。
    ex = _klines_ex(monkeypatch, lambda *a, **kw: ([], "failed"))
    df = ex.klines("SH.600519", "5m", start_date="2023-11-01 00:00:00", end_date="2023-11-15 00:00:00")
    assert df.empty
    assert df.attrs.get("fetch_incomplete") is True


def test_klines_partial_data_but_one_failed_refuses_hole(monkeypatch):
    # 一段有数据、另一段 failed(补拉仍失败) → 带洞 → 宁可返空+标记,绝不返回带洞 DataFrame。
    def _seg(code, period, adjust, e, s, priority="interactive"):
        if e.day <= 5:                       # 较早的段(end≈11-05)失败
            return ([], "failed")
        return ([_candle(int(e.timestamp()))], "complete")
    ex = _klines_ex(monkeypatch, _seg)
    df = ex.klines("SH.600519", "1m", start_date="2023-11-01 00:00:00", end_date="2023-11-15 00:00:00")
    assert df.attrs.get("fetch_incomplete") is True
    assert df.empty


def test_klines_all_complete_has_no_incomplete_attr(monkeypatch):
    # 全 complete + 有数据 → 正常 df,无 fetch_incomplete 标记。
    def _seg(code, period, adjust, e, s, priority="interactive"):
        return ([_candle(int(e.timestamp()))], "complete")
    ex = _klines_ex(monkeypatch, _seg)
    df = ex.klines("SH.600519", "5m", start_date="2023-11-01 00:00:00", end_date="2023-11-15 00:00:00")
    assert not df.attrs.get("fetch_incomplete")


def test_klines_reached_origin_not_treated_as_hole(monkeypatch):
    # 301600 到历史原点是正常数据耗尽,不得判为缺段(否则历史短的新股/美股 1m 频繁误拒)。
    def _seg(code, period, adjust, e, s, priority="interactive"):
        return ([_candle(int(e.timestamp()))], "reached_origin")
    ex = _klines_ex(monkeypatch, _seg)
    df = ex.klines("SH.600519", "5m", start_date="2023-11-01 00:00:00", end_date="2023-11-15 00:00:00")
    assert not df.attrs.get("fetch_incomplete")


def test_us_klines_attach_strict_price_basis_metadata(monkeypatch):
    def _seg(code, period, adjust, e, s, priority="interactive"):
        return ([_candle(int(e.timestamp()))], "complete")

    ex = _klines_ex(monkeypatch, _seg)
    monkeypatch.setattr(ex, "_market_of_code", lambda code: "us")

    df = ex.klines(
        "TSLA.US",
        "5m",
        start_date="2023-11-01 00:00:00",
        end_date="2023-11-15 00:00:00",
    )

    assert not df.empty
    assert df.attrs["structure_price_quantum"] == "0.001"
    assert df.attrs["price_basis_revision"].startswith("sha256:")
    assert df.attrs["price_basis_provider"] == "longbridge"
    assert df.attrs["price_basis_adjustment"] == "forward"

    runtime = build_strict_chart_cd(
        market="us",
        code="TSLA.US",
        frequency="5m",
        frame=df,
    )
    assert runtime.error_code is None
    assert runtime.cd is not None
