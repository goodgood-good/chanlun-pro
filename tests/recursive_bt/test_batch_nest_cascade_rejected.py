"""R4-C2: batch 模式 + nest_cascade 应显式拒绝, 而非静默产出分支信号冒名 nest_cascade。

build_symbol_from_klines 的 batch 路径(else)只特判 signal_source=='upgrade';
nest_cascade 会落到 collect_branch_signals 却仍把 out['signal_source'] 标为 nest_cascade,
使 batch nest_cascade-vs-branch 对拍被污染。介入事件仅在 walk_forward 路径实现, 故在参数
校验处显式拒绝该组合。
"""
import pytest

from chanlun.recursive_bt.backtest.live_backtest import build_symbol_from_klines


def test_batch_nest_cascade_raises():
    with pytest.raises(ValueError, match="walk_forward"):
        build_symbol_from_klines(
            "us", "AAPL", None,
            signal_mode="batch", signal_source="nest_cascade",
        )


def test_walk_forward_nest_cascade_passes_validation():
    # walk_forward + nest_cascade 合法: 不得被该守卫误伤(校验通过后因 df_op=None 在更下游
    # 报 AttributeError, 只要不是本守卫的 ValueError 即证明未被拒)。
    with pytest.raises(Exception) as ei:
        build_symbol_from_klines(
            "us", "AAPL", None,
            signal_mode="walk_forward", signal_source="nest_cascade",
        )
    assert "requires signal_mode=walk_forward" not in str(ei.value)