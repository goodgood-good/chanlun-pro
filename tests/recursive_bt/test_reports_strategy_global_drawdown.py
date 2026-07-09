"""R3-C1: 策略报告聚合最大回撤应为全曲线口径, 非各段 dd 取 max。

_summarize_segments 原以 max(每段自身 max_drawdown) 作为聚合回撤, 会漏掉跨段回撤
(峰在前段、谷在后段, 每段各自把 peak 重置为本段首值)→系统性低估。修复后传入全曲线
curve 一次性算全局最大回撤。仅影响报告展示(降级门控走 scoring.py 独立源)。
"""

from chanlun.recursive_bt.strategy_optimizer.reports_strategy import (
    _segment_from_points,
    _summarize_segments,
)


def _pt(t, eq, trades=0):
    return {"time": t, "equity": eq, "trades": trades}


_STAGE = {
    "action": "a", "risk_state": "r", "target_candidate": "c",
    "decision_key": "k", "reason": "x", "applied_config": {},
}


def test_summary_max_drawdown_is_global_not_per_segment_max():
    # seg1: 1.0→2.0→1.5(段内 dd 25%), seg2: 1.5→1.0(段内 dd 33.3%)
    seg1_pts = [_pt("t1", 1.0), _pt("t2", 2.0), _pt("t3", 1.5)]
    seg2_pts = [_pt("t4", 1.5), _pt("t5", 1.0)]
    curve = seg1_pts + seg2_pts
    seg1 = _segment_from_points("us", _STAGE, seg1_pts)
    seg2 = _segment_from_points("us", _STAGE, seg2_pts)
    segments = [seg1, seg2]

    per_seg_max = max(seg1["max_drawdown"], seg2["max_drawdown"])
    summary = _summarize_segments(segments, curve)

    # 全局回撤: 峰 2.0(seg1) → 谷 1.0(seg2) = 50%
    assert abs(summary["max_drawdown"] - 0.5) < 1e-9
    # 旧口径(各段 dd 取 max)约 33.3%, 系统性低估
    assert per_seg_max < 0.4
    assert summary["max_drawdown"] > per_seg_max + 0.1


def test_summary_empty_segments_zero_drawdown():
    summary = _summarize_segments([], [])
    assert summary["max_drawdown"] == 0.0
    assert summary["segment_count"] == 0