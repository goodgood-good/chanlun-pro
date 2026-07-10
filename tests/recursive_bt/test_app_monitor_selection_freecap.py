"""app_monitor _selection_events 满仓 off-by-one: free=0(持仓已满)时旧码先 append 后判
len(events)>=free → 1>=0 仍发 1 条满仓买入事件, 与共享路径 live_monitor.collect_monitor_events
的 buys[:free](free=0→0 条)语义分裂;发出不可执行的买入通知且 identity 被 deduper 永久
mark, 之后腾出仓位同一买点不再提醒=信号被烧掉(新R3-F2-APPMON-2)。修复=free 判断前移到
append 之前。"""

from types import SimpleNamespace

from chanlun.recursive_bt.monitor.app_monitor import DynamicRecursiveMonitor


def _mk_monitor(max_pos, candidates):
    m = object.__new__(DynamicRecursiveMonitor)
    m.market = "a"
    m.names = {}
    m.config = SimpleNamespace(
        op_level="5m",
        max_pos=max_pos,
        nest_mode="off",
        trend_3boost=False,
        bs_point_ratio_multipliers=None,
    )
    m.last_selection_candidates = candidates
    return m


def _cand(code, bs_type="1buy"):
    return SimpleNamespace(
        code=code, name=code, bs_type=bs_type, big_dir="up",
        signal_time="2026-07-10 10:00:00", price=10.0, buy_class=1,
    )


def test_full_portfolio_free_zero_emits_no_buy():
    # 持仓已满(2/2): free=0 → 不得发任何买入事件(对齐 live_monitor buys[:0]=[])
    m = _mk_monitor(2, [_cand("SH.600000"), _cand("SH.600001")])
    events = m._selection_events(holdings={"SH.600100", "SH.600101"})
    assert events == []


def test_free_one_caps_at_one():
    # free=1(持仓1/2): 3个候选只发1条
    m = _mk_monitor(2, [_cand("SH.600000"), _cand("SH.600001"), _cand("SH.600002")])
    events = m._selection_events(holdings={"SH.600100"})
    assert len(events) == 1


def test_held_candidate_skipped_not_counted():
    # 候选之一已持仓 → 跳过不占 free; free=2-2=0 → 0 事件
    m = _mk_monitor(2, [_cand("SH.600000"), _cand("SH.600001")])
    events = m._selection_events(holdings={"SH.600000", "SH.600101"})
    assert events == []


def test_free_two_allows_two():
    # 无持仓 max_pos=2: 3候选发2条
    m = _mk_monitor(2, [_cand("SH.600000"), _cand("SH.600001"), _cand("SH.600002")])
    events = m._selection_events(holdings=set())
    assert len(events) == 2