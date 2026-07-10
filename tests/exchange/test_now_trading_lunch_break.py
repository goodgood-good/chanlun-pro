# -*- coding: utf-8 -*-
"""R13-#2: 期货 now_trading() 上午收盘误判。

exchange_tq / exchange_tdx_futures 的 now_trading 用 hour in {9,10,11,...},
hour==11 使 11:00-11:59 整段判为交易时段,但期货上午 11:30 收盘 → 11:30-11:59
误判为交易中。经 signal_monitor/scheduler.py:38 `if ex.now_trading() is False`
可达(EXCHANGE_FUTURES 默认 tdx_futures):11:30-11:59 会让信号监控任务在期货
已休市时误扫。修复=移除集合中的 11,加 (hour == 11 and minute < 30)
(与 exchange_baostock.py:115 权威口径一致)。
"""
import sys
import types

import pytest

# ---- stub tqsdk(exchange_tq 依赖 futures extras) ----
if "tqsdk" not in sys.modules:
    _tq = types.ModuleType("tqsdk")
    _objs = types.ModuleType("tqsdk.objs")
    for _n in ("Account", "Position", "Quote"):
        setattr(_objs, _n, type(_n, (), {}))
    _tq.objs = _objs
    for _n in ("TqApi", "TqAccount", "TqAuth", "TqKq"):
        setattr(_tq, _n, type(_n, (), {}))
    sys.modules["tqsdk"] = _tq
    sys.modules["tqsdk.objs"] = _objs

import chanlun.exchange.exchange_tdx_futures as fut_mod  # noqa: E402
import chanlun.exchange.exchange_tq as tq_mod  # noqa: E402


def _unwrap(obj):
    return getattr(obj, "__wrapped__", obj)


_CASES = [
    (_unwrap(fut_mod.ExchangeTDXFutures), fut_mod, "tdx_futures"),
    (_unwrap(tq_mod.ExchangeTq), tq_mod, "tq"),
]


def _call(cls, mod, hour, minute, monkeypatch):
    monkeypatch.setattr(
        mod.time,
        "strftime",
        lambda fmt: f"{hour:02d}" if fmt == "%H" else f"{minute:02d}",
    )
    return cls.now_trading(object())


@pytest.mark.parametrize("cls,mod,name", _CASES)
def test_now_trading_lunch_break_closed(cls, mod, name, monkeypatch):
    # 11:30-11:59 期货上午已收盘 → False(修复前 hour==11 恒 True 误判)
    assert _call(cls, mod, 11, 45, monkeypatch) is False, f"{name} 11:45 应休市"
    assert _call(cls, mod, 11, 30, monkeypatch) is False, f"{name} 11:30 收盘应休市"
    assert _call(cls, mod, 11, 59, monkeypatch) is False, f"{name} 11:59 应休市"


@pytest.mark.parametrize("cls,mod,name", _CASES)
def test_now_trading_morning_open_preserved(cls, mod, name, monkeypatch):
    # 11:00-11:29 仍在上午盘 → True(回归: 修复不能误伤)
    assert _call(cls, mod, 11, 0, monkeypatch) is True, f"{name} 11:00 应交易"
    assert _call(cls, mod, 11, 15, monkeypatch) is True, f"{name} 11:15 应交易"
    # 其它时段 sanity
    assert _call(cls, mod, 10, 0, monkeypatch) is True, f"{name} 10:00 应交易"
    assert _call(cls, mod, 14, 0, monkeypatch) is True, f"{name} 14:00 应交易"