# -*- coding: utf-8 -*-
"""B1 · 背驰力度第三要素「柱子高度」(伸长高度/乖离极限) 测试。

原文 L025:111 力度三要素「1黄白线 2柱子面积 3柱子高度」+ L025:11 乖离极限
「面积大于前面但柱子高度不破=背驰」。口径(用户拍板):柱子(面积 OR 高度) AND 黄白线,
柱高度全级别生效(含笔),黄白线按频率门控。

RED 测试 = test_up_area_grows_but_height_not_new_high / test_down_对称:
改前(仅面积)这两例 is_beichi=False(面积未衰竭),改后(面积 OR 高度)=True。
保留性守护 = test_area_decays_still_beichi / test_height_also_new_high_not_beichi。
"""
from types import SimpleNamespace as NS

from chanlun.core.beichi_calculator import is_beichi

FREQ = "30m"   # ≥5m → _use_huangbai=True(黄白线参与),且 seg 非 BI 也走黄白线


def _ld_up(up_sum, hist_max, dif_max):
    """up 段 ld:面积=up_sum、柱高=hist_max(最高红柱)、黄白线高=dif_max。"""
    return {
        "dea": {"end": 0.0, "max": 0.0, "min": 0.0},
        "dif": {"end": dif_max, "max": dif_max, "min": 1.0},   # min>0:不触回0轴,纯靠高度衰减判黄白线
        "hist": {"sum": up_sum, "up_sum": up_sum, "down_sum": 0.0,
                 "max": hist_max, "min": 0.0, "end": 0.0},
    }


def _ld_down(down_sum, hist_min, dif_min):
    """down 段 ld:面积=down_sum、柱深=hist_min(最深绿柱,负)、黄白线低=dif_min。"""
    return {
        "dea": {"end": 0.0, "max": 0.0, "min": 0.0},
        "dif": {"end": dif_min, "max": -1.0, "min": dif_min},  # max<0:不触回0轴,纯靠高度衰减
        "hist": {"sum": abs(down_sum), "up_sum": 0.0, "down_sum": down_sum,
                 "max": 0.0, "min": hist_min, "end": 0.0},
    }


def _provider(seg_a, ld_a, ld_b):
    """ld_provider(start,end):按起点身份区分 a/b 段。"""
    return lambda s, e: ld_a if s is seg_a.start else ld_b


# ── RED 1：up 面积增大但柱高不创新高 → 背驰(乖离极限 L025:11) ──
def test_up_area_grows_but_height_not_new_high_is_beichi():
    seg_a = NS(type="up", high=12.0, low=5.0, start=NS(), end=NS())
    seg_b = NS(type="up", high=15.0, low=8.0, start=NS(), end=NS())   # high15>12 创新高
    ld_a = _ld_up(up_sum=18.0, hist_max=3.0, dif_max=5.0)
    ld_b = _ld_up(up_sum=30.0, hist_max=2.0, dif_max=2.0)             # 面积↑30 但柱高↓2 黄白线↓2
    assert is_beichi(seg_a, seg_b, _provider(seg_a, ld_a, ld_b), FREQ) is True


# ── RED 2：down 面积增大但柱深变浅 → 背驰(对称) ──
def test_down_area_grows_but_depth_shallower_is_beichi():
    seg_a = NS(type="down", high=20.0, low=12.0, start=NS(), end=NS())
    seg_b = NS(type="down", high=15.0, low=10.0, start=NS(), end=NS())  # low10<12 创新低
    ld_a = _ld_down(down_sum=18.0, hist_min=-3.0, dif_min=-5.0)
    ld_b = _ld_down(down_sum=30.0, hist_min=-2.0, dif_min=-2.0)         # 面积↑30 但柱深↓-2(更浅) 黄白线↓
    assert is_beichi(seg_a, seg_b, _provider(seg_a, ld_a, ld_b), FREQ) is True


# ── 守护：面积衰竭仍背驰(不回归) ──
def test_area_decays_still_beichi():
    seg_a = NS(type="up", high=12.0, low=5.0, start=NS(), end=NS())
    seg_b = NS(type="up", high=15.0, low=8.0, start=NS(), end=NS())
    ld_a = _ld_up(up_sum=18.0, hist_max=3.0, dif_max=5.0)
    ld_b = _ld_up(up_sum=6.0, hist_max=2.0, dif_max=2.0)               # 面积↓6 衰竭
    assert is_beichi(seg_a, seg_b, _provider(seg_a, ld_a, ld_b), FREQ) is True


# ── 守护：面积↑且柱高也创新高 → 非背驰(柱子维度都不弱) ──
def test_up_area_and_height_both_new_high_not_beichi():
    seg_a = NS(type="up", high=12.0, low=5.0, start=NS(), end=NS())
    seg_b = NS(type="up", high=15.0, low=8.0, start=NS(), end=NS())
    ld_a = _ld_up(up_sum=18.0, hist_max=3.0, dif_max=5.0)
    ld_b = _ld_up(up_sum=30.0, hist_max=5.0, dif_max=2.0)              # 面积↑30 柱高↑5(创新高) → 柱子不弱
    assert is_beichi(seg_a, seg_b, _provider(seg_a, ld_a, ld_b), FREQ) is False
