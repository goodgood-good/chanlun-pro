# -*- coding: utf-8 -*-
"""R14-C3 (HIGH): live_monitor.main() 常驻 while True 循环裸调 run_once(),任意深层
未捕获异常一路冒泡终结整个监控进程(无重试无自愈),该市场(a/us)全部买卖点提醒 + paper
撮合快照全停摆直到人工发现重启。修复=抽出 _safe_scan_cycle 助手, try/except 吞本轮异常
仅记日志并续跑(与文件内 collect_monitor_events 等'单元失败不传染整体'防护对齐)。

对照: app_monitor 的 DynamicRecursiveMonitor.run_once 走 APScheduler(异常隔离), 但默认
enabled=False; 当前生产用的正是无此保护的独立 CLI live_monitor.py。
"""
import datetime as _dt
from types import SimpleNamespace

import chanlun.recursive_bt.monitor.live_monitor as lm


def test_safe_scan_cycle_swallows_run_once_exception(monkeypatch):
    """run_once 抛未捕获异常时, 单轮扫描必须吞掉续跑(不外传终结进程)。"""
    called = {"run_once": 0}

    def boom(*a, **k):
        called["run_once"] += 1
        raise RuntimeError("deep crash in run_once")

    monkeypatch.setattr(lm, "run_once", boom)
    monkeypatch.setattr(lm, "market_is_open", lambda *a, **k: True)
    args = SimpleNamespace(force=True, market="a", op_level="l1")
    now = _dt.datetime(2026, 1, 2, 10, 0)

    # 不抛异常即证明被吞; run_once 确被调用; selector None 跳过 rescan → last_rescan_date 原样返回
    result = lm._safe_scan_cycle(
        now, None, {}, {}, object(), args, None, None, None, set(), _dt.date(2026, 1, 2)
    )
    assert called["run_once"] == 1
    assert result == _dt.date(2026, 1, 2)


def test_safe_scan_cycle_skips_run_once_when_market_closed(monkeypatch):
    """休市且非 force → 不调 run_once(保持原门控语义)。"""
    called = {"run_once": 0}
    monkeypatch.setattr(
        lm, "run_once", lambda *a, **k: called.__setitem__("run_once", called["run_once"] + 1)
    )
    monkeypatch.setattr(lm, "market_is_open", lambda *a, **k: False)
    args = SimpleNamespace(force=False, market="a", op_level="l1")
    now = _dt.datetime(2026, 1, 2, 10, 0)

    lm._safe_scan_cycle(
        now, None, {}, {}, object(), args, None, None, None, set(), _dt.date(2026, 1, 2)
    )
    assert called["run_once"] == 0


def test_safe_scan_cycle_swallows_rescan_exception(monkeypatch):
    """selector 非 None + 新交易日触发 rescan; rescan 崩也要被吞(不终结进程)。"""
    monkeypatch.setattr(
        lm, "rescan_selection_pool", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("rescan crash"))
    )
    monkeypatch.setattr(lm, "market_is_open", lambda *a, **k: False)
    monkeypatch.setattr(lm, "run_once", lambda *a, **k: None)
    args = SimpleNamespace(force=False, market="a", op_level="l1")
    now = _dt.datetime(2026, 1, 3, 10, 0)  # time >= 9:00, 新交易日

    # rescan 崩在 last_rescan_date 更新前 → 被吞 → 返回原 last_rescan_date(未更新)
    result = lm._safe_scan_cycle(
        now, object(), {}, {}, object(), args, None, None, None, set(), _dt.date(2026, 1, 2)
    )
    assert result == _dt.date(2026, 1, 2)