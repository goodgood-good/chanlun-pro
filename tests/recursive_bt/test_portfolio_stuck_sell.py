"""Round12 HIGH-1: original_layered 部分卖 shares*ratio<lot 取整为0股 → 原 carry 该卖单永久滞留
→ 经 _process_exits 的 pend_sell 屏蔽该标一切后续退出(大级别down强平/结构止损全失效)→ 持仓裸奔到
final_close = 错回测(P&L/风险画像失真)。修复后 sub-lot 卖单 drop, 后续 big-down 退出生效。"""

import numpy as np
import pandas as pd

from chanlun.recursive_bt.engine.engine import A_STOCK, Signal
from chanlun.recursive_bt.sim import portfolio as P


def _make_sym(dates, px, big_dir):
    buy = Signal(dates[5], 0, "3buy", 10.0)
    sell = Signal(dates[10], 0, "3sell", 10.0)
    return {
        "name": "X", "code": "X.T", "rules": A_STOCK, "dates": dates,
        "open": px.copy(), "high": px * 1.01, "low": px * 0.99, "close": px.copy(),
        "d2i": {d: i for i, d in enumerate(dates)},
        "small_by_bar": {5: [buy], 10: [sell]},
        "big_dir_at": big_dir,
    }


def test_original_layered_sublot_sell_does_not_block_big_down_exit():
    N = 60
    dates = list(pd.date_range("2024-01-01", periods=N, freq="D", tz="Asia/Shanghai"))
    px = np.full(N, 10.0)
    big_dir = ["up"] * 30 + ["down"] * 30  # bar30 起 big-down 应强平
    sym = _make_sym(dates, px, list(big_dir))
    res = P.portfolio_backtest(
        syms={"X.T": sym}, filt=None, max_pos=1, init_cash=2100.0,
        label="r", sell_ratio_policy="original_layered", regime_mode="off",
    )
    trades = res["trades"]
    assert len(trades) >= 1
    # 修复前: 3sell(200*0.25=50→0股)卡死 → bar30 big-down 被 pend_sell 屏蔽 → 拖到末尾 final_close。
    # 修复后: sub-lot 卖单 drop → bar30 big-down 强平生效, 不再裸奔到末尾。
    assert trades[-1].reason != "final_close", (
        f"退出不应被卡死sub-lot卖单屏蔽到final_close, reason={trades[-1].reason} "
        f"exit={pd.Timestamp(trades[-1].exit_date).date()}"
    )
    assert pd.Timestamp(trades[-1].exit_date) < dates[-1]