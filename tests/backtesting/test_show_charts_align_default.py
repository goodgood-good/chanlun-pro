# -*- coding: utf-8 -*-
"""R2-F2-1: show_charts 的 to_dt_align_type 默认 "bob" 与本仓 DB 分钟线 eob 打点错配。

eob 源 bar 时间戳 T 覆盖 (T-1m,T],bob 分支(KlinesGenerator closed="left")把它分进
[T,T+Nm) 箱 → 合成K线逐根错位一根源 bar(OHLC 错/缺首分钟/卷入前期末分钟/末尾碎片),
其上缠论全失真。KlinesGenerator 类默认 "eob" 本就正确,仅 show_charts 调用方默认覆盖
成 "bob";全库除 currency(前对齐)外均 eob 打点。修复=默认 None → 按市场解析
(currency/currency_spot→bob,其余→eob),显式传参仍尊重。
"""
import inspect

from chanlun.backtesting.backtest import BackTest, _default_dt_align_type


def test_default_dt_align_type_by_market():
    for m in ("a", "us", "hk", "futures"):
        assert _default_dt_align_type(m) == "eob", m
    for m in ("currency", "currency_spot"):
        assert _default_dt_align_type(m) == "bob", m


def test_show_charts_signature_default_is_none():
    """旧码默认 "bob":非 currency 市场合成图错位;新默认 None=运行时按市场解析。"""
    sig = inspect.signature(BackTest.show_charts)
    assert sig.parameters["to_dt_align_type"].default is None