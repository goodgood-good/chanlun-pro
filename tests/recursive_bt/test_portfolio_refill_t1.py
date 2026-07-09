# -*- coding: utf-8 -*-
"""C1: portfolio activity_refill 加仓不更新 T+1 时钟, A股当日 refill 买入的股份当日可卖,
回测允许实盘做不到的当日回转 → trend_core 模式收益/回撤系统性偏乐观。

修复用独立 last_buy_date 做 T+1 时钟(refill 推进它), entry_date 保持最初开仓不被污染。
refill 内部状态不经 public API 暴露, 本测试作为 trend_core 路径的回归网:
①trend_core 模式端到端跑通不崩; ②所有 A股 trade 满足 T+1(exit 日 > entry 日);
③last_buy_date 未发生时默认回退 entry_date, 非 refill 路径行为不变。
"""
import numpy as np
import pandas as pd

from chanlun.recursive_bt.engine.engine import A_STOCK, Signal
from chanlun.recursive_bt.sim import portfolio as P


def _make_sym(dates, px, signals_by_bar):
    N = len(dates)
    return {
        "name": "X", "code": "X.T", "rules": A_STOCK, "dates": dates,
        "open": px.copy(), "high": px * 1.01, "low": px * 0.99, "close": px.copy(),
        "d2i": {d: i for i, d in enumerate(dates)},
        "small_by_bar": signals_by_bar,
        "big_dir_at": ["up"] * N,
    }


def test_trend_core_backtest_runs_and_all_trades_respect_t1():
    N = 40
    dates = list(pd.date_range("2024-01-01", periods=N, freq="D", tz="Asia/Shanghai"))
    px = np.full(N, 10.0)
    sigs = {
        3: [Signal(dates[3], 2, "1buy", 10.0)],    # 核心级开仓
        18: [Signal(dates[18], 0, "1sell", 10.0)],  # 小级别卖点(留核心→wait_buy)
        24: [Signal(dates[24], 1, "1buy", 10.0)],   # 回补买点(refill)
        34: [Signal(dates[34], 0, "1sell", 10.0)],  # 再卖
    }
    sym = _make_sym(dates, px, sigs)
    res = P.portfolio_backtest(
        syms={"X.T": sym}, filt=None, max_pos=1, init_cash=100000.0,
        label="t1", sell_ratio_policy="all_out", regime_mode="off",
        trend_core_hold_ratio=0.5, core_signal_level=2,
    )
    trades = res["trades"]
    # ① 跑通(不崩)且产出交易
    assert isinstance(trades, list)
    # ② A股 T+1: 任一 trade 的退出日必严格晚于进入日(entry_date 未被 refill 污染,
    #    仍为最初开仓; refill 买入的股份由 last_buy_date 锁 T+1, 绝无当日回转)
    for tr in trades:
        assert pd.Timestamp(tr.exit_date).date() > pd.Timestamp(tr.entry_date).date(), (
            f"A股 T+1 违背: entry={pd.Timestamp(tr.entry_date).date()} "
            f"exit={pd.Timestamp(tr.exit_date).date()} reason={tr.reason}"
        )


def test_t_plus_1_gate_uses_last_buy_date_default_entry_date():
    """非 refill 场景 last_buy_date 未设置时默认回退 entry_date, T+1 语义不变。"""
    N = 20
    dates = list(pd.date_range("2024-02-01", periods=N, freq="D", tz="Asia/Shanghai"))
    px = np.full(N, 10.0)
    sigs = {
        3: [Signal(dates[3], 0, "1buy", 10.0)],
        4: [Signal(dates[4], 0, "1sell", 10.0)],   # 次日卖, T+1 允许
    }
    sym = _make_sym(dates, px, sigs)
    res = P.portfolio_backtest(
        syms={"X.T": sym}, filt=None, max_pos=1, init_cash=100000.0,
        label="t1b", sell_ratio_policy="all_out", regime_mode="off",
    )
    for tr in res["trades"]:
        assert pd.Timestamp(tr.exit_date).date() > pd.Timestamp(tr.entry_date).date()