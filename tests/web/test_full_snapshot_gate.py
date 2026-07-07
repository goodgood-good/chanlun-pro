"""D4-F1: /tv/history 轮询响应 full_snapshot gate。

幽灵形态根因: 轮询响应按窄窗口 slice 形态且不带 full_snapshot, 前端窗口外只增不删。
修复=最近窗口权威 + 源全量快照 时带全量形态 + full_snapshot=True(前端整体替换清幽灵)。
gate 必须严格: 向左滚动(to<末根)/窄窗口 range-miss(源非全量)一律不置 —— 否则前端整体
替换会丢弃窗口外合法形态(比幽灵更糟, 空图)。本测试钉死 gate 的灾难场景返 False。
"""
from cl_app.services.chart_compute import _decide_full_snapshot


def test_recent_window_full_source_emits():
    # 最近窗口(to=1000>=末根900)+ 源全量 -> 置
    assert _decide_full_snapshot("false", 1000, [100, 500, 900], True) is True


def test_unbounded_to_emits():
    # to==0 表示无上界(最近窗口)
    assert _decide_full_snapshot("false", 0, [100, 900], True) is True


def test_to_equals_last_bar_emits():
    assert _decide_full_snapshot("false", 900, [100, 500, 900], True) is True


def test_backward_scroll_does_not_emit():
    # 向左滚动: to=500 < 末根900 -> 不置(否则整体替换丢弃窗外合法形态)
    assert _decide_full_snapshot("false", 500, [100, 500, 900], True) is False


def test_narrow_source_does_not_emit():
    # range-miss 窄窗口结果(源非全量快照) -> 不置
    assert _decide_full_snapshot("false", 1000, [100, 900], False) is False


def test_first_load_does_not_emit():
    # 首帧走 update=false 全量替换, 不需 full_snapshot
    assert _decide_full_snapshot("true", 1000, [100, 900], True) is False


def test_empty_bars_does_not_emit():
    assert _decide_full_snapshot("false", 1000, [], True) is False


def test_none_bars_does_not_emit():
    assert _decide_full_snapshot("false", 1000, None, True) is False