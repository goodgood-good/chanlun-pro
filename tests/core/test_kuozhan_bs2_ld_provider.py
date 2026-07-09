# -*- coding: utf-8 -*-
"""C13: get_kuozhan_levels 调 Bs2BranchCalculator().calculate(levels) 漏传 ld_provider/
frequency → 升级级图层缺最弱档二类(破前低+盘整背驰), 与信号链 get_branch_bspoints(传
ld/frequency)口径分裂, 用户对照图表核对回测成交找不到对应信号点。修复后两者同口径。

用真实 fixture(有递归 levels)驱动 get_kuozhan_levels, spy Bs2.calculate 断言其收到
ld_provider 与 frequency(非 None)——修复前只传 levels(两者 None, 红)。
"""
import pathlib

import pandas as pd
import pytest

from chanlun.core.cl import CL
from chanlun.core import bs2_branch as _bs2mod

_FIX = pathlib.Path(__file__).resolve().parents[1] / "fixtures"
_CFG = {"macd_ld_use_htf": True, "recursive_zs_diversity": False}


@pytest.mark.skipif(
    not (_FIX / "SH.600519_5m.parquet").exists(),
    reason="tests/fixtures 缺失(parquet 未恢复)",
)
def test_kuozhan_passes_ld_provider_and_frequency(monkeypatch):
    df = pd.read_parquet(_FIX / "SH.600519_5m.parquet").tail(6000).reset_index(drop=True)
    cd = CL("SH.600519", "5m", dict(_CFG))
    cd.process_klines(df)

    calls = []
    orig = _bs2mod.Bs2BranchCalculator.calculate

    def _spy(self, levels, ld_provider=None, frequency=None):
        calls.append((ld_provider, frequency))
        return orig(self, levels, ld_provider, frequency)

    monkeypatch.setattr(_bs2mod.Bs2BranchCalculator, "calculate", _spy)

    cd.get_kuozhan_levels()
    assert calls, "get_kuozhan_levels 应调用 Bs2BranchCalculator.calculate"
    # 修复后: kuozhan 分支传 ld/frequency; 修复前只传 levels → 两者 None
    assert any(ld is not None and freq is not None for ld, freq in calls), (
        f"kuozhan 应与信号链同口径传 ld_provider/frequency, 实际 calls={calls}"
    )