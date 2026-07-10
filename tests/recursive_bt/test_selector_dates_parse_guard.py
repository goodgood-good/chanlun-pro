# -*- coding: utf-8 -*-
"""R12-#2: _fundamental_snapshot 对 dates[-1]→pd.Timestamp 转换零防护,一份 dates[-1] 不可解析的
坏缓存文件即让 select() 整体抛 DateParseError,而 live_monitor.py 启动路径(2342行,--market a
默认)无 try/except 兜底→常驻监控进程崩溃退出,重启在同一坏文件上再崩,人工介入前起不来。

同函数紧邻的 float(close[-1])(263-266)与 _load_fundamental_snapshot 内 pd.Timestamp(ann,tz=)
(357-360)均有 try/except,唯独 dates[-1] 这处遗漏。修复=wrap line262 的 pd.Timestamp,解析失败
返回空 FundamentalSnapshot()(与 260 行 dates/close 为空的早退同语义='基本面不可用'非'不合格')。

pandas 2.1 对 NaT/NaN/None/'' 静默转 NaT 不抛,只有真正不可解析的字符串才触发,本测用 'not-a-date'。
"""
from types import SimpleNamespace

from chanlun.recursive_bt.select.chanlun_selector import (
    OriginalChanlunASelector,
    FundamentalSnapshot,
)


def _fake_self():
    return SimpleNamespace(
        config=SimpleNamespace(require_three_systems=True, fund_data="D:/__nonexistent_fund__")
    )


def test_unparseable_dates_returns_empty_snapshot_not_crash():
    """dates[-1] 不可解析 → 返回空快照而非 DateParseError 崩 select()。"""
    snap = OriginalChanlunASelector._fundamental_snapshot(
        _fake_self(), "SH.600000", {"dates": ["not-a-date"], "close": [10.0]}
    )
    assert isinstance(snap, FundamentalSnapshot)
    assert not snap.fund_ok  # 空快照=基本面不可用


def test_empty_dates_returns_empty_snapshot_regression():
    """dates/close 为空仍走既有早退返回空快照(回归保护)。"""
    snap = OriginalChanlunASelector._fundamental_snapshot(
        _fake_self(), "SH.600000", {"dates": [], "close": []}
    )
    assert isinstance(snap, FundamentalSnapshot)
    assert not snap.fund_ok


def test_require_three_systems_false_early_return():
    """require_three_systems=False 早退 fund_ok=True(回归保护,不受本修复影响)。"""
    fs = SimpleNamespace(config=SimpleNamespace(require_three_systems=False, fund_data="x"))
    snap = OriginalChanlunASelector._fundamental_snapshot(fs, "SH.600000", {"dates": ["not-a-date"], "close": [10.0]})
    assert snap.fund_ok is True