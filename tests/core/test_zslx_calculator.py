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


def _ld(up_sum=0.0, down_sum=0.0, dif_max=0.0, dif_min=0.0) -> dict:
    """构造 query_macd_ld 风格的 ld 字典。"""
    return {
        "dea": {"end": 0.0, "max": 0.0, "min": 0.0},
        "dif": {"end": 0.0, "max": dif_max, "min": dif_min},
        "hist": {"sum": up_sum + down_sum, "up_sum": up_sum,
                 "down_sum": down_sum, "max": 0.0, "min": 0.0, "end": 0.0},
    }


def test_wt_beichi_panzheng_true():
    """单中枢盘整：离开段相对中枢内前一同向段力度衰竭 → 背驰 True。"""
    core_a = _seg(0, "up", 4, 8)
    core_b = _seg(2, "down", 8, 5)
    core_c = _seg(4, "up", 5, 10)   # 离开段 zs.lines[-1]，创新高 10 > 8
    zs = _zs(0, [core_a, core_b, core_c], zg=8, zd=5, gg=8, dd=4)
    provider = {(0, 1): _ld(up_sum=100, dif_max=3),   # core_a
                (4, 5): _ld(up_sum=50, dif_max=2)}    # core_c 离开段
    ldp = lambda s, e: provider[(s.k.k_index, e.k.k_index)]
    assert zc._wt_beichi([zs], [core_a, core_b, core_c], ldp, WZGX) is True


def test_wt_beichi_no_beichi_false():
    """力度未衰竭 → 非背驰。"""
    core_a = _seg(0, "up", 4, 8)
    core_b = _seg(2, "down", 8, 5)
    core_c = _seg(4, "up", 5, 10)
    zs = _zs(0, [core_a, core_b, core_c], zg=8, zd=5, gg=8, dd=4)
    provider = {(0, 1): _ld(up_sum=50, dif_max=2), (4, 5): _ld(up_sum=100, dif_max=3)}
    ldp = lambda s, e: provider[(s.k.k_index, e.k.k_index)]
    assert zc._wt_beichi([zs], [core_a, core_b, core_c], ldp, WZGX) is False


def test_calculate_empty_returns_empty():
    """无中枢 → 空列表。"""
    assert zc.ZslxCalculator().calculate([], [], lambda s, e: {}, WZGX) == []


def test_calculate_single_zs_pending_panzheng():
    """单中枢 → 1 个 pending 盘整走势类型。"""
    lines = [_seg(0, "up", 4, 8), _seg(1, "down", 8, 5), _seg(2, "up", 5, 10)]
    zs = _zs(0, lines, zg=8, zd=5, gg=8, dd=4)
    wts = zc.ZslxCalculator().calculate([zs], lines, lambda s, e: {}, WZGX)
    assert len(wts) == 1
    assert wts[0].zslx_type == "盘整"
    assert wts[0].done is False
    assert wts[0].zss == [zs]


def test_calculate_two_up_zs_pending_uptrend():
    """2 个依次向上中枢、无背驰 → 1 个 pending 上涨趋势。"""
    z1_lines = [_seg(0, "up", 4, 8), _seg(1, "down", 8, 5), _seg(2, "up", 5, 9)]
    z2_lines = [_seg(3, "up", 12, 16), _seg(4, "down", 16, 13), _seg(5, "up", 13, 18)]
    z1 = _zs(0, z1_lines, zg=8, zd=5, gg=9, dd=4)
    z2 = _zs(1, z2_lines, zg=16, zd=13, gg=18, dd=12)
    ldp = lambda s, e: _ld(up_sum=100, dif_max=5)
    wts = zc.ZslxCalculator().calculate([z1, z2], z1_lines + z2_lines, ldp, WZGX)
    assert len(wts) == 1
    assert wts[0].zslx_type == "上涨"
    assert wts[0].done is False
    assert wts[0].zss == [z1, z2]


def test_calculate_direction_break_splits():
    """方向断裂：向上中枢后接向下中枢 → 切成 2 个走势类型。"""
    z1_lines = [_seg(0, "up", 4, 8), _seg(1, "down", 8, 5), _seg(2, "up", 5, 9)]
    z2_lines = [_seg(3, "down", 9, 3), _seg(4, "up", 3, 6), _seg(5, "down", 6, 1)]
    z1 = _zs(0, z1_lines, zg=8, zd=5, gg=9, dd=4)
    z2 = _zs(1, z2_lines, zg=6, zd=3, gg=9, dd=1)   # 整体在 z1 之下 → 反向
    ldp = lambda s, e: _ld(up_sum=100, down_sum=100, dif_max=5, dif_min=-5)
    wts = zc.ZslxCalculator().calculate([z1, z2], z1_lines + z2_lines, ldp, WZGX)
    assert len(wts) == 2
    assert wts[0].zss == [z1] and wts[0].done is True
    assert wts[1].zss == [z2] and wts[1].done is False


def test_calculate_beichi_terminates_trend():
    """背驰终结：2 向上中枢且第二个离开段趋势背驰 → 第一个走势类型 done。"""
    # A 段：进入前一中枢之前的同向段，beichi_qs 的比较对象
    a_seg = _seg(0, "up", 2, 6)
    # z1 的进入段（一个 LINE），其起点 k_index=2 作为 beichi_qs 的时间边界
    z1_entry = _seg(2, "up", 3, 8)
    z1_lines = [_seg(4, "up", 4, 8), _seg(5, "down", 8, 5), _seg(6, "up", 5, 9)]
    z2_lines = [_seg(7, "up", 12, 16), _seg(8, "down", 16, 13), _seg(9, "up", 13, 20)]
    z1 = _zs(0, z1_lines, zg=8, zd=5, gg=9, dd=4)
    z1.start = z1_entry          # 进入段，使 beichi_qs 能定位 A 段
    z2 = _zs(1, z2_lines, zg=16, zd=13, gg=20, dd=12)
    all_lines = [a_seg, z1_entry] + z1_lines + z2_lines
    # A 段(_seg 0)力度强、z2 离开段(_seg 9)力度弱 → 趋势背驰
    provider = {(0, 1): _ld(up_sum=100, dif_max=5),    # a_seg 比较段
                (9, 10): _ld(up_sum=30, dif_max=1)}    # _seg(9) 离开段，力度弱
    def ldp(s, e):
        return provider.get((s.k.k_index, e.k.k_index), _ld(up_sum=80, dif_max=4))
    wts = zc.ZslxCalculator().calculate([z1, z2], all_lines, ldp, WZGX)
    assert len(wts) == 1
    assert wts[0].zss == [z1, z2]
    assert wts[0].done is True
    assert wts[0].zslx_type == "上涨"
