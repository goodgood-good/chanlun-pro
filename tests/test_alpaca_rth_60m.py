"""tests/test_alpaca_rth_60m.py — N1 验证 60m 09:00 bar 不再被丢弃。

历史 bug: 原 RTH 过滤用"bar 起点 in [09:30, 16:00)" 判定, 60m bar 的
timestamp 是 NY 09:00 起点 (覆盖 09:00-10:00 包含 09:30 开盘), 起点不在
[09:30, 16:00) 区间内被错误丢弃 → 美股 60m 图开盘第一根缺失。

修复后: 按"bar 区间 [start, start+freq) 与 [09:30, 16:00) 有交集"判定,
09:00 起点的 60m bar 区间 [09:00, 10:00) 与 [09:30, 16:00) 有交集 → 保留。

测试通过 sys.modules['alpaca']=None 屏蔽 SDK 模拟未装环境也能跑;
仅测试纯 _filter_alpaca_rth_bars 函数, 不依赖 ExchangeAlpaca 类。
"""

from __future__ import annotations

import datetime
import importlib
import sys

import pytest
import pytz


@pytest.fixture
def filter_func(monkeypatch):
    """通过 monkeypatch sys.modules 让 exchange_alpaca 顶层 SDK import 成功
    (用 stub MagicMock 充当 SDK 模块), 然后 import 拿到 _filter_alpaca_rth_bars。
    """
    from unittest.mock import MagicMock

    # 防止本机已装的 alpaca 真模块干扰: 提供 stub
    stubs = {
        "alpaca": MagicMock(),
        "alpaca.data": MagicMock(),
        "alpaca.data.historical": MagicMock(),
        "alpaca.data.timeframe": MagicMock(),
    }
    for k, v in stubs.items():
        monkeypatch.setitem(sys.modules, k, v)

    # 强制重新加载 exchange_alpaca, 让 try/except SDK import 走 stub 分支
    sys.modules.pop("chanlun.exchange.exchange_alpaca", None)
    mod = importlib.import_module("chanlun.exchange.exchange_alpaca")
    return mod._filter_alpaca_rth_bars


def _bar(ny_hour: int, ny_minute: int = 0, day: int = 4):
    """day=4 是周五 (2024-01-05), 周末过滤不会影响测试。"""

    class _Bar:
        pass

    b = _Bar()
    ny = pytz.timezone("America/New_York")
    b.timestamp = ny.localize(datetime.datetime(2024, 1, day, ny_hour, ny_minute, 0))
    return b


def test_60m_open_bar_at_0900_preserved(filter_func):
    """N1 核心: 60m bar 起点 NY 09:00 (覆盖开盘) 必须保留。"""
    bars = [_bar(9, 0), _bar(10, 0), _bar(15, 0)]
    out = filter_func(bars, "60m")
    assert len(out) == 3, "60m 09:00 起点 bar 应被保留 (区间 [09:00,10:00) 与 RTH 有交集)"


def test_60m_pre_market_0800_dropped(filter_func):
    """60m bar 起点 NY 08:00 (区间 [08:00, 09:00)) 与 [09:30, 16:00) 无交集 → drop。"""
    bars = [_bar(8, 0)]
    out = filter_func(bars, "60m")
    assert out == [], "60m 08:00 起点 bar 区间 [08:00,09:00) 不该有交集"


def test_60m_post_market_1600_dropped(filter_func):
    """60m bar 起点 NY 16:00 (盘后) 应 drop。"""
    bars = [_bar(16, 0)]
    out = filter_func(bars, "60m")
    assert out == []


def test_1m_open_bar_at_0929_dropped(filter_func):
    """1m bar 09:29 在 09:30 之前, 不应保留 (开盘前 bar)。"""
    bars = [_bar(9, 29)]
    out = filter_func(bars, "1m")
    assert out == []


def test_1m_open_bar_at_0930_preserved(filter_func):
    """1m bar 09:30 是第一根开盘 K, 必须保留。"""
    bars = [_bar(9, 30)]
    out = filter_func(bars, "1m")
    assert len(out) == 1


def test_30m_open_bar_at_0930_preserved(filter_func):
    bars = [_bar(9, 30)]
    out = filter_func(bars, "30m")
    assert len(out) == 1


def test_120m_open_bar_at_0900_preserved(filter_func):
    """120m bar 09:00 起点 (区间 [09:00, 11:00)) 覆盖开盘, 必须保留。"""
    bars = [_bar(9, 0)]
    out = filter_func(bars, "120m")
    assert len(out) == 1


def test_weekend_bar_dropped_regardless_of_time(filter_func):
    """周末 bar 无论几点都 drop (alpaca 防御性过滤)。"""
    saturday_bar = _bar(10, 0, day=6)  # 2024-01-06 周六
    out = filter_func([saturday_bar], "60m")
    assert out == []


def test_daily_frequency_passes_through(filter_func):
    """frequency='d' 不在分钟级集合, 整列原样返回 (不过滤)。"""
    bars = [_bar(0, 0)]  # 日 K 是 NY 00:00 timestamp
    out = filter_func(bars, "d")
    assert out == bars
