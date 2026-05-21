"""tests/signal_monitor/test_validation.py — 验证闭环本身的冒烟测试。

只保证 validation 模块在真实 fixture 上能跑通、报告结构正确（截短数据加速）；
信号质量结论由 `python -m chanlun.signal_monitor.validation` 的完整报告给出。
"""
from __future__ import annotations

import pytest

from chanlun.signal_monitor.validation import (
    fixture_path,
    format_report,
    forward_return,
    load_klines,
    run_validation,
)

LADDER = ["d", "30m", "5m"]


def _load_small():
    """加载 a_SZ_301004 三级 fixture 并各截短到 600 根加速；缺文件则 skip。"""
    klines = {}
    for lvl in LADDER:
        p = fixture_path("a_SZ_301004", lvl)
        if not p.is_file():
            pytest.skip(f"缺少 fixture: {p}")
        klines[lvl] = load_klines(p).head(600)
    return klines


def test_run_validation_smoke():
    klines = _load_small()
    report = run_validation(
        klines["30m"], klines, operation_level="30m", level_ladder=LADDER,
        code="a_SZ_301004", forward_bars=8, max_checkpoints=3, warmup=300,
    )
    # 报告结构
    assert report["operation_level"] == "30m"
    assert report["level_ladder"] == LADDER
    assert isinstance(report["signals"], list)
    assert report["signals_count"] == len(report["signals"])
    assert set(report["by_grade"]) == {"A", "B", "C"}
    assert "overall" in report and "by_direction" in report
    # 每条信号字段完整
    for s in report["signals"]:
        assert s["grade"] in ("A", "B", "C")
        assert s["direction"] in ("bullish", "bearish")
        assert isinstance(s["hit"], bool)
    # 报告可格式化
    txt = format_report(report)
    assert "信号有效性验证报告" in txt
    assert "按分级" in txt


def test_forward_return_basics():
    import pandas as pd
    df = pd.DataFrame({
        "date": pd.date_range("2024-01-01", periods=20, freq="30min", tz="UTC"),
        "close": [10.0 + i for i in range(20)],  # 单调上涨
    })
    # 偏多信号 + 后续上涨 → signed_ret > 0，hit
    fr = forward_return(df, fire_idx=2, direction="bullish", forward_bars=5)
    assert fr["hit"] is True
    assert fr["signed_ret"] > 0
    # 偏空信号 + 后续上涨 → signed_ret < 0，不 hit
    fr2 = forward_return(df, fire_idx=2, direction="bearish", forward_bars=5)
    assert fr2["hit"] is False
    # 末端无前瞻空间 → None
    assert forward_return(df, fire_idx=19, direction="bullish", forward_bars=5) is None
