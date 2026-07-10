# -*- coding: utf-8 -*-
"""R13-#4: fetch.build() 输出字典漏写 high/low(dfs 明明含这两列),下游 sim/portfolio._bar_low/
_bar_high 用 s.get("low")/s.get("high") 取不到时静默 fallback 成 s["close"] → _position_structural_
invalidation 的结构止损判定用 close 而非真实 low/high。A股默认回测走 source=bt_data(portfolio.load_cached
不回填),1buy/3buy 默认带 structural_stop → 插针击穿止损但收盘收回的场景在 A股默认回测完全漏判,
回撤/胜率系统性乐观。修复=out 补 "high"/"low"(dfs 已有,零成本)。

★注意: 本修改改变回测 structural stop 口径 → 需回测基线重生;且已存量 bt_data/*.pkl 需一次
fetch.py run() 全量重建(或轻量 patch 补 high/low 两列)才生效——留用户决定,不擅自 regen。
"""
import datetime

import numpy as np
import pandas as pd

import chanlun.recursive_bt.data.fetch as fetch


class _FakeStrat:
    def __init__(self, *a, **k):
        self.small_by_bar = []
        self.big_dir_at = []


def _mk_dfs(n=220):
    return pd.DataFrame({
        "date": [datetime.datetime(2020, 1, 1) + datetime.timedelta(minutes=5 * i) for i in range(n)],
        "open": [100.0 + i * 0.1 for i in range(n)],
        "high": [101.0 + i * 0.1 for i in range(n)],
        "low": [99.0 + i * 0.1 for i in range(n)],
        "close": [100.5 + i * 0.1 for i in range(n)],
        "volume": [1000.0] * n,
    })


def test_build_out_includes_high_low(monkeypatch):
    dfs = _mk_dfs(220)
    monkeypatch.setattr(fetch, "_sig", lambda *a, **k: (dfs, list(range(5))))
    monkeypatch.setattr(fetch, "MTFStrategy", _FakeStrat)
    monkeypatch.setattr(fetch, "_daily_bsp_and_d3", lambda *a, **k: ([], [False] * len(dfs)))
    monkeypatch.setattr(fetch, "limit_pct", lambda code: 0.1)

    out = fetch.build("TEST", None)

    assert out is not None
    assert "high" in out and "low" in out, "build() 输出必须含 high/low 供 structural stop 用真实高低点"
    assert np.array_equal(out["high"], dfs["high"].to_numpy())
    assert np.array_equal(out["low"], dfs["low"].to_numpy())
    # 回归: open/close 仍在
    assert np.array_equal(out["open"], dfs["open"].to_numpy())
    assert np.array_equal(out["close"], dfs["close"].to_numpy())