"""Round8 BUG-4: engine.Simulator._metrics 夏普年化因子须按 self.dates 实际 bar 频率
自适应(sqrt(bar数/年数)), 不再硬编码 sqrt(244*240)(1m 口径)。backtest_symbol 实际喂
5m(df5)时旧代码把夏普高估约 2.24x。"""

import numpy as np
import pandas as pd

from chanlun.recursive_bt.engine.engine import Simulator


def test_metrics_sharpe_annualization_adaptive_not_hardcoded():
    sim = Simulator.__new__(Simulator)
    n = 200
    sim.dates = pd.date_range("2024-01-01", periods=n, freq="5min").to_list()
    sim.closes = np.full(n, 100.0)
    steps = np.where(np.arange(n - 1) % 2 == 0, 0.001, -0.0005)
    equity = 100000.0 * np.cumprod(np.concatenate([[1.0], 1 + steps]))

    result = sim._metrics(equity, [])

    rets = np.diff(equity) / equity[:-1]
    span_days = (sim.dates[-1] - sim.dates[0]).total_seconds() / 86400.0
    ann_adaptive = np.sqrt((n - 1) / (span_days / 365.25))
    expected = float(np.mean(rets) / (np.std(rets) + 1e-12) * ann_adaptive)
    old_hardcoded = float(np.mean(rets) / (np.std(rets) + 1e-12) * np.sqrt(244 * 240))

    assert abs(result.sharpe - expected) < 1e-6, "夏普未用 self.dates 自适应年化"
    assert abs(result.sharpe - old_hardcoded) > 1e-3, "夏普仍是硬编码 sqrt(244*240)"