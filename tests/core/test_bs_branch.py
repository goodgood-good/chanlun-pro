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
