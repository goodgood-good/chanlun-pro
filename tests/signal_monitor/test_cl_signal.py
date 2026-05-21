"""tests/signal_monitor/test_cl_signal.py — ClSignal 信号对象单测。"""
from __future__ import annotations

from chanlun.signal_monitor.cl_signal import (
    DIRECTION_BULLISH,
    SIGNAL_KIND_BI_BEICHI,
    ClSignal,
    build_line_stable_key,
    make_identity,
)


def test_make_identity_stable_and_discriminating():
    a = make_identity("SH.600000", "30m", "bi_beichi", "bullish", "2024-01-01 10:00:00|down")
    b = make_identity("SH.600000", "30m", "bi_beichi", "bullish", "2024-01-01 10:00:00|down")
    assert a == b
    # 任一字段不同 → identity 不同
    assert a != make_identity("SH.600001", "30m", "bi_beichi", "bullish", "2024-01-01 10:00:00|down")
    assert a != make_identity("SH.600000", "5m", "bi_beichi", "bullish", "2024-01-01 10:00:00|down")
    assert a != make_identity("SH.600000", "30m", "xd_beichi", "bullish", "2024-01-01 10:00:00|down")


def test_signal_identity_property_and_to_dict():
    sig = ClSignal(
        market="a", code="SH.600000", name="测试",
        operation_level="30m", signal_kind=SIGNAL_KIND_BI_BEICHI,
        direction=DIRECTION_BULLISH, line_stable_key="2024-01-01 10:00:00|down",
    )
    assert sig.identity == make_identity(
        "SH.600000", "30m", "bi_beichi", "bullish", "2024-01-01 10:00:00|down")
    assert sig.grade == "C"  # 默认未分级
    d = sig.to_dict()
    assert d["identity"] == sig.identity
    assert d["signal_kind"] == "bi_beichi"
    assert d["direction"] == "bullish"


def test_build_line_stable_key_from_real_line(cl_with_synthetic_klines):
    cd = cl_with_synthetic_klines(150, multi_freq=True)
    bi = cd.get_bis()[-1]
    key = build_line_stable_key(bi)
    assert "|" in key
    assert key.endswith(bi.type)
