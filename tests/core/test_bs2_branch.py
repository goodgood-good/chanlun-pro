"""tests/core/test_bs2_branch.py — P5b 二类买卖点 TDD。

受控多级 LevelResult + fake DivergenceResult（_seg 范式造 leave_seg）。
"""
from __future__ import annotations

from chanlun.core.cl_interface import CLKline, FX, XD, ZS
from chanlun.core.zs_branch import DivergenceResult
from chanlun.core.recursive_branch import LevelResult
from chanlun.core.bs2_branch import Bs2BranchCalculator


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
    return xd


def _zs() -> ZS:
    z = ZS(zs_type="xd", start=None)
    z.lines = [_seg(0, "up", 6, 9)]
    z.zd, z.zg = 6, 9
    z._bounds_dirty = True
    z.update_boundaries()
    return z


def _dv(_type, leave_seg, kind="qs", is_beichi=True, provisional=False) -> DivergenceResult:
    return DivergenceResult(is_beichi=is_beichi, kind=kind,
                            compare_seg=leave_seg, leave_seg=leave_seg, provisional=provisional)


def _lr(level, dvs) -> LevelResult:
    return LevelResult(level=level, zss=[_zs() for _ in dvs],
                       done_divergence=list(dvs), zslxs=[], upgrade_idx=[])


def test_basic_2buy():
    # L1 一买(c向下,end k=10 val=5) + L0 后续一买(end k=12 val=6,≥5,>10) → L1 二买
    c_k = _seg(9, "down", 10, 5)
    c_sub = _seg(11, "down", 9, 6)
    levels = [_lr(0, [_dv("down", c_sub)]), _lr(1, [_dv("down", c_k)])]
    pts = Bs2BranchCalculator().calculate(levels)
    assert len(pts) == 1
    assert pts[0].bs_type == "2buy" and pts[0].level == 1
    assert pts[0].anchor_fx is c_sub.end
    assert pts[0].divergence is not None


def test_2buy_breaks_prev_low_filtered():
    c_k = _seg(9, "down", 10, 5)
    c_sub = _seg(11, "down", 9, 3)               # 低点 3 < 5 破前低 → 跳过
    levels = [_lr(0, [_dv("down", c_sub)]), _lr(1, [_dv("down", c_k)])]
    assert Bs2BranchCalculator().calculate(levels) == []


def test_2buy_sub_before_lk_filtered():
    c_k = _seg(9, "down", 10, 5)                  # t=10
    c_sub = _seg(3, "down", 9, 6)                 # end k=4 < 10 在前 → 不算
    levels = [_lr(0, [_dv("down", c_sub)]), _lr(1, [_dv("down", c_k)])]
    assert Bs2BranchCalculator().calculate(levels) == []


def test_2buy_takes_first():
    c_k = _seg(9, "down", 10, 5)
    c_a = _seg(11, "down", 9, 6)                  # end k=12
    c_b = _seg(15, "down", 9, 7)                  # end k=16 更晚
    levels = [_lr(0, [_dv("down", c_a), _dv("down", c_b)]), _lr(1, [_dv("down", c_k)])]
    pts = Bs2BranchCalculator().calculate(levels)
    assert len(pts) == 1 and pts[0].anchor_fx is c_a.end   # 取最早


def test_2buy_same_direction_only():
    # L0 只有一卖(向上),L1 一买无同向配对 → 无二买
    c_k = _seg(9, "down", 10, 5)
    c_sub = _seg(11, "up", 6, 11)
    levels = [_lr(0, [_dv("up", c_sub)]), _lr(1, [_dv("down", c_k)])]
    assert Bs2BranchCalculator().calculate(levels) == []


def test_l0_no_second():
    c = _seg(9, "down", 10, 5)
    levels = [_lr(0, [_dv("down", c)])]
    assert Bs2BranchCalculator().calculate(levels) == []


def test_2sell_symmetric():
    # L1 一卖(c向上,high=15) + L0 后续一卖(high=14≤15,>10) → L1 二卖
    c_k = _seg(9, "up", 5, 15)
    c_sub = _seg(11, "up", 6, 14)
    levels = [_lr(0, [_dv("up", c_sub)]), _lr(1, [_dv("up", c_k)])]
    pts = Bs2BranchCalculator().calculate(levels)
    assert len(pts) == 1 and pts[0].bs_type == "2sell" and pts[0].level == 1


def test_provisional_excluded():
    c_k = _seg(9, "down", 10, 5)
    c_sub = _seg(11, "down", 9, 6)
    levels = [_lr(0, [_dv("down", c_sub, provisional=True)]), _lr(1, [_dv("down", c_k)])]
    assert Bs2BranchCalculator().calculate(levels) == []


def test_empty_returns_empty():
    assert Bs2BranchCalculator().calculate([]) == []


def test_three_levels_each_has_second():
    # 多级泛化:L2 一买(k4,v4) + L1 一买(k10,v5) + L0 一买(k21,v6)
    # L1 二买=L0 一买(k21>10,6≥5);L2 二买=L1 一买(k10>4,5≥4)
    c_l2 = _seg(3, "down", 11, 4)
    c_l1 = _seg(9, "down", 10, 5)
    c_l0 = _seg(20, "down", 9, 6)
    levels = [_lr(0, [_dv("down", c_l0)]), _lr(1, [_dv("down", c_l1)]), _lr(2, [_dv("down", c_l2)])]
    pts = Bs2BranchCalculator().calculate(levels)
    by_level = {p.level: p for p in pts}
    assert len(pts) == 2
    assert by_level[1].anchor_fx is c_l0.end     # L1 二买 = L0 一买
    assert by_level[2].anchor_fx is c_l1.end     # L2 二买 = L1 一买
