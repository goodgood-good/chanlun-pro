"""tests/core/test_interval_nest_calculator.py — 区间套(interval_nest)测试。

缠论原文第三章·第六节《区间套》：根据背驰段从高级别向低级别逐级寻找背驰点。
本模块基于 ④ 的递归层级树实现。用例直接构造受控层级树喂入，不走 K 线流水线。
"""

from __future__ import annotations

from chanlun.core import interval_nest_calculator as inc
from chanlun.core.cl_interface import CLKline, Config, FX, Level, XD, ZS, ZSLX
from chanlun.core.recursive_calculator import LevelResult

WZGX = Config.ZS_WZGX_ZGGDD.value


def _fx(kidx: int, val: float, ftype: str) -> FX:
    k = CLKline(k_index=kidx, date=None, h=val, l=val, o=val, c=val, a=0.0, klines=[])
    return FX(_type=ftype, k=k, klines=[k], val=val)


def _seg(index: int, _type: str, start_val: float, end_val: float) -> XD:
    """构造一根线段(XD)。"""
    if _type == "up":
        start, end = _fx(index, start_val, "di"), _fx(index + 1, end_val, "ding")
    else:
        start, end = _fx(index, start_val, "ding"), _fx(index + 1, end_val, "di")
    xd = XD(start=start, end=end, _type=_type, index=index)
    xd.done = True
    xd.zs_high = max(start_val, end_val)
    xd.zs_low = min(start_val, end_val)
    return xd


def _zslx(level_obj: Level = Level.M1) -> ZSLX:
    """构造一个空走势类型对象。"""
    return ZSLX(zslx_level=level_obj)


def test_units_at_level_l0_is_xds():
    """_units_at_level：第 0 级走势单元 = 线段(xds)。"""
    xds = [_seg(0, "up", 4, 8), _seg(1, "down", 8, 5)]
    levels = []
    assert inc._units_at_level(levels, xds, 0) == xds


def test_units_at_level_l1_is_prev_zslxs():
    """_units_at_level：第 k(>=1) 级走势单元 = 第 k-1 级走势类型。"""
    xds = [_seg(0, "up", 4, 8)]
    z0 = _zslx()
    levels = [LevelResult(0, [], [z0]), LevelResult(1, [], [])]
    assert inc._units_at_level(levels, xds, 1) == [z0]
