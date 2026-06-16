# -*- coding: utf-8 -*-
"""recursive_bt 端到端特征网:build_symbol_from_klines + portfolio_backtest 行为指纹。

钉住「固定 fixture 输入 → 固定回测输出」,覆盖两大块:
  ① 信号生成:build_symbol_from_klines(递归层中枢/走势/买卖点 → signal_events)
  ② 回测撮合:portfolio_backtest(开平仓/收益/回撤/夏普)

这是 architecture_review.md 标的 **P0**:为未来拆分 portfolio_backtest(约894行巨型函数)
与 live_backtest 上帝模块提供贴身安全网——重构前后此网必须全绿,信号生成与回测撮合
行为不得漂移。chan_core 黄金主只覆盖 core.CL 信号层,测不到 recursive_bt 这条端到端链路。

注意:
- 平台相关(Windows + numpy/scipy 版本,同 chan_core golden);浮点用容差断言。
- 5m / 30m fixture 时间窗口不重叠,big_dir 多为 neutral——本网只要求输入固定→输出固定
  (确定性指纹),不追求经济意义,作为重构回归网完全有效。
- 用 batch 信号模式(确定、快);walk_forward 另有 test_incremental_equivalence 守。
"""
from pathlib import Path

import pandas as pd
import pytest

from chanlun.recursive_bt.live_backtest import build_symbol_from_klines
from chanlun.recursive_bt.portfolio import portfolio_backtest

FIX_DIR = Path(__file__).resolve().parents[1] / "fixtures"
_OP_BARS = 2000  # 5m 截取窗口(指纹随此数变,勿改)


def _build_sym() -> dict:
    df5 = pd.read_parquet(FIX_DIR / "SH.600519_5m.parquet").iloc[:_OP_BARS].reset_index(drop=True)
    df30 = pd.read_parquet(FIX_DIR / "SH.600519_30m.parquet").reset_index(drop=True)
    return build_symbol_from_klines(
        "a", "SH.600519", df5, df30,
        op_level="5m", big_level="30m", signal_mode="batch",
    )


def _run_bt(sym: dict) -> dict:
    return portfolio_backtest(universe=["SH.600519"], syms={"SH.600519": dict(sym)}, max_pos=1)


@pytest.fixture(scope="module")
def sym() -> dict:
    return _build_sym()


def test_build_symbol_fingerprint(sym):
    """信号生成层:固定输入 → 固定 signal_events 数与序列长度。"""
    assert sym["signal_mode"] == "batch"
    assert len(sym["dates"]) == _OP_BARS
    assert len(sym["big_dir_at"]) == _OP_BARS
    assert len(sym["signal_events"]) == 16


def test_portfolio_backtest_fingerprint(sym):
    """回测撮合层:固定 syms → 固定收益/回撤/夏普/交易指纹。"""
    res = _run_bt(sym)
    # 结构性指纹(整数,最稳)
    assert res["n"] == 6
    assert len(res["trades"]) == 6
    assert len(res["equity"]) == _OP_BARS
    assert len(res["bench"]) == _OP_BARS
    # 数值指纹(平台相关,容差断言)
    assert res["total"] == pytest.approx(0.009675037500000983, rel=1e-6)
    assert res["bh"] == pytest.approx(-0.021498510004257287, rel=1e-6)
    assert res["max_dd"] == pytest.approx(0.03306321582156107, rel=1e-6)
    assert res["bench_dd"] == pytest.approx(0.07788361373431478, rel=1e-6)
    assert res["sharpe"] == pytest.approx(0.7122408582848564, rel=1e-6)
    assert res["wr"] == pytest.approx(1 / 3, rel=1e-6)


def test_reproducible():
    """两次独立构建+回测,核心指标逐位一致(确定性:无随机/无序依赖/无隐藏状态)。"""
    a = _run_bt(_build_sym())
    b = _run_bt(_build_sym())
    assert a["total"] == b["total"]
    assert a["n"] == b["n"]
    assert a["sharpe"] == b["sharpe"]
    assert a["max_dd"] == b["max_dd"]
