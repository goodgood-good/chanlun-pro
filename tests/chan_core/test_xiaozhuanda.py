# -*- coding: utf-8 -*-
"""B6 · 小转大(小背驰-大转折)检测器测试。

原文 L044 小背驰-大转折定理:小级别(顶/底)背驰引发大级别(向下/向上)的**必要条件**
是该级别走势**最后一个次级别中枢出现第三类(卖/买)点**(只有必要、无充分)。
口径(用户拍板):块R 新增独立检测器、次级别背驰 + 最后次级别中枢三类点 → 小转大候选/
预警(非定性买卖点)。

用轻量 stub 构造块R levels(复用真 BsBranchCalculator._third_class,stub ZS/LINE/FX/
背驰),精确钉死门控:三买+底背驰→向上候选;缺三类/缺同向背驰/方向不匹配→无候选。
"""
from types import SimpleNamespace as NS

from chanlun.core.xiaozhuanda_branch import XiaoZhuanDaCalculator, XiaoZhuanDaCandidate


def _fx(val, i=0):
    return NS(val=float(val), k=NS(date=f"d{i}", k_index=i))


def _seg(t, sv, ev, i):
    return NS(_type=t, start=_fx(sv, i), end=_fx(ev, i + 1))


def _dv(is_beichi, leave_type, kind="qs"):
    return NS(is_beichi=is_beichi, kind=kind, leave_seg=NS(_type=leave_type))


def _level(lvl, zss, done_div, units):
    return NS(level=lvl, zss=zss, done_divergence=done_div, units=units, zslxs=[])


def _levels(last_has_third="3buy", sub_beichi_leave="down"):
    """构造 [sub(L0), big(L1)]。sub 最后中枢按 last_has_third 出三类、前中枢按
    sub_beichi_leave 出趋势背驰。last_has_third=None → 最后中枢无三类。"""
    done_div0 = _dv(True, sub_beichi_leave) if sub_beichi_leave else None
    zs0 = NS(end=None, zg=6.0, zd=3.0, gg=7.0, dd=2.0, lines=[])     # 前中枢:仅背驰、无三类(end=None)
    units = []
    if last_has_third == "3buy":
        leave = _seg("up", 8, 12, 10)      # 向上离开
        retest = _seg("down", 12, 11, 12)  # 回试 end=11 >= zg=10 → 3buy
        zs_last = NS(end=leave, zg=10.0, zd=7.0, gg=12.0, dd=6.0, lines=[])
        units = [leave, retest]
    elif last_has_third == "3sell":
        leave = _seg("down", 6, 2, 10)     # 向下离开
        retest = _seg("up", 2, 3, 12)      # 回抽 end=3 <= zd=4 → 3sell
        zs_last = NS(end=leave, zg=8.0, zd=4.0, gg=9.0, dd=1.0, lines=[])
        units = [leave, retest]
    else:                                   # 最后中枢无三类
        zs_last = NS(end=None, zg=10.0, zd=7.0, gg=12.0, dd=6.0, lines=[])
    sub = _level(0, [zs0, zs_last], [done_div0, None], units)
    big = _level(1, [NS(end=None, zg=0.0, zd=0.0, gg=0.0, dd=0.0, lines=[])], [None], [])
    return [sub, big]


# ── RED 1：最后次级别中枢三买 + 底背驰(leave down) → 向上小转大候选 ──
def test_up_candidate_when_last_sub_zs_3buy_and_bottom_beichi():
    cands = XiaoZhuanDaCalculator().calculate(_levels("3buy", "down"))
    assert len(cands) == 1
    c = cands[0]
    assert isinstance(c, XiaoZhuanDaCandidate)
    assert c.direction == "up" and c.level == 1


# ── RED 2：最后次级别中枢三卖 + 顶背驰(leave up) → 向下小转大候选 ──
def test_down_candidate_when_last_sub_zs_3sell_and_top_beichi():
    cands = XiaoZhuanDaCalculator().calculate(_levels("3sell", "up"))
    assert len(cands) == 1 and cands[0].direction == "down"


# ── 守护:最后中枢无三类 → 无候选(L044 必要条件不满足) ──
def test_no_candidate_without_third_at_last_zs():
    assert XiaoZhuanDaCalculator().calculate(_levels(None, "down")) == []


# ── 守护:有三买但无同向(底)背驰 → 无候选(次级别背驰缺) ──
def test_no_candidate_without_matching_beichi():
    assert XiaoZhuanDaCalculator().calculate(_levels("3buy", "up")) == []   # 只有顶背驰


# ── 守护:只有 L0(无大级别) → 无候选 ──
def test_no_candidate_without_higher_level():
    sub = _levels("3buy", "down")[0]
    assert XiaoZhuanDaCalculator().calculate([sub]) == []


# ── 端到端:真实 fixture(QQQ.US_1m,实测产 1 个 down/L1 候选)结构合理 ──
def test_get_xiaozhuanda_candidates_on_real_fixture():
    from pathlib import Path

    import pandas as pd

    from chanlun.core.cl import CL
    from chanlun.recursive_bt.engine.engine import CL_CFG

    fix = Path(__file__).resolve().parents[1] / "fixtures" / "synthetic" / "QQQ.US_1m.parquet"
    cd = CL("QQQ.US", "1m", dict(CL_CFG))
    cd.process_klines(pd.read_parquet(fix))
    cands = cd.get_xiaozhuanda_candidates()
    assert isinstance(cands, list) and len(cands) >= 1            # 实测 ≥1(必要条件满足)
    for c in cands:
        assert c.direction in ("up", "down")
        assert c.level >= 1 and c.sub_level == c.level - 1
        assert c.necessary_zs is not None and c.anchor_fx is not None
        assert c.invalid is not None
