"""tests/core/test_recursive_calculator.py — 递归装配(RecursiveCalculator)测试。

缠论原文：中枢=某走势类型中3+连续次级别走势类型重叠；走势分解定理一/二；
延伸超9段升级（段2.1）。④ 把 ①②③ 交替递归成多级层级树。

用例直接构造受控线段序列 + 假 ld_provider 喂入计算器，不走 K 线流水线。
"""

from __future__ import annotations

from chanlun.core import recursive_calculator as rc
from chanlun.core.cl_interface import CLKline, Config, FX, Level, XD, ZS, ZSLX

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


def _ld(up_sum=50.0, down_sum=50.0, dif_max=2.0, dif_min=-2.0) -> dict:
    """构造 query_macd_ld 风格的 ld 字典（递归测试用 benign 力度，不触发背驰）。"""
    return {
        "dea": {"end": 0.0, "max": 0.0, "min": 0.0},
        "dif": {"end": 0.0, "max": dif_max, "min": dif_min},
        "hist": {"sum": up_sum + down_sum, "up_sum": up_sum,
                 "down_sum": down_sum, "max": 0.0, "min": 0.0, "end": 0.0},
    }


def _benign_ldp(s, e):
    return _ld()


def test_calculate_empty_returns_empty():
    """无线段 → 空层级列表。"""
    assert rc.RecursiveCalculator().calculate([], _benign_ldp, WZGX) == []


def _zslx(zss: list) -> ZSLX:
    """构造一个走势类型，仅含递归测试需要的 zss。"""
    z = ZSLX(zslx_level=Level.M1)
    z.zss = zss
    return z


def _zs(zg, zd, gg, dd) -> ZS:
    """构造一个中枢（只填边界字段）。"""
    return ZS(zs_type="xd", start=None, zg=zg, zd=zd, gg=gg, dd=dd)


def test_as_units_writes_range_and_index():
    """_as_units：ZSLX 的 zs_high/zs_low 取中枢包络 [min(dd),max(gg)]，index 重排。"""
    zslx0 = _zslx([_zs(zg=8, zd=5, gg=9, dd=4), _zs(zg=15, zd=12, gg=17, dd=10)])
    zslx1 = _zslx([_zs(zg=20, zd=18, gg=22, dd=16)])
    units = rc._as_units([zslx0, zslx1])
    assert units is not None and len(units) == 2
    assert (zslx0.zs_high, zslx0.zs_low) == (17, 4)   # max gg=17, min dd=4
    assert (zslx1.zs_high, zslx1.zs_low) == (22, 16)
    assert zslx0.index == 0 and zslx1.index == 1


def _zs_with_lines(lines: list) -> ZS:
    """构造一个中枢，含给定构成段 lines。"""
    z = ZS(zs_type="xd", start=None)
    z.lines = lines
    z._bounds_dirty = True
    z.update_boundaries()
    return z


def _overlap_segs(n: int) -> list:
    """n 根交替、范围都含 [5,8] 的线段。"""
    return [_seg(i, "up" if i % 2 == 0 else "down",
                 5 if i % 2 == 0 else 8, 8 if i % 2 == 0 else 5)
            for i in range(n)]


def test_split_oversized_keeps_short_zs():
    """构成段 ≤8 → 中枢不分裂。"""
    zs = _zs_with_lines(_overlap_segs(8))
    out = rc._split_oversized([zs])
    assert out == [zs]


def test_split_oversized_splits_nine_into_three():
    """构成段 9 → 拆成 3 个三段子中枢。"""
    zs = _zs_with_lines(_overlap_segs(9))
    out = rc._split_oversized([zs])
    assert len(out) == 3
    assert [len(s.lines) for s in out] == [3, 3, 3]


def test_split_oversized_remainder_into_last_group():
    """N%3≠0：余数并入末组（10→3,3,4；11→3,3,5）。"""
    out10 = rc._split_oversized([_zs_with_lines(_overlap_segs(10))])
    assert [len(s.lines) for s in out10] == [3, 3, 4]
    out11 = rc._split_oversized([_zs_with_lines(_overlap_segs(11))])
    assert [len(s.lines) for s in out11] == [3, 3, 5]


def test_split_oversized_sub_zs_boundaries():
    """子中枢边界口径与 ZsCalculator 一致：zg=前三段 zs_high 之 min。"""
    out = rc._split_oversized([_zs_with_lines(_overlap_segs(9))])
    for sub in out:
        assert (sub.zg, sub.zd) == (8, 5)
        assert (sub.gg, sub.dd) == (8, 5)


def test_calculate_eight_overlap_stops_at_L0():
    """8 根重叠线段 → 1 个 8 段中枢、不分裂 → 1 个盘整 → 不足 3 个、停在 L0。"""
    results = rc.RecursiveCalculator().calculate(_overlap_segs(8), _benign_ldp, WZGX)
    assert len(results) == 1
    assert results[0].level == 0
    assert len(results[0].zss) == 1
    assert len(results[0].zslxs) == 1


def test_calculate_nine_overlap_recurses_to_L1():
    """9 根重叠线段 → L0 经 9 段分裂出 3 个子中枢 + 3 个盘整 → 升出 L1。"""
    results = rc.RecursiveCalculator().calculate(_overlap_segs(9), _benign_ldp, WZGX)
    assert len(results) == 2
    assert results[0].level == 0
    assert len(results[0].zss) == 3        # 9 段分裂后
    assert len(results[0].zslxs) == 3
    assert results[1].level == 1
    assert len(results[1].zss) == 1        # 3 个盘整重叠 → 1 个 L1 中枢


def test_calculate_few_segments_no_zhongshu():
    """线段太少、扫不出中枢 → 空层级列表。"""
    results = rc.RecursiveCalculator().calculate(_overlap_segs(2), _benign_ldp, WZGX)
    assert results == []
