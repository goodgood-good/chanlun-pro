"""tests/core/test_beichi_calculator.py — 级别无关背驰内核(beichi_calculator)测试。

缠论原文(《缠中说禅股市技术理论解释2017》第二章·第四节《背驰与盘整背驰》)：
  背驰 = 趋势力度比上一次趋势力度弱；盘整背驰 = 盘整中当下笔/线段比前一笔/线段弱。
原文细则(第三章·第二十五节实战例)：
  - 力度口径与级别相关：1分钟以下级别只比柱子面积；1分钟级别及以上加黄白线。
  - 创新高是前提：「背驰如果没有创新高，是不存在的」。

这些用例直接构造受控走势段 + 假 ld_provider 喂入内核，不走 K 线流水线。
"""

from __future__ import annotations

from chanlun.core import beichi_calculator as bc
from chanlun.core.cl_interface import BI, CLKline, FX, XD, ZSLX, Level


def _fx(kidx: int, val: float, ftype: str) -> FX:
    k = CLKline(k_index=kidx, date=None, h=val, l=val, o=val, c=val, a=0.0, klines=[])
    return FX(_type=ftype, k=k, klines=[k], val=val)


def _seg(cls, index: int, _type: str, start_val: float, end_val: float):
    """构造一根走势段。cls ∈ {BI, XD, ZSLX}。

    up 段起点底分型、终点顶分型；down 段相反。LINE.__init__ 会据此算 high/low。
    """
    if _type == "up":
        start, end = _fx(index, start_val, "di"), _fx(index + 1, end_val, "ding")
    else:
        start, end = _fx(index, start_val, "ding"), _fx(index + 1, end_val, "di")
    if cls is ZSLX:
        return ZSLX(zslx_level=Level.M1, start=start, end=end, _type=_type, index=index)
    return cls(start=start, end=end, _type=_type, index=index)


def test_use_huangbai_bi_is_area_only():
    """笔(BI)→ 仅柱子面积口径，不看黄白线（原文：1分钟以下只比柱子面积）。"""
    bi = _seg(BI, 0, "up", 4, 8)
    assert bc._use_huangbai(bi) is False


def test_use_huangbai_xd_and_zslx_use_huangbai():
    """线段(XD)/走势类型(ZSLX)→ 双重确认，需看黄白线（原文：1分钟级别及以上）。"""
    assert bc._use_huangbai(_seg(XD, 0, "up", 4, 8)) is True
    assert bc._use_huangbai(_seg(ZSLX, 0, "up", 4, 8)) is True


def test_xingao_xindi_up_requires_new_high():
    """up 段：后段创出更高的高点 → True；未创新高 → False。"""
    seg_a = _seg(XD, 0, "up", 4, 8)    # 高点 8
    seg_b_new = _seg(XD, 2, "up", 5, 10)   # 高点 10 > 8
    seg_b_old = _seg(XD, 2, "up", 5, 7)    # 高点 7 < 8
    assert bc._xingao_xindi(seg_a, seg_b_new) is True
    assert bc._xingao_xindi(seg_a, seg_b_old) is False


def test_xingao_xindi_down_requires_new_low():
    """down 段：后段创出更低的低点 → True；未创新低 → False。"""
    seg_a = _seg(XD, 0, "down", 10, 5)   # 低点 5
    seg_b_new = _seg(XD, 2, "down", 9, 3)  # 低点 3 < 5
    seg_b_old = _seg(XD, 2, "down", 9, 6)  # 低点 6 > 5
    assert bc._xingao_xindi(seg_a, seg_b_new) is True
    assert bc._xingao_xindi(seg_a, seg_b_old) is False


def _ld(up_sum=0.0, down_sum=0.0, dif_max=0.0, dif_min=0.0) -> dict:
    """构造 query_macd_ld 风格的 ld 字典（只填内核会读的字段）。"""
    return {
        "dea": {"end": 0.0, "max": 0.0, "min": 0.0},
        "dif": {"end": 0.0, "max": dif_max, "min": dif_min},
        "hist": {"sum": up_sum + down_sum, "up_sum": up_sum,
                 "down_sum": down_sum, "max": 0.0, "min": 0.0, "end": 0.0},
    }


def test_ld_decays_up_area_weaker():
    """up 段：后段红柱面积更小 → 力度衰竭 True。"""
    seg_a, seg_b = _seg(XD, 0, "up", 4, 8), _seg(XD, 2, "up", 5, 10)
    ld_a, ld_b = _ld(up_sum=100, dif_max=3), _ld(up_sum=50, dif_max=2)
    provider = {(0, 1): ld_a, (2, 3): ld_b}
    ldp = lambda s, e: provider[(s.k.k_index, e.k.k_index)]
    assert bc._ld_decays(seg_a, seg_b, ldp) is True


def test_ld_decays_up_area_not_weaker():
    """up 段：后段红柱面积没变小 → False。"""
    seg_a, seg_b = _seg(XD, 0, "up", 4, 8), _seg(XD, 2, "up", 5, 10)
    provider = {(0, 1): _ld(up_sum=50, dif_max=2), (2, 3): _ld(up_sum=100, dif_max=1)}
    ldp = lambda s, e: provider[(s.k.k_index, e.k.k_index)]
    assert bc._ld_decays(seg_a, seg_b, ldp) is False


def test_ld_decays_xd_needs_huangbai_too():
    """线段：柱子面积衰竭但黄白线没衰竭 → False（双重确认）。"""
    seg_a, seg_b = _seg(XD, 0, "up", 4, 8), _seg(XD, 2, "up", 5, 10)
    # up_sum 衰竭(100→50)，但 dif_max 反而升高(2→3) → 黄白线没衰竭
    provider = {(0, 1): _ld(up_sum=100, dif_max=2), (2, 3): _ld(up_sum=50, dif_max=3)}
    ldp = lambda s, e: provider[(s.k.k_index, e.k.k_index)]
    assert bc._ld_decays(seg_a, seg_b, ldp) is False


def test_ld_decays_bi_skips_huangbai():
    """笔：只比柱子面积，黄白线没衰竭也不影响 → True（原文：1分钟以下只比柱子）。"""
    seg_a, seg_b = _seg(BI, 0, "up", 4, 8), _seg(BI, 2, "up", 5, 10)
    # 与上一用例同样的数据：up_sum 衰竭、dif_max 没衰竭
    provider = {(0, 1): _ld(up_sum=100, dif_max=2), (2, 3): _ld(up_sum=50, dif_max=3)}
    ldp = lambda s, e: provider[(s.k.k_index, e.k.k_index)]
    assert bc._ld_decays(seg_a, seg_b, ldp) is True


def test_ld_decays_down_uses_green_area_and_dif_min():
    """down 段：比绿柱面积 down_sum；黄白线衰竭 = dif_min 上移(less negative)。"""
    seg_a, seg_b = _seg(XD, 0, "down", 10, 5), _seg(XD, 2, "down", 9, 3)
    # down_sum 衰竭(100→50)；dif_min 由 -3 上移到 -2 → 黄白线衰竭
    provider = {(0, 1): _ld(down_sum=100, dif_min=-3), (2, 3): _ld(down_sum=50, dif_min=-2)}
    ldp = lambda s, e: provider[(s.k.k_index, e.k.k_index)]
    assert bc._ld_decays(seg_a, seg_b, ldp) is True
