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


def _zs_with_lines(lines: list) -> ZS:
    """构造一个中枢，含给定构成段。"""
    z = ZS(zs_type="xd", start=None)
    z.lines = lines
    return z


def test_drill_chain_single_level():
    """L0 走势类型：背驰段是线段(XD) → 链长 1、到 L0 即止。"""
    leave = _seg(9, "up", 5, 12)
    wt0 = _zslx()
    wt0.zss = [_zs_with_lines([_seg(7, "up", 4, 8), leave])]
    chain = inc._drill_chain(wt0, 0)
    assert len(chain) == 1
    level, cur, seg = chain[0]
    assert level == 0 and cur is wt0 and seg is leave


def test_drill_chain_two_levels():
    """L1 走势类型：背驰段是 L0 走势类型 → 继续下钻到 L0 的线段。"""
    l0_leave = _seg(9, "up", 5, 12)
    wt0 = _zslx()
    wt0.zss = [_zs_with_lines([_seg(7, "up", 4, 8), l0_leave])]
    wt1 = _zslx()
    wt1.zss = [_zs_with_lines([_zslx(), wt0])]   # L1 背驰段 = wt0(L0 走势类型)
    chain = inc._drill_chain(wt1, 1)
    assert [lv for lv, _, _ in chain] == [1, 0]
    assert chain[0][2] is wt0          # 第一重背驰段 = wt0
    assert chain[1][2] is l0_leave     # 第二重(L0)背驰段 = 线段


def _ld(up_sum=0.0, down_sum=0.0, dif_max=0.0, dif_min=0.0) -> dict:
    """构造 query_macd_ld 风格的 ld 字典。"""
    return {
        "dea": {"end": 0.0, "max": 0.0, "min": 0.0},
        "dif": {"end": 0.0, "max": dif_max, "min": dif_min},
        "hist": {"sum": up_sum + down_sum, "up_sum": up_sum,
                 "down_sum": down_sum, "max": 0.0, "min": 0.0, "end": 0.0},
    }


def _uptrend_beichi_fixture():
    """构造一个「上涨趋势 + 趋势背驰」的走势类型 wt 及其走势单元 units、ldp。

    复用 ② beichi_qs 的趋势背驰夹具形态：A 段力度强、离开段力度弱。
    返回 (wt, units, ldp)。
    """
    a_seg = _seg(0, "up", 3, 7)                 # A 段（比较对象）
    z1_entry = _seg(2, "up", 4, 8)              # z1 进入段，start.k_index=2
    leave = _seg(20, "up", 13, 20)              # z2 离开段（背驰段），创新高 20
    z1 = ZS(zs_type="xd", start=None, zg=8, zd=5, gg=9, dd=4)
    z1.start = z1_entry
    z1.lines = [_seg(4, "up", 4, 8)]
    z2 = ZS(zs_type="xd", start=None, zg=16, zd=13, gg=20, dd=12)
    z2.lines = [leave]
    wt = _zslx()
    wt.zslx_type = "上涨"
    wt._type = "up"
    wt.zss = [z1, z2]
    units = [a_seg, z1_entry]
    provider = {(0, 1): _ld(up_sum=100, dif_max=5),    # a_seg 力度强
                (20, 21): _ld(up_sum=30, dif_max=1)}   # leave 力度弱 → 背驰
    def ldp(s, e):
        return provider.get((s.k.k_index, e.k.k_index), _ld(up_sum=80, dif_max=4))
    return wt, units, ldp


def test_zslx_is_beichi_uptrend_true():
    """趋势走势类型 + 趋势背驰 → True。"""
    wt, units, ldp = _uptrend_beichi_fixture()
    assert inc._zslx_is_beichi(wt, units, ldp, WZGX) is True


def test_zslx_is_beichi_panzheng_no_lines_false():
    """走势类型无中枢/无构成段 → False（无可比段）。"""
    wt = _zslx()
    wt.zslx_type = "盘整"
    wt.zss = []
    assert inc._zslx_is_beichi(wt, [], lambda s, e: {}, WZGX) is False
