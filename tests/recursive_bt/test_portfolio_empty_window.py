"""Round12 MEDIUM-1: 空交易窗口(t_start 晚于全部数据 / t_end<t_start)→ master 过滤后为空 →
收尾强平 t=master[-1] 及 _report 的 equity[-1]/[0] 越界 IndexError。改前置抛清晰 ValueError。"""

import numpy as np
import pandas as pd
import pytest

from chanlun.recursive_bt.engine.engine import A_STOCK
from chanlun.recursive_bt.sim import portfolio as P


def _sym():
    dates = list(pd.date_range("2024-01-01 09:35", periods=60, freq="5min", tz="Asia/Shanghai"))
    n = len(dates)
    return {
        "name": "X", "code": "X.TEST", "rules": A_STOCK, "dates": dates,
        "open": np.full(n, 10.0), "high": np.full(n, 10.1),
        "low": np.full(n, 9.9), "close": np.full(n, 10.0),
        "d2i": {d: i for i, d in enumerate(dates)},
        "small_by_bar": {}, "big_dir_at": ["neutral"] * n,
    }


def test_empty_window_future_start_raises_clear_error():
    syms = {"X.TEST": _sym()}
    with pytest.raises(ValueError, match="回测窗口"):
        P.portfolio_backtest(
            syms={k: dict(v) for k, v in syms.items()}, filt=None, max_pos=2, label="r",
            t_start=pd.Timestamp("2099-01-01", tz="Asia/Shanghai"),
        )


def test_empty_window_reversed_start_end_raises_clear_error():
    syms = {"X.TEST": _sym()}
    with pytest.raises(ValueError, match="回测窗口"):
        P.portfolio_backtest(
            syms={k: dict(v) for k, v in syms.items()}, filt=None, max_pos=2, label="r",
            t_start=pd.Timestamp("2024-01-01 12:00", tz="Asia/Shanghai"),
            t_end=pd.Timestamp("2024-01-01 10:00", tz="Asia/Shanghai"),
        )