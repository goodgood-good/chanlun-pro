"""§1.4 线段（承重墙）测试。

fixture 用**直接构造的笔序列**（A-21 真值由定义推导）以隔离线段逻辑：
特征序列分型 / 第一二种情况 / 起始三笔重合 / 逆时间确认 / 截断等价。
"""

from chan.config import DEFAULT
from chan.duan import find_duans, find_duans_from_bars
from chan.types import Bar, Bi, ChanStatus, Direction, Fenxing, FxType, confirmed_only

U, D = Direction.UP, Direction.DOWN


def _bi(direction, low, high, confirm_bar):
    """最小确认笔（find_duans 只用 direction/high/low/confirm_bar）。"""
    fx = Fenxing(fx_type=FxType.DI, k_idx=0, high=high, low=low, confirm_bar=confirm_bar)
    return Bi(direction=direction, start_fx=fx, end_fx=fx, high=high, low=low,
              confirm_bar=confirm_bar, status=ChanStatus.CONFIRMED)


def _sig(d):
    return (d.start_bi, d.end_bi, int(d.direction), d.start_value, d.end_value, d.confirm_bar)


# ── 单个上线段（第一种情况：无缺口）──
def test_up_segment_case1():
    pens = [
        _bi(U, 10, 13, 1), _bi(D, 11, 13, 2), _bi(U, 11, 15, 3), _bi(D, 13, 15, 4),
        _bi(U, 13, 17, 5),  # 峰 17（线段终点）
        _bi(D, 14, 17, 6), _bi(U, 14, 16, 7), _bi(D, 11, 16, 8),  # 反转确认顶分型
    ]
    duans = find_duans(pens, DEFAULT)
    assert len(duans) == 1
    d0 = duans[0]
    assert d0.direction is U and (d0.start_bi, d0.end_bi) == (0, 4)
    assert (d0.start_value, d0.end_value) == (10, 17)  # 段内极点都在端点（標準化）
    assert d0.case == 1 and d0.status is ChanStatus.CONFIRMED
    assert d0.confirm_bar == 8  # 顶分型第三元素(p7)确认时


# ── 三线段交替 + ★截断等价（C1 筐一）──
def _three_seg_pens():
    return [
        _bi(U, 10, 13, 1), _bi(D, 11, 13, 2), _bi(U, 11, 15, 3), _bi(D, 13, 15, 4), _bi(U, 13, 17, 5),
        _bi(D, 14, 17, 6), _bi(U, 14, 16, 7), _bi(D, 11, 16, 8), _bi(U, 11, 13, 9), _bi(D, 8, 13, 10),
        _bi(U, 8, 12, 11), _bi(D, 10, 12, 12), _bi(U, 10, 18, 13), _bi(D, 15, 18, 14), _bi(U, 15, 20, 15),
        _bi(D, 16, 20, 16), _bi(U, 16, 19, 17), _bi(D, 13, 19, 18),
    ]


def test_three_segments_alternate():
    duans = find_duans(_three_seg_pens(), DEFAULT)
    assert [(d.direction, d.start_bi, d.end_bi) for d in duans] == [
        (U, 0, 4), (D, 5, 9), (U, 10, 14),
    ]
    # 端点：每段都是极点到极点（线段方向定理：上段不能有更低低点、下段不能有更高高点）
    assert [(d.start_value, d.end_value) for d in duans] == [(10, 17), (17, 8), (8, 20)]
    assert all(d.status is ChanStatus.CONFIRMED for d in duans)


def test_truncation_equivalence():
    pens = _three_seg_pens()
    full = find_duans(pens, DEFAULT)
    full_conf = [d for d in full if d.status is ChanStatus.CONFIRMED]
    for t in range(1, 19):
        trunc_pens = [b for b in pens if b.confirm_bar <= t]
        tc = [d for d in find_duans(trunc_pens, DEFAULT) if d.status is ChanStatus.CONFIRMED]
        exp = [d for d in full_conf if d.confirm_bar <= t]
        assert [_sig(d) for d in tc] == [_sig(d) for d in exp], f"t={t}: 线段截断等价被破坏"


# ── 第二种情况（有缺口）：先 PENDING_ENDPOINT，次段分型出现后逆时间确认 ──
def test_case2_gap_pending_then_reverse_confirms():
    base = [
        _bi(U, 10, 12, 1), _bi(D, 11, 12, 2), _bi(U, 11, 14, 3), _bi(D, 13, 14, 4),
        _bi(U, 13, 20, 5),  # 缺口跳升到 20
        _bi(D, 16, 20, 6), _bi(U, 16, 18, 7), _bi(D, 11, 18, 8),  # c 已现但次段分型未成
    ]
    d_before = find_duans(base, DEFAULT)
    assert len(d_before) == 1
    assert d_before[0].case == 2
    assert d_before[0].status is ChanStatus.PENDING_ENDPOINT  # 缺口→须次段确认

    # 续出次段（下段）的底分型 → 逆时间确认前段
    more = base + [_bi(U, 11, 14, 9), _bi(D, 8, 14, 10), _bi(U, 8, 12, 11),
                   _bi(D, 10, 12, 12), _bi(U, 10, 16, 13)]
    d_after = find_duans(more, DEFAULT)
    assert d_after[0].case == 2 and d_after[0].status is ChanStatus.CONFIRMED
    assert d_after[0].confirm_bar == 13  # 逆时间：次段分型确认 bar


# ── 起始三笔不重合 → 构不成线段（課65:57）──
def test_first_three_no_overlap_rejected():
    pens = [_bi(U, 10, 12, 1), _bi(D, 13, 15, 2), _bi(U, 16, 20, 3),
            _bi(D, 18, 22, 4), _bi(U, 21, 25, 5)]
    assert find_duans(pens, DEFAULT) == []


# ── confirmed_only 挡住 PENDING_ENDPOINT（下游可见性）──
def test_confirmed_only_excludes_pending_endpoint():
    base = [
        _bi(U, 10, 12, 1), _bi(D, 11, 12, 2), _bi(U, 11, 14, 3), _bi(D, 13, 14, 4),
        _bi(U, 13, 20, 5), _bi(D, 16, 20, 6), _bi(U, 16, 18, 7), _bi(D, 11, 18, 8),
    ]
    duans = find_duans(base, DEFAULT)
    assert confirmed_only(duans) == []  # 唯一线段是 PENDING_ENDPOINT → 下游不可见


# ── 不足三笔 → 空 ──
def test_too_few_pens():
    assert find_duans([_bi(U, 10, 13, 1), _bi(D, 11, 13, 2)], DEFAULT) == []


# ── 端到端：bar → 笔 → 线段（含首段逆势孤笔解析 + 截断等价）──
def _e2e_bars():
    turns = [("D", 10, 1), ("T", 13, 5), ("D", 11, 5), ("T", 16, 5), ("D", 13, 5),
             ("T", 20, 5), ("D", 16, 5), ("T", 18, 5), ("D", 11, 5), ("T", 14, 5),
             ("D", 6, 5), ("T", 9, 5), ("D", 7, 5), ("T", 13, 5), ("D", 11, 5), ("T", 22, 5)]
    centers = [turns[0][1]]
    cur = turns[0][1]
    for _kind, level, ln in turns[1:]:
        step = (level - cur) / ln
        for _ in range(ln):
            cur += step
            centers.append(round(cur, 4))
        cur = level
    return [Bar(idx=i, dt=None, o=x - 0.5, h=x + 0.5, l=x - 0.5, c=x + 0.5)
            for i, x in enumerate(centers)]


def test_e2e_pipeline_and_truncation():
    bars = _e2e_bars()
    duans = find_duans_from_bars(bars)
    # 首段=上(逆势孤笔被跳过)，到峰 20.5；其后为下段
    assert duans[0].direction is U
    assert duans[0].end_value == 20.5 and duans[0].status is ChanStatus.CONFIRMED
    assert duans[1].direction is D
    # 端到端截断等价（确定性结构无未来函数）
    full_conf = [d for d in duans if d.status is ChanStatus.CONFIRMED]
    for t in range(5, len(bars)):
        tc = [d for d in find_duans_from_bars(bars[: t + 1]) if d.status is ChanStatus.CONFIRMED]
        exp = [d for d in full_conf if d.confirm_bar <= t]
        assert [_sig(d) for d in tc] == [_sig(d) for d in exp], f"t={t}: e2e 线段截断等价被破坏"
