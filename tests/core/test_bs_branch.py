"""tests/core/test_bs_branch.py — P5a 买卖点 TDD。

受控 ZsBranchResult + fake DivergenceResult + 受控 lines（_seg/_make_zs 范式，
绕笔划分浮点敏感）。
"""
from __future__ import annotations

from chanlun.core.cl_interface import CLKline, FX, XD, ZS
from chanlun.core.zs_branch import ZsBranchResult, DivergenceResult
from chanlun.core.bs_branch import BuySellPoint, BsBranchCalculator  # noqa: F401


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


def _dv(_type, leave_seg, kind="qs", is_beichi=True) -> DivergenceResult:
    return DivergenceResult(is_beichi=is_beichi, kind=kind,
                            compare_seg=leave_seg, leave_seg=leave_seg, provisional=False)


def _result(done_zss, done_div) -> ZsBranchResult:
    return ZsBranchResult(done_zss=list(done_zss), live=[], freeze_idx=0,
                          done_divergence=list(done_div))


# 一个标准中枢核心(本体[6,9]),不设 end → 三类不触发,只测一类
def _zs_no_end():
    return _make_zs([_seg(0, "up", 6, 9), _seg(1, "down", 9, 6), _seg(2, "up", 6, 9)], 6, 9)


def test_first_class_qs_down_is_1buy():
    c = _seg(5, "down", 10, 5)                       # 下跌趋势背驰离开段
    res = _result([_zs_no_end()], [_dv("down", c)])
    pts = BsBranchCalculator().calculate(res, [])
    assert len(pts) == 1
    assert pts[0].bs_type == "1buy"
    assert pts[0].anchor_fx is c.end                 # 锚离开段末端(di 低点)
    assert pts[0].divergence is not None


def test_first_class_qs_up_is_1sell():
    c = _seg(5, "up", 5, 10)
    res = _result([_zs_no_end()], [_dv("up", c)])
    pts = BsBranchCalculator().calculate(res, [])
    assert len(pts) == 1 and pts[0].bs_type == "1sell"


def test_first_class_pz_not_produced():
    c = _seg(5, "down", 10, 5)
    res = _result([_zs_no_end()], [_dv("down", c, kind="pz")])   # 盘整背驰不产一类
    assert BsBranchCalculator().calculate(res, []) == []


def test_first_class_non_beichi_not_produced():
    c = _seg(5, "down", 10, 5)
    res = _result([_zs_no_end()], [_dv("down", c, is_beichi=False)])
    assert BsBranchCalculator().calculate(res, []) == []


def test_first_class_none_divergence_skipped():
    res = _result([_zs_no_end()], [None])
    assert BsBranchCalculator().calculate(res, []) == []


def test_calculate_empty_returns_empty():
    assert BsBranchCalculator().calculate(_result([], []), []) == []


# 中枢本体核心[6,9],带离开段 end → 测三类
def _zs_with_leave(leave):
    return _make_zs([_seg(0, "up", 6, 9), _seg(1, "down", 9, 6), _seg(2, "up", 6, 9)], 6, 9, end=leave)


def test_third_class_up_retest_holds_zg_is_3buy():
    leave = _seg(3, "up", 8, 14)                      # 向上离开(冲出 ZG=9)
    retest = _seg(4, "down", 14, 10)                  # 回试向下,低点 10 >= ZG=9 不破
    z = _zs_with_leave(leave)
    lines = [_seg(0, "up", 6, 9), _seg(1, "down", 9, 6), _seg(2, "up", 6, 9), leave, retest]
    pts = BsBranchCalculator().calculate(_result([z], [None]), lines)
    assert len(pts) == 1
    assert pts[0].bs_type == "3buy"
    assert pts[0].anchor_fx is retest.end            # 锚回试段末端(di 低点)
    assert pts[0].divergence is None


def test_third_class_up_retest_breaks_zg_none():
    leave = _seg(3, "up", 8, 14)
    retest = _seg(4, "down", 14, 7)                   # 低点 7 < ZG=9 破 → 不产
    z = _zs_with_leave(leave)
    lines = [leave, retest]
    assert BsBranchCalculator().calculate(_result([z], [None]), lines) == []


def test_third_class_down_retest_holds_zd_is_3sell():
    leave = _seg(3, "down", 6, 2)                     # 向下离开(跌破 ZD=6)
    retest = _seg(4, "up", 2, 5)                      # 回试向上,高点 5 <= ZD=6 不破
    z = _zs_with_leave(leave)
    lines = [leave, retest]
    pts = BsBranchCalculator().calculate(_result([z], [None]), lines)
    assert len(pts) == 1 and pts[0].bs_type == "3sell"


def test_third_class_no_retest_seg_none():
    leave = _seg(3, "up", 8, 14)
    z = _zs_with_leave(leave)
    lines = [leave]                                   # leave 是末段,无下一段
    assert BsBranchCalculator().calculate(_result([z], [None]), lines) == []


def test_first_and_third_coexist():
    # 同一中枢:向上离开段 qs 背驰(→1sell) + 回试不破 ZG(→3buy),两点并存
    leave = _seg(3, "up", 8, 14)
    retest = _seg(4, "down", 14, 10)
    z = _zs_with_leave(leave)
    lines = [leave, retest]
    pts = BsBranchCalculator().calculate(_result([z], [_dv("up", leave)]), lines)
    assert {p.bs_type for p in pts} == {"1sell", "3buy"}


def test_third_class_down_retest_breaks_zd_none():
    # 对称:向下离开,回试高点破 ZD → 不产 3sell
    leave = _seg(3, "down", 6, 2)
    retest = _seg(4, "up", 2, 8)                      # 高点 8 > ZD=6 破 → 不产
    z = _zs_with_leave(leave)
    lines = [leave, retest]
    assert BsBranchCalculator().calculate(_result([z], [None]), lines) == []
