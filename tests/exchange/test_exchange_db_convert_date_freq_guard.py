# -*- coding: utf-8 -*-
"""R1-F3-1: ExchangeDB.klines 对所有周期无差别 apply __convert_date。

期货夜盘跨零点品种(AU/AG/SC 至 02:30、cu 等至 01:00)分钟数据真实含 00:00:00 bar;
line 222 对每行 apply,futures 分支只判 hour==0&minute==0 → 夜盘 00:00 分钟 bar 被改写
成同日 09:00,与日盘开盘 09:00 bar 重复时间戳/夜盘 OHLC 错插早盘时间轴/00:00 bar 消失。
修复=仅日级及以上周期做规整(镜像 exchange_tdx_futures.py:282 自家成例)。
"""
import datetime
import types

import pytz

import chanlun.exchange.exchange_db as db_mod
from chanlun.exchange.exchange_db import ExchangeDB


def _mk_ex(market):
    ex = object.__new__(ExchangeDB)
    ex.market = market
    ex.tz = pytz.timezone("Asia/Shanghai")
    return ex


def _row(dt_str, c=100.5, code="AU2412"):
    return types.SimpleNamespace(
        code=code, dt=datetime.datetime.fromisoformat(dt_str),
        o=100.0, h=101.0, l=99.0, c=c, v=1000.0, p=10.0,
    )


def test_futures_minute_midnight_bar_not_rewritten(monkeypatch):
    """futures 1m: 夜盘 00:00 bar 保留原时刻,不得改写成 09:00 与日盘 bar 碰撞。"""
    rows = [
        _row("2024-05-13 23:59:00", c=550.0),
        _row("2024-05-14 00:00:00", c=550.5),   # 真实夜盘 bar(旧代码被改写成 09:00)
        _row("2024-05-14 09:00:00", c=560.0),   # 日盘开盘 bar
    ]
    monkeypatch.setattr(db_mod, "db", types.SimpleNamespace(klines_query=lambda *a, **k: rows))
    ex = _mk_ex("futures")
    df = ex.klines("AU2412", "1m")
    times = [d.strftime("%H:%M") for d in df["date"]]
    # 旧代码: ["23:59","09:00","09:00"](00:00 消失+重复 09:00)
    assert times == ["23:59", "00:00", "09:00"], times
    assert len(df) == 3


def test_futures_daily_midnight_still_regularized(monkeypatch):
    """d 周期: 00:00 日线时间戳仍规整到 09:00(原意图保留,零回归)。"""
    rows = [_row("2024-05-14 00:00:00", c=555.0)]
    monkeypatch.setattr(db_mod, "db", types.SimpleNamespace(klines_query=lambda *a, **k: rows))
    ex = _mk_ex("futures")
    df = ex.klines("AU888", "d")
    assert df["date"].iloc[0].strftime("%H:%M") == "09:00"


def test_a_market_daily_regularized_minute_untouched(monkeypatch):
    """A股: d 周期 00:00→15:00 保留;1m 不再 apply(防御一致性)。"""
    rows = [_row("2024-05-14 00:00:00", c=10.0, code="SH.600519")]
    monkeypatch.setattr(db_mod, "db", types.SimpleNamespace(klines_query=lambda *a, **k: rows))
    ex = _mk_ex("a")
    d1 = ex.klines("SH.600519", "d")
    assert d1["date"].iloc[0].strftime("%H:%M") == "15:00"
    d2 = ex.klines("SH.600519", "1m")
    assert d2["date"].iloc[0].strftime("%H:%M") == "00:00"