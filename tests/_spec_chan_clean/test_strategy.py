"""操作层 多头策略 测试。"""

from chan.duan import find_duans_from_bars
from chan.strategy import StrategyConfig, mtf_trend_up, run_long_strategy, summary
from chan.types import Bar, ChanStatus, Direction, MaiMaiDian, MMDType, confirmed_only


def _bars(closes):
    return [Bar(idx=i, dt=None, o=c, h=c, l=c, c=c) for i, c in enumerate(closes)]


def _mmd(t, cb, price=0.0):
    return MaiMaiDian(t, price, cb, ChanStatus.CONFIRMED)


# ── 基础：买点入场、卖点离场、收益含成本 ──
def test_entry_exit_basic():
    bars = _bars([10, 11, 12, 13, 12])
    sigs = [_mmd(MMDType.BUY1, 1), _mmd(MMDType.SELL1, 3)]  # 入场 bar1(px11) 离场 bar3(px13)
    cfg = StrategyConfig(stop_loss=0, cost=0.0)
    trades = run_long_strategy(bars, sigs, cfg)
    assert len(trades) == 1
    assert trades[0].entry_bar == 1 and trades[0].exit_bar == 3
    assert abs(trades[0].ret - (13 / 11 - 1)) < 1e-9


# ── 三卖不作多头离场（默认 exit_types 不含 SELL3）──
def test_sell3_not_exit():
    bars = _bars([10, 11, 12, 13, 14])
    sigs = [_mmd(MMDType.BUY1, 1), _mmd(MMDType.SELL3, 3)]  # SELL3 不离场
    trades = run_long_strategy(bars, sigs, StrategyConfig(cost=0.0))
    assert len(trades) == 0  # 持有到末尾仍未离场（无 SELL1/2）


# ── 止损（bar 收盘触发）──
def test_stop_loss():
    bars = _bars([10, 11, 10, 9, 8])
    sigs = [_mmd(MMDType.BUY1, 1)]  # 入场 bar1(px11)
    cfg = StrategyConfig(stop_loss=0.10, cost=0.0)  # -10% 止损
    trades = run_long_strategy(bars, sigs, cfg)
    assert len(trades) == 1 and trades[0].reason == "stop"
    assert trades[0].exit_bar == 3  # bar3 px9: 9/11-1=-18%≤-10% → 触发


# ── 趋势过滤：price<MA 不入场 ──
def test_trend_filter_blocks_entry():
    bars = _bars([20, 18, 16, 14, 12, 10])  # 持续下跌，price<EMA
    sigs = [_mmd(MMDType.BUY3, 3)]
    cfg = StrategyConfig(trend_ma=3, cost=0.0)
    assert run_long_strategy(bars, sigs, cfg) == []  # 下跌语境，趋势过滤挡住


# ── MTF 大级别趋势过滤：时间戳对齐 + C1 无前瞻 ──
def _big_bars():
    """大周期 bar（dt=idx 便于对齐）：段级震荡形成 ≥1 确认线段。"""
    turns = [("D", 10, 1), ("T", 13, 5), ("D", 11, 5), ("T", 16, 5), ("D", 13, 5), ("T", 20, 5),
             ("D", 17, 5), ("T", 18, 5), ("D", 11, 5), ("T", 14, 5), ("D", 6, 5), ("T", 9, 5),
             ("D", 7, 5), ("T", 13, 5), ("D", 11, 5), ("T", 22, 5)]
    centers = [turns[0][1]]
    cur = turns[0][1]
    for _k, lv, ln in turns[1:]:
        step = (lv - cur) / ln
        for _ in range(ln):
            cur += step
            centers.append(round(cur, 4))
        cur = lv
    return [Bar(idx=i, dt=i, o=x - 0.5, h=x + 0.5, l=x - 0.5, c=x + 0.5) for i, x in enumerate(centers)]


def test_mtf_trend_up_alignment_and_c1():
    big = _big_bars()
    duans = confirmed_only(find_duans_from_bars(big))
    assert duans, "大周期应形成 ≥1 确认线段"
    first = duans[0]
    cb = big[first.confirm_bar].dt  # int 时间戳
    # 操作 bar 时间戳：确认前 / 确认时 / 确认后
    op = [Bar(idx=i, dt=t, o=1, h=1, l=1, c=1) for i, t in enumerate([cb - 1, cb, cb + 1])]
    tu = mtf_trend_up(op, big)
    assert tu[0] is False  # C1：线段确认前 → 无大级别方向 → 不多头
    assert tu[1] == (first.direction is Direction.UP)  # 确认时点起反映首段方向
    assert tu[2] == (first.direction is Direction.UP)


def test_mtf_no_segment_all_false():
    big = [Bar(idx=i, dt=i, o=10, h=11, l=9, c=10) for i in range(2)]  # <3 bar 无线段
    op = [Bar(idx=0, dt=5, o=1, h=1, l=1, c=1)]
    assert mtf_trend_up(op, big) == [False]


# ── summary 统计 ──
def test_summary():
    bars = _bars([10, 11, 13, 12, 11, 13])
    sigs = [_mmd(MMDType.BUY1, 1), _mmd(MMDType.SELL1, 2),
            _mmd(MMDType.BUY1, 4), _mmd(MMDType.SELL1, 5)]
    trades = run_long_strategy(bars, sigs, StrategyConfig(cost=0.0))
    n, wr, comp = summary(trades)
    assert n == 2 and wr == 1.0 and comp > 0
