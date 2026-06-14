"""§4 级别递归装配 测试。f2 恒定：每层 find_zhongshus + find_zoushi，单元换上层走势类型。"""

from chan.config import DEFAULT
from chan.recursive import build_recursive
from chan.types import (
    ChanStatus,
    Direction,
    Duan,
    ZoushiKind,
    ZoushiType,
    confirmed_only,
)
from chan.zhongshu import find_zhongshus

U, D = Direction.UP, Direction.DOWN


def _dn(direction, lo, hi, cb):
    sv, ev = (lo, hi) if direction is U else (hi, lo)
    return Duan(direction=direction, start_bi=0, end_bi=0, start_value=sv, end_value=ev,
                high=hi, low=lo, confirm_bar=cb, status=ChanStatus.CONFIRMED)


def _zsl(direction, lo, hi, cb):
    sv, ev = (lo, hi) if direction is U else (hi, lo)
    return ZoushiType(kind=ZoushiKind.UP if direction is U else ZoushiKind.DOWN, direction=direction,
                      start_unit=0, end_unit=0, start_value=sv, end_value=ev, high=hi, low=lo,
                      confirm_bar=cb, status=ChanStatus.CONFIRMED, level=0)


# ── find_zhongshus 泛型：吃走势类型单元 → 高级别中枢（f2 恒定）──
def test_zhongshu_generic_over_zoushi():
    units = [_zsl(U, 10, 15, 1), _zsl(D, 11, 15, 2), _zsl(U, 11, 14, 3), _zsl(D, 8, 12, 4)]
    z1 = find_zhongshus(units, DEFAULT, level=1)
    assert len(z1) == 1
    assert z1[0].level == 1
    assert (z1[0].zd, z1[0].zg) == (11, 14)


# ── build_recursive：手造线段 → L0 中枢 + 走势类型 ──
def test_build_recursive_level0():
    segs = [_dn(U, 10, 15, 1), _dn(D, 11, 15, 2), _dn(U, 11, 14, 3), _dn(D, 8, 10, 4),
            _dn(U, 8, 11, 5), _dn(D, 9, 11, 6), _dn(U, 9, 12, 7), _dn(D, 5, 7, 8)]
    levels = build_recursive(segs, DEFAULT)
    assert levels[0].level == 0
    assert len(levels[0].zhongshus) == 2          # 两个段中枢
    assert all(z.level == 0 for z in levels[0].zhongshus)
    assert len(levels[0].zoushi) >= 1


# ── 级别终止：不足 3 段已确认走势类型 → 不升层（C1）──
def test_recursion_terminates_below_three_zoushi():
    segs = [_dn(U, 10, 15, 1), _dn(D, 11, 15, 2), _dn(U, 11, 14, 3), _dn(D, 8, 10, 4)]
    levels = build_recursive(segs, DEFAULT)
    # 仅 1 段中枢 → ≤1 走势类型(且 PENDING) → 无 L1
    assert all(lr.level == 0 for lr in levels)


# ── 只升已确认：末个 PENDING 走势类型不参与升层 ──
def test_only_confirmed_zoushi_recurse():
    segs = [_dn(U, 10, 15, 1), _dn(D, 11, 15, 2), _dn(U, 11, 14, 3), _dn(D, 8, 10, 4)]
    levels = build_recursive(segs, DEFAULT)
    z0 = levels[0].zoushi
    # 末走势类型 PENDING，confirmed_only 过滤后不足 3 → 未升层
    assert any(z.status is ChanStatus.PENDING for z in z0)
    assert len(confirmed_only(z0)) < 3


# ── 端到端：bar → 笔 → 线段 → 段中枢（段级震荡形成一个中枢）──
def test_e2e_from_bars_forms_zhongshu():
    from chan.recursive import build_recursive_from_bars
    from chan.types import Bar

    turns = [("D", 10, 1), ("T", 13, 5), ("D", 11, 5), ("T", 16, 5), ("D", 13, 5), ("T", 20, 5),
             ("D", 17, 5), ("T", 18, 5), ("D", 14, 5), ("T", 15, 5), ("D", 12, 5),
             ("T", 14, 5), ("D", 13, 5), ("T", 16, 5), ("D", 15, 5), ("T", 19, 5),
             ("D", 16, 5), ("T", 17, 5), ("D", 14, 5), ("T", 15, 5), ("D", 13, 5)]
    centers = [turns[0][1]]
    cur = turns[0][1]
    for _k, level, ln in turns[1:]:
        step = (level - cur) / ln
        for _ in range(ln):
            cur += step
            centers.append(round(cur, 4))
        cur = level
    bars = [Bar(idx=i, dt=None, o=x - 0.5, h=x + 0.5, l=x - 0.5, c=x + 0.5)
            for i, x in enumerate(centers)]
    levels = build_recursive_from_bars(bars)
    assert levels and levels[0].level == 0
    assert len(levels[0].zhongshus) >= 1
    z = levels[0].zhongshus[0]
    assert z.zd <= z.zg and z.dd <= z.zd and z.zg <= z.gg   # 区间自洽：[dd≤zd≤zg≤gg]


# ── 空输入 ──
def test_empty():
    assert build_recursive([], DEFAULT) == []
