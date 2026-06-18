# -*- coding: utf-8 -*-
"""B4 · 最弱二类买卖点(原文 L101 第二类买点 case(二)最弱)。

L101:23「第二类买点跌破第一类买点…这是完全可以的,这里一般都构成盘整背驰」(娇注:反向力度
非常弱时出现、虽少见也有)。旧 second_class/bs2 仅取「不破前低」(强/一般档)、漏 case(二)。
B4:一类点后回调**跌破前低 BUT 对一类背驰段构成盘整背驰(is_beichi)** → 最弱二买(止损下移到新低)。

stub:LINE 同时设 .type(is_beichi 读)/._type(second_class 读)+ .high/.low;ld_provider 造
盘整背驰(回调段力度<一类背驰段)。复用 ZsBranchResult/BsBranchCalculator 真逻辑。
"""
from types import SimpleNamespace as NS

from chanlun.core.bs_branch import BsBranchCalculator
from chanlun.core.zs_branch import ZsBranchResult

FREQ = "30m"


def _fx(val, i):
    return NS(val=float(val), k=NS(date=f"d{i}", k_index=i))


def _seg(t, sv, ev, i):
    """LINE stub:.type/._type 同值(property 别名),high/low 按方向。"""
    hi, lo = (max(sv, ev), min(sv, ev))
    return NS(type=t, _type=t, high=float(hi), low=float(lo),
              start=_fx(sv, i), end=_fx(ev, i + 1))


def _ld_down(down_sum, hist_min, dif_min):
    return {"dea": {"end": 0.0, "max": 0.0, "min": 0.0},
            "dif": {"end": dif_min, "max": -1.0, "min": dif_min},
            "hist": {"sum": abs(down_sum), "up_sum": 0.0, "down_sum": float(down_sum),
                     "max": 0.0, "min": float(hist_min), "end": 0.0}}


def _scene(pullback_pz_beichi: bool):
    """一类 1buy(背驰段 c down,前低=100) → 反弹 → 回调破前低(95)。
    pullback_pz_beichi=True 时 ld 造回调段盘整背驰(力度<c)、False 时回调力度更强(非背驰)。"""
    c = _seg("down", 110, 100, 0)          # 一类背驰段,low=100=前低(extreme)
    rebound = _seg("up", 100, 108, 2)
    pullback = _seg("down", 108, 95, 4)    # 破前低 95<100
    z = NS(zd=98.0, zg=109.0, gg=110.0, dd=95.0)
    dv = NS(is_beichi=True, kind="qs", leave_seg=c, provisional=False)
    zr = ZsBranchResult(done_zss=[z], live=[], freeze_idx=0, done_divergence=[dv])
    lines = [c, rebound, pullback]
    ld_c = _ld_down(down_sum=30.0, hist_min=-5.0, dif_min=-5.0)
    ld_pb = (_ld_down(down_sum=10.0, hist_min=-2.0, dif_min=-2.0) if pullback_pz_beichi
             else _ld_down(down_sum=50.0, hist_min=-8.0, dif_min=-8.0))   # 更强=非背驰
    ldp = lambda s, e: ld_c if s is c.start else ld_pb   # noqa: E731
    return zr, lines, ldp


# ── RED：回调破前低 + 盘整背驰 → 最弱二买(2buy) ──
def test_weakest_2buy_when_break_low_and_pz_beichi():
    zr, lines, ldp = _scene(pullback_pz_beichi=True)
    pts = BsBranchCalculator().second_class(zr, lines, ldp, FREQ)
    twos = [p for p in pts if p.bs_type == "2buy"]
    assert len(twos) == 1                                    # 最弱二买出
    assert twos[0].structural_stop_below == 95.0             # 止损下移到回调新低


# ── 守护：回调破前低 但 非盘整背驰(力度更强) → 不出二买 ──
def test_no_2buy_when_break_low_but_stronger():
    zr, lines, ldp = _scene(pullback_pz_beichi=False)
    pts = BsBranchCalculator().second_class(zr, lines, ldp, FREQ)
    assert [p for p in pts if p.bs_type == "2buy"] == []


# ── 守护：无 ld_provider(旧调用) → 退化,破前低不出二买(不回归) ──
def test_no_ld_provider_no_weakest():
    zr, lines, _ = _scene(pullback_pz_beichi=True)
    pts = BsBranchCalculator().second_class(zr, lines)        # 不传 ld
    assert [p for p in pts if p.bs_type == "2buy"] == []


# ── B4 bs2 跨级最弱二买:L_k 一买后、次级别一买破前低+盘整背驰 → L_k 最弱二买 ──
def _bs2_scene(sub_pz_beichi: bool):
    from chanlun.core.bs2_branch import Bs2BranchCalculator   # noqa: F401 (供调用方导入)
    c_k = _seg("down", 110, 100, 0)        # L1 一买背驰段,前低=100,t_end=1
    c_sub = _seg("down", 104, 95, 10)      # 次级别(L0)一买,破前低 95<100,t_end=11(在后)
    dv_k = NS(is_beichi=True, kind="qs", leave_seg=c_k, provisional=False)
    dv_sub = NS(is_beichi=True, kind="qs", leave_seg=c_sub, provisional=False)
    L1 = NS(level=1, zss=[NS(zd=98.0, zg=109.0, gg=110.0, dd=95.0)], done_divergence=[dv_k])
    L0 = NS(level=0, zss=[NS(zd=96.0, zg=105.0, gg=106.0, dd=95.0)], done_divergence=[dv_sub])
    ld_k = _ld_down(30.0, -5.0, -5.0)
    ld_sub = (_ld_down(10.0, -2.0, -2.0) if sub_pz_beichi      # 弱=盘整背驰
              else _ld_down(50.0, -8.0, -8.0))                 # 更强=非背驰
    ldp = lambda s, e: ld_k if s is c_k.start else ld_sub      # noqa: E731
    return [L0, L1], ldp


def test_bs2_weakest_cross_level_2buy():
    from chanlun.core.bs2_branch import Bs2BranchCalculator
    levels, ldp = _bs2_scene(sub_pz_beichi=True)
    pts = Bs2BranchCalculator().calculate(levels, ldp, FREQ)
    twos = [p for p in pts if p.bs_type == "2buy" and p.level == 1]
    assert len(twos) == 1                                      # 最弱跨级二买
    assert twos[0].structural_stop_below == 95.0              # 止损=次级别新低


def test_bs2_no_weakest_when_sub_stronger():
    from chanlun.core.bs2_branch import Bs2BranchCalculator
    levels, ldp = _bs2_scene(sub_pz_beichi=False)
    pts = Bs2BranchCalculator().calculate(levels, ldp, FREQ)
    assert [p for p in pts if p.bs_type == "2buy"] == []      # 破前低非背驰→不出
