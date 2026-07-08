"""Round12 LOW-2: portfolio._report 的乐观上界警告行含 ⚠(U+26A0 WARNING SIGN, 不在 GBK 码表)。
zh-CN Windows 控制台默认 GBK, 无 PYTHONIOENCODING=utf-8 时 _report 打印该行抛 UnicodeEncodeError
→ 回测算完(耗时)才崩、且在 write_outputs 前中止。改 ASCII 标记(其余 CJK/ASCII 均 GBK-safe)。"""

import io
import sys

import numpy as np
import pandas as pd

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


def test_report_prints_without_gbk_crash():
    syms = {"X.TEST": _sym()}
    old = sys.stdout
    sys.stdout = io.TextIOWrapper(io.BytesIO(), encoding="gbk", errors="strict")
    try:
        P.portfolio_backtest(
            syms={k: dict(v) for k, v in syms.items()}, filt=None, max_pos=2, label="r"
        )
        sys.stdout.flush()
    finally:
        sys.stdout = old