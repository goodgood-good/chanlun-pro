# -*- coding: utf-8 -*-
"""R5 part② · Bs1BranchCalculator 多级一类买卖点(L1+ 趋势背驰一类)。

块R 升级路径需 L1+ 一类(qs 趋势背驰),当前 bs2._first_points 已在各级算一类但仅作二类
中间量、未 emit。Bs1 emit 各级(level>=1)的 qs 趋势背驰一类(离开向下=1buy/向上=1sell),
供 R5 后 get_kuozhan_levels 升级买卖点(bs1+bs2+bs3)。L0 一类仍由 bs_branch 在 branch
模式产 → Bs1 只 emit level>=1(不重复 L0、不动 branch golden)。
"""
from types import SimpleNamespace as NS

from chanlun.core.bs1_branch import Bs1BranchCalculator


def _dv(is_beichi, kind, leave_type, end_val=10.0, prov=False):
    leave = NS(_type=leave_type, start=NS(val=end_val - 1, k=NS(date="d0", k_index=3)),
               end=NS(val=end_val, k=NS(date="d1", k_index=5)))
    return NS(is_beichi=is_beichi, kind=kind, leave_seg=leave, provisional=prov)


def _level(lvl, zss, done_div):
    return NS(level=lvl, zss=zss, done_divergence=done_div, units=[], zslxs=[])


def _two_levels(l1_dv):
    """[L0(空), L1(一个中枢 + l1_dv 背驰)]。"""
    zs = NS(zd=5.0, zg=10.0, gg=11.0, dd=4.0)
    return [_level(0, [], []), _level(1, [zs], [l1_dv])]


# ── RED 1：L1 底背驰(qs, leave down) → 1buy@L1 ──
def test_l1_bottom_qs_beichi_emits_1buy():
    pts = Bs1BranchCalculator().calculate(_two_levels(_dv(True, "qs", "down")))
    assert len(pts) == 1
    assert pts[0].bs_type == "1buy" and pts[0].level == 1


# ── RED 2：L1 顶背驰(qs, leave up) → 1sell@L1 ──
def test_l1_top_qs_beichi_emits_1sell():
    pts = Bs1BranchCalculator().calculate(_two_levels(_dv(True, "qs", "up")))
    assert len(pts) == 1 and pts[0].bs_type == "1sell" and pts[0].level == 1


# ── 守护：L0 一类不由 Bs1 emit(避免重复 L0、不动 branch) ──
def test_l0_not_emitted():
    lvls = [_level(0, [NS(zd=5.0, zg=10.0, gg=11.0, dd=4.0)], [_dv(True, "qs", "down")])]
    assert Bs1BranchCalculator().calculate(lvls) == []


# ── 守护：盘整背驰(pz)/非背驰 不 emit 一类(一类=趋势背驰) ──
def test_pz_or_not_beichi_skipped():
    assert Bs1BranchCalculator().calculate(_two_levels(_dv(True, "pz", "down"))) == []
    assert Bs1BranchCalculator().calculate(_two_levels(_dv(False, "qs", "down"))) == []


# ── 守护：provisional(进行中)不 emit(只坐实背驰) ──
def test_provisional_skipped():
    assert Bs1BranchCalculator().calculate(_two_levels(_dv(True, "qs", "down", prov=True))) == []
