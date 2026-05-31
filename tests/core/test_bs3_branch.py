"""tests/core/test_bs3_branch.py — P5c 多级三类买卖点 TDD。

受控多级 LevelResult（带 units）+ 中枢 z.end 离开段（_seg/_make_zs 范式）。
"""
from __future__ import annotations

from chanlun.core.cl_interface import CLKline, FX, XD, ZS
from chanlun.core.recursive_branch import LevelResult
from chanlun.core.bs3_branch import Bs3BranchCalculator


def _fx(kidx, val, ftype):
    k = CLKline(k_index=kidx, date=None, h=val, l=val, o=val, c=val, a=0.0, klines=[])
    return FX(_type=ftype, k=k, klines=[k], val=val)


def _seg(idx, _type, sv, ev) -> XD:
    if _type == "up":
        s, e = _fx(idx, sv, "di"), _fx(idx + 1, ev, "ding")
    else:
        s, e = _fx(idx, sv, "ding"), _fx(idx + 1, ev, "di")
    xd = XD(start=s, end=e, _type=_type, index=idx)
    xd.done = True
    xd.zs_high, xd.zs_low = max(sv, ev), min(sv, ev)
    return xd


def _make_zs(core, zd, zg, end=None) -> ZS:
    z = ZS(zs_type="xd", start=None)
    z.lines = list(core)
    z.zd, z.zg = zd, zg
    z._bounds_dirty = True
    z.update_boundaries()
    if end is not None:
        z.end = end
    return z


def _lr(level, zss, divs, units) -> LevelResult:
    return LevelResult(level=level, zss=list(zss), done_divergence=list(divs),
                       zslxs=[], upgrade_idx=[], units=list(units))


def _core():
    return [_seg(0, "up", 6, 9), _seg(1, "down", 9, 6), _seg(2, "up", 6, 9)]


def test_l0_third_buy():
    leave = _seg(3, "up", 8, 14)                  # 向上离开(冲出 ZG=9)
    retest = _seg(4, "down", 14, 10)              # 回试低点 10 ≥ ZG=9 不破
    z = _make_zs(_core(), 6, 9, end=leave)
    units = _core() + [leave, retest]
    pts = Bs3BranchCalculator().calculate([_lr(0, [z], [None], units)])
    assert len(pts) == 1
    assert pts[0].bs_type == "3buy" and pts[0].level == 0
    assert pts[0].anchor_fx is retest.end


def test_l1_third_buy_is_expand():
    # 扩张三买 = L1 中枢三类(level==1)
    leave = _seg(3, "up", 8, 14)
    retest = _seg(4, "down", 14, 10)
    z = _make_zs(_core(), 6, 9, end=leave)
    units = _core() + [leave, retest]
    pts = Bs3BranchCalculator().calculate([_lr(1, [z], [None], units)])
    assert len(pts) == 1 and pts[0].bs_type == "3buy" and pts[0].level == 1


def test_pending_no_end_no_third():
    z = _make_zs(_core(), 6, 9)                   # 无 end(pending)
    assert Bs3BranchCalculator().calculate([_lr(1, [z], [None], [])]) == []


def test_l0_third_sell():
    leave = _seg(3, "down", 6, 2)
    retest = _seg(4, "up", 2, 5)                  # 高点 5 ≤ ZD=6 不破
    z = _make_zs(_core(), 6, 9, end=leave)
    units = [leave, retest]
    pts = Bs3BranchCalculator().calculate([_lr(0, [z], [None], units)])
    assert len(pts) == 1 and pts[0].bs_type == "3sell" and pts[0].level == 0


def test_retest_breaks_zg_none():
    leave = _seg(3, "up", 8, 14)
    retest = _seg(4, "down", 14, 7)               # 低点 7 < ZG=9 破 → 不产
    z = _make_zs(_core(), 6, 9, end=leave)
    assert Bs3BranchCalculator().calculate([_lr(0, [z], [None], [leave, retest])]) == []


def test_multi_level_each_third():
    # L0 + L1 各出三类 → level 集合 {0,1}
    leave0 = _seg(3, "up", 8, 14)
    retest0 = _seg(4, "down", 14, 10)
    z0 = _make_zs(_core(), 6, 9, end=leave0)
    leave1 = _seg(13, "up", 8, 14)
    retest1 = _seg(14, "down", 14, 10)
    z1 = _make_zs(_core(), 6, 9, end=leave1)
    levels = [_lr(0, [z0], [None], _core() + [leave0, retest0]),
              _lr(1, [z1], [None], _core() + [leave1, retest1])]
    pts = Bs3BranchCalculator().calculate(levels)
    assert {p.level for p in pts} == {0, 1}


def test_empty_returns_empty():
    assert Bs3BranchCalculator().calculate([]) == []
