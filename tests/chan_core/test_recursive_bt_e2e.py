# -*- coding: utf-8 -*-
"""recursive_bt 端到端特征网:build_symbol_from_klines + portfolio_backtest 行为指纹。

钉住「固定 fixture 输入 → 固定回测输出」,覆盖两大块:
  ① 信号生成:build_symbol_from_klines(递归层中枢/走势/买卖点 → signal_events)
  ② 回测撮合:portfolio_backtest(开平仓/收益/回撤/夏普)

这是 architecture_review.md 标的 **P0**:为拆分 portfolio_backtest(约894行巨型函数)
与 live_backtest 上帝模块提供贴身安全网——重构前后此网必须全绿,信号生成与回测撮合
行为不得漂移。chan_core 黄金主只覆盖 core.CL 信号层,测不到 recursive_bt 这条端到端链路。

覆盖矩阵(多参数+多标的,压缩重构盲区):
  - 茅台 5m/30m:max_pos=1(基准)、max_pos=2(多仓撮合)、buy_classes={1}(买点过滤分支)
  - QQQ.US 30m/d:美股标的(不同数据/不同 n)

注意:
- 平台相关(Windows + numpy/scipy 版本,同 chan_core golden);浮点用容差断言。
- fixture 各级别时间窗口不必重叠——本网只要求输入固定→输出固定(确定性指纹),
  不追求经济意义,作为重构回归网完全有效。
- 用 batch 信号模式(确定、快);walk_forward 另有 test_incremental_equivalence 守。
"""
from pathlib import Path

import pandas as pd
import pytest

from chanlun.recursive_bt.live_backtest import build_symbol_from_klines
from chanlun.recursive_bt.portfolio import portfolio_backtest

FIX_DIR = Path(__file__).resolve().parents[1] / "fixtures"
_OP_BARS = 2000  # 茅台 5m 截取窗口(指纹随此数变,勿改)


def _build_maotai() -> dict:
    df5 = pd.read_parquet(FIX_DIR / "SH.600519_5m.parquet").iloc[:_OP_BARS].reset_index(drop=True)
    df30 = pd.read_parquet(FIX_DIR / "SH.600519_30m.parquet").reset_index(drop=True)
    return build_symbol_from_klines(
        "a", "SH.600519", df5, df30,
        op_level="5m", big_level="30m", signal_mode="batch",
    )


def _build_qqq() -> dict:
    df30 = pd.read_parquet(FIX_DIR / "QQQ.US_30m.parquet").reset_index(drop=True)
    dfd = pd.read_parquet(FIX_DIR / "QQQ.US_d.parquet").reset_index(drop=True)
    return build_symbol_from_klines(
        "us", "QQQ.US", df30, dfd,
        op_level="30m", big_level="d", signal_mode="batch",
    )


@pytest.fixture(scope="module")
def maotai() -> dict:
    return _build_maotai()


@pytest.fixture(scope="module")
def qqq() -> dict:
    return _build_qqq()


def test_build_symbol_fingerprint(maotai):
    """信号生成层:固定输入 → 固定 signal_events 数与序列长度。"""
    assert maotai["signal_mode"] == "batch"
    assert len(maotai["dates"]) == _OP_BARS
    assert len(maotai["big_dir_at"]) == _OP_BARS
    assert len(maotai["signal_events"]) == 16


def test_portfolio_backtest_fingerprint(maotai):
    """回测撮合层(基准组 max_pos=1):固定 syms → 固定收益/回撤/夏普/交易指纹。"""
    res = portfolio_backtest(universe=["SH.600519"], syms={"SH.600519": dict(maotai)}, max_pos=1)
    assert res["n"] == 6
    assert len(res["trades"]) == 6
    assert len(res["equity"]) == _OP_BARS
    assert len(res["bench"]) == _OP_BARS
    assert res["total"] == pytest.approx(0.009675037500000983, rel=1e-6)
    assert res["bh"] == pytest.approx(-0.021498510004257287, rel=1e-6)
    assert res["max_dd"] == pytest.approx(0.03306321582156107, rel=1e-6)
    assert res["bench_dd"] == pytest.approx(0.07788361373431478, rel=1e-6)
    assert res["sharpe"] == pytest.approx(0.7122408582848564, rel=1e-6)
    assert res["wr"] == pytest.approx(1 / 3, rel=1e-6)


def test_portfolio_param_variants(maotai):
    """参数分支覆盖:max_pos=2(多仓撮合)与 buy_classes={1}(只一类买点)。"""
    r2 = portfolio_backtest(universe=["SH.600519"], syms={"SH.600519": dict(maotai)}, max_pos=2)
    assert r2["n"] == 6
    assert r2["total"] == pytest.approx(0.0058257943, rel=1e-6)
    assert r2["max_dd"] == pytest.approx(0.014365503265, rel=1e-6)
    assert r2["sharpe"] == pytest.approx(0.894335228619, rel=1e-6)

    r1 = portfolio_backtest(universe=["SH.600519"], syms={"SH.600519": dict(maotai)},
                            max_pos=1, buy_classes={1})
    assert r1["n"] == 1
    assert r1["wr"] == 0.0
    assert r1["total"] == pytest.approx(-0.0042377505, rel=1e-6)
    assert r1["sharpe"] == pytest.approx(-1.871094225203, rel=1e-6)


def test_portfolio_layered_positions(maotai):
    """仓位分层路径(swing_signal_level>0 + core_signal_level>0):显式覆盖 P1 第二刀抽出的
    _entry_layer / _activity_parts / _sellable_layer_shares / _deplete_activity_layers 的完整
    core/swing/scalp 分层撮合逻辑——默认参数下这些走简化路径(swing<=0),等于没测到执行。"""
    res = portfolio_backtest(
        universe=["SH.600519"], syms={"SH.600519": dict(maotai)},
        max_pos=3, swing_signal_level=1, core_signal_level=2,
    )
    assert res["n"] == 6
    assert res["total"] == pytest.approx(0.0034130017, rel=1e-6)
    assert res["max_dd"] == pytest.approx(0.0096168992, rel=1e-6)
    assert res["sharpe"] == pytest.approx(0.7744295356, rel=1e-6)


def test_qqq_fingerprint(qqq):
    """美股标的(QQQ.US 30m/d):不同数据 → 固定信号与回测指纹。"""
    assert len(qqq["signal_events"]) == 10
    res = portfolio_backtest(universe=["QQQ.US"], syms={"QQQ.US": dict(qqq)}, max_pos=1)
    assert res["n"] == 1
    assert res["wr"] == 1.0
    assert res["total"] == pytest.approx(0.222139511245, rel=1e-6)
    assert res["bh"] == pytest.approx(0.234464896895, rel=1e-6)
    assert res["max_dd"] == pytest.approx(0.034323479038, rel=1e-6)
    assert res["sharpe"] == pytest.approx(7.022712970662, rel=1e-6)


def test_reproducible():
    """两次独立构建+回测,核心指标逐位一致(确定性:无随机/无序依赖/无隐藏状态)。"""
    def run():
        s = _build_maotai()
        return portfolio_backtest(universe=["SH.600519"], syms={"SH.600519": s}, max_pos=1)
    a, b = run(), run()
    assert a["total"] == b["total"]
    assert a["n"] == b["n"]
    assert a["sharpe"] == b["sharpe"]
    assert a["max_dd"] == b["max_dd"]
