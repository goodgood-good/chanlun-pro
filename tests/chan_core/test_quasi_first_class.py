# -*- coding: utf-8 -*-
"""B3 · 类一买/类一卖(L027 盘整背驰=历史性底部=类买点)。

原文 L027:大级别盘整背驰 → 历史底/类一买(类=quasi,缠明言需次级别确认、操作意义弱)。
done_divergence 含 pz(盘整背驰)条目(zs_branch:402 kind="qs"if趋势else"pz"),pz+is_beichi
即类一买源。QuasiFirstClassCalculator emit 各级 pz+is_beichi 的离开段 → 类1buy/类1sell。

★bs_type='类1buy'/'类1sell' 刻意区别于 strict 一类(qs,bs1_branch);且**不入默认
get_branch_bspoints/回测 BUYS 白名单**(engine.buy_class 取首字符 int,'类'会崩),作单独
marker(cd.get_branch_quasi_first)暴露,供图表/分析/可选,不改默认信号/golden/回测。
"""
from types import SimpleNamespace as NS

from chanlun.core.bs1_branch import QuasiFirstClassCalculator


def _dv(is_beichi, kind, leave_type, prov=False):
    leave = NS(_type=leave_type, start=NS(val=9.0, k=NS(date="d0", k_index=3)),
               end=NS(val=10.0, k=NS(date="d1", k_index=5)))
    return NS(is_beichi=is_beichi, kind=kind, leave_seg=leave, provisional=prov)


def _level(lvl, zss, dd):
    return NS(level=lvl, zss=zss, done_divergence=dd, units=[], zslxs=[])


def _one(dv, lvl=0):
    zs = NS(zd=5.0, zg=10.0, gg=11.0, dd=4.0)
    return [_level(lvl, [zs], [dv])]


# ── RED 1：盘整背驰(pz, is_beichi, leave down) → 类1buy ──
def test_pz_beichi_down_emits_quasi_1buy():
    pts = QuasiFirstClassCalculator().calculate(_one(_dv(True, "pz", "down")))
    assert len(pts) == 1 and pts[0].bs_type == "类1buy" and pts[0].level == 0


# ── RED 2：盘整背驰(pz, is_beichi, leave up) → 类1sell ──
def test_pz_beichi_up_emits_quasi_1sell():
    pts = QuasiFirstClassCalculator().calculate(_one(_dv(True, "pz", "up")))
    assert len(pts) == 1 and pts[0].bs_type == "类1sell"


# ── 守护：趋势背驰(qs)不由本计算器出(那是 strict 一类 bs1) ──
def test_qs_not_emitted_here():
    assert QuasiFirstClassCalculator().calculate(_one(_dv(True, "qs", "down"))) == []


# ── 守护：pz 但未背驰 / provisional → 不出 ──
def test_pz_not_beichi_or_provisional_skipped():
    assert QuasiFirstClassCalculator().calculate(_one(_dv(False, "pz", "down"))) == []
    assert QuasiFirstClassCalculator().calculate(_one(_dv(True, "pz", "down", prov=True))) == []
