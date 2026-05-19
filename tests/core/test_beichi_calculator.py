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
