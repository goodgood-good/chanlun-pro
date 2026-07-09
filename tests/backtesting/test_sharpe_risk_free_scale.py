"""R4-C7: trade 模式 Sharpe 的日无风险利率须与 daily_return/return_std 同量纲(×100)。

daily_return / return_std 在 backtest.result() 中已 ×100(百分点), 而 daily_risk_free
原为 risk_free/sqrt(days) 的裸小数(~0.0019)未 ×100→risk_free 项被约 100x 稀释近乎失效、
Sharpe 高估至接近毛值。抽成 _sharpe_ratio 纯函数便于钉死量纲。
"""
import numpy as np

from chanlun.backtesting.backtest import _sharpe_ratio


def test_sharpe_risk_free_scaled_to_percent():
    dr, sd, rf, days = 5.0, 1.0, 0.03, 100  # daily_return/std 已是百分点
    s = _sharpe_ratio(dr, sd, rf, days)
    gross = _sharpe_ratio(dr, sd, 0.0, days)  # 无风险=0 的毛 Sharpe
    # 正确: 日无风险 = 0.03/sqrt(100)*100 = 0.3 百分点; 年化后毛值扣 0.3*sqrt(100)=3.0
    # 旧代码 daily_risk_free=0.03/10=0.003 → 只扣 0.03, 此断言(差 3.0)失败
    assert abs((gross - s) - 3.0) < 1e-9
    assert abs(s - 47.0) < 1e-9


def test_sharpe_matches_manual_formula():
    dr, sd, rf, days = 0.08, 1.2, 0.03, 240
    expected_drf = 0.03 / np.sqrt(240) * 100
    expected = (0.08 - expected_drf) / 1.2 * np.sqrt(240)
    assert abs(_sharpe_ratio(dr, sd, rf, days) - expected) < 1e-9


def test_sharpe_zero_std_returns_zero():
    assert _sharpe_ratio(1.0, 0.0, 0.03, 240) == 0