"""tests/core/test_zslx_calculator.py — 走势类型划分(ZslxCalculator)测试。

缠论原文(《缠中说禅股市技术理论解释2017》)：
  盘整 = 完成的走势类型只含 1 个走势中枢；
  趋势 = 完成的走势类型含 ≥2 个依次同向的走势中枢（上涨/下跌）；
  走势类型终结于背驰或盘整背驰（《中阴阶段》）。

用例直接构造受控中枢序列 + 假 ld_provider 喂入计算器，不走 K 线流水线。
"""

from __future__ import annotations

from chanlun.core import zslx_calculator as zc
from chanlun.core.cl_interface import CLKline, Config, FX, XD, ZS

WZGX = Config.ZS_WZGX_ZGGDD.value


def _fx(kidx: int, val: float, ftype: str) -> FX:
    k = CLKline(k_index=kidx, date=None, h=val, l=val, o=val, c=val, a=0.0, klines=[])
    return FX(_type=ftype, k=k, klines=[k], val=val)


def _seg(index: int, _type: str, start_val: float, end_val: float) -> XD:
    """构造一根走势段(线段)。"""
    if _type == "up":
        start, end = _fx(index, start_val, "di"), _fx(index + 1, end_val, "ding")
    else:
        start, end = _fx(index, start_val, "ding"), _fx(index + 1, end_val, "di")
    xd = XD(start=start, end=end, _type=_type, index=index)
    xd.done = True
    xd.zs_high = max(start_val, end_val)
    xd.zs_low = min(start_val, end_val)
    return xd


def _zs(index: int, lines: list, zg: float, zd: float, gg: float, dd: float) -> ZS:
    """构造中枢：lines 是走势段列表，zg/zd/gg/dd 显式给定。"""
    z = ZS(zs_type="xd", start=None, zg=zg, zd=zd, gg=gg, dd=dd)
    z.lines = lines
    z.index = index
    return z


def test_classify_single_zs_is_panzheng():
    """单中枢 → 盘整；方向取净涨跌。"""
    zs = _zs(0, [_seg(0, "up", 4, 8), _seg(1, "down", 8, 5), _seg(2, "up", 5, 10)],
             zg=8, zd=5, gg=8, dd=4)
    zslx_type, direction = zc._classify([zs], WZGX)
    assert zslx_type == "盘整"
    assert direction == "up"   # 首段起点 4 → 末段终点 10，净涨


def test_classify_two_up_zs_is_uptrend():
    """≥2 依次向上中枢 → 上涨趋势。"""
    z1 = _zs(0, [_seg(0, "up", 4, 8)], zg=8, zd=5, gg=9, dd=4)
    z2 = _zs(1, [_seg(2, "up", 12, 16)], zg=16, zd=12, gg=17, dd=10)
    zslx_type, direction = zc._classify([z1, z2], WZGX)
    assert zslx_type == "上涨"
    assert direction == "up"


def test_classify_two_down_zs_is_downtrend():
    """≥2 依次向下中枢 → 下跌趋势。"""
    z1 = _zs(0, [_seg(0, "down", 16, 12)], zg=16, zd=12, gg=17, dd=10)
    z2 = _zs(1, [_seg(2, "down", 8, 4)], zg=8, zd=5, gg=9, dd=4)
    zslx_type, direction = zc._classify([z1, z2], WZGX)
    assert zslx_type == "下跌"
    assert direction == "down"
