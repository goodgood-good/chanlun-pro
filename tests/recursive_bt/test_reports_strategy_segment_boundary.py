# -*- coding: utf-8 -*-
"""R1-F2-1: 策略归因分段边界跨格式字符串比较。

审计事件 time 是 isoformat 默认 'T' 分隔(live_monitor.py:234),权益曲线快照是
isoformat(sep=" ")(live_monitor.py:1612 等);_build_market_strategy_segments 的
while 条件对两种格式裸字符串比较,'T'(0x54)>' '(0x20) 使同日事件恒大于同日快照,
每个 runtime override 生效边界确定性滑到次日首个快照,当日切换后的权益点全部
错归旧策略段。修复=事件摄入与比较点归一 'T'→空格(位置10精确替换)。
"""
from chanlun.recursive_bt.strategy_optimizer.reports_strategy import (
    _build_market_strategy_segments,
)


def _curve():
    return [
        {"time": "2026-07-09 10:00:00", "equity": 100000.0, "trades": 0},
        {"time": "2026-07-09 21:30:00", "equity": 110000.0, "trades": 2},
        {"time": "2026-07-10 10:00:00", "equity": 110000.0, "trades": 2},
    ]


def _event_t_sep():
    return [{
        "time": "2026-07-09T20:00:00",          # 'T' 分隔(审计事件生产格式)
        "action": "switch",
        "risk_state": "normal",
        "target_candidate": "cand_x",
        "decision_key": "a|switch|cand_x",
        "reason": "test",
        "applied_config": {},
    }]


def test_same_day_t_event_boundary_not_slipped_to_next_day():
    segs = _build_market_strategy_segments("a", _curve(), _event_t_sep())
    assert len(segs) == 2, [s["target_candidate"] for s in segs]
    # 事件 20:00 早于 21:30 快照 → baseline 段只含 10:00 一个快照
    assert segs[0]["snapshots"] == 1, f"旧码: 21:30 快照被错归 baseline(snapshots={segs[0]['snapshots']})"
    # 切换段从当日 21:30 开始,而非滑到次日 10:00
    assert segs[1]["target_candidate"] == "cand_x"
    assert segs[1]["start_time"] == "2026-07-09 21:30:00", segs[1]["start_time"]
    assert segs[1]["snapshots"] == 2