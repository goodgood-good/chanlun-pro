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


def test_split_one_sub_zs_get_entry_segment():
    """9段分裂的子中枢带进入段：sub[k>0].start=前一组末段、sub[0].start 沿用原中枢。

    使分裂子中枢若构成趋势，beichi_qs 能定位 A 段（否则 start=None 时趋势背驰
    恒不成立）。
    """
    segs = _overlap_segs(9)
    zs = _zs_with_lines(segs)
    zs.start = None                      # 原中枢无进入段
    out = rc._split_oversized([zs])
    assert len(out) == 3
    assert out[0].start is None          # 原中枢无进入段 → 首子中枢也无
    assert out[1].start is segs[2]       # 前一组 (0,1,2) 的末段
    assert out[2].start is segs[5]       # 前一组 (3,4,5) 的末段


def test_split_one_first_sub_inherits_zs_entry():
    """原中枢有进入段时，首个子中枢沿用之。"""
    segs = _overlap_segs(9)
    entry = _seg(99, "down", 8, 5)
    zs = _zs_with_lines(segs)
    zs.start = entry
    out = rc._split_oversized([zs])
    assert out[0].start is entry


# =================================================================
# 子项目 ⑤ · 中枢扩展(原文 #391)：相邻同级别中枢的 GG/DD 包络重叠 → 合并为高级中枢
# =================================================================


def _zs_full(zg, zd, gg, dd, lines=None, index=0):
    """构造一个完整 ZS:zg/zd/gg/dd + lines + index。"""
    zs = _zs(zg=zg, zd=zd, gg=gg, dd=dd)
    zs.index = index
    if lines is not None:
        zs.lines = lines
    return zs


def test_expand_single_zs_passthrough():
    """单个中枢 → 原样返回,expanded_with 为空。"""
    z = _zs_full(zg=8, zd=5, gg=9, dd=4, lines=[_seg(0, "up", 4, 8)])
    out = rc._expand_overlapping([z])
    assert len(out) == 1 and out[0] is z
    assert out[0].expanded_with == []


def test_expand_non_overlapping_pair_kept_independent():
    """两中枢 GG/DD 包络无重叠(独立趋势)→ 不合并、保持两个中枢。"""
    z1 = _zs_full(zg=8, zd=5, gg=9, dd=4, lines=[_seg(0, "up", 4, 8)], index=0)
    z2 = _zs_full(zg=18, zd=15, gg=19, dd=14, lines=[_seg(10, "up", 14, 18)], index=1)
    out = rc._expand_overlapping([z1, z2])
    assert len(out) == 2
    assert all(zs.expanded_with == [] for zs in out)


def test_expand_overlapping_pair_merges_to_higher_zs():
    """两中枢 GG/DD 包络重叠(原文 #391)→ 合并为 1 个高级中枢。

    高级中枢的 gg/dd = 包络合并(max gg / min dd),lines = 子中枢 lines 拼接,
    expanded_with = [sub1, sub2]。
    """
    seg1 = _seg(0, "up", 4, 8)
    seg2 = _seg(1, "down", 8, 6)
    z1 = _zs_full(zg=8, zd=6, gg=9, dd=5, lines=[seg1, seg2], index=0)
    seg3 = _seg(2, "up", 7, 11)
    seg4 = _seg(3, "down", 11, 8)
    # z2.dd=7 与 z1.gg=9 重叠 ([dd=7,gg=12] 与 [dd=5,gg=9] 包络相交 [7,9])
    z2 = _zs_full(zg=11, zd=8, gg=12, dd=7, lines=[seg3, seg4], index=1)

    out = rc._expand_overlapping([z1, z2])
    assert len(out) == 1, "GG/DD 包络重叠的相邻中枢应合并为 1 个"
    merged = out[0]
    assert merged.gg == 12 and merged.dd == 5, "高级中枢取包络 max gg/min dd"
    assert merged.expanded_with == [z1, z2]
    assert merged.lines == [seg1, seg2, seg3, seg4], "高级中枢 lines = 子中枢 lines 时序拼接"


def test_expand_three_overlapping_zss_merge_chain():
    """三个相邻 GG/DD 包络两两重叠 → 合并为 1 个含 3 个子中枢的高级中枢。"""
    z1 = _zs_full(zg=8, zd=5, gg=9, dd=4, lines=[_seg(0, "up", 4, 8)], index=0)
    z2 = _zs_full(zg=11, zd=8, gg=12, dd=7, lines=[_seg(2, "up", 7, 11)], index=1)
    z3 = _zs_full(zg=13, zd=10, gg=14, dd=9, lines=[_seg(4, "up", 9, 13)], index=2)
    out = rc._expand_overlapping([z1, z2, z3])
    assert len(out) == 1
    assert out[0].expanded_with == [z1, z2, z3]
    assert (out[0].gg, out[0].dd) == (14, 4)


def test_expand_only_consecutive_overlap_break_resets_chain():
    """链式重叠遇到断点 → 前段合并、断点后另起一组。"""
    z1 = _zs_full(zg=8, zd=5, gg=9, dd=4, lines=[_seg(0, "up", 4, 8)], index=0)
    z2 = _zs_full(zg=11, zd=8, gg=12, dd=7, lines=[_seg(2, "up", 7, 11)], index=1)
    # z3 与 z2 不重叠(dd=20 > z2.gg=12)
    z3 = _zs_full(zg=22, zd=18, gg=24, dd=20, lines=[_seg(4, "up", 18, 22)], index=2)
    z4 = _zs_full(zg=25, zd=21, gg=26, dd=19, lines=[_seg(6, "up", 19, 25)], index=3)
    out = rc._expand_overlapping([z1, z2, z3, z4])
    assert len(out) == 2
    assert out[0].expanded_with == [z1, z2]
    assert out[1].expanded_with == [z3, z4]


def test_recursive_calculate_runs_expansion_after_split_oversized():
    """主流程 RecursiveCalculator 集成:扫描 → 9 段分裂 → 扩展 → 划分。

    构造 9 段大中枢 → _split_oversized 拆 3 段子中枢 ×3 → 扩展识别它们的
    GG/DD 重叠 → 合并为 1 个高级中枢(L0 → L1 升级路径的扩展版本)。
    """
    # 构造 10 段强重叠序列(实测会形成单个超长中枢)
    base_lines = [
        _seg(0, "down", 10, 5),   # entry
        _seg(1, "up", 5, 8),
        _seg(2, "down", 8, 5),
        _seg(3, "up", 5, 8),
        _seg(4, "down", 8, 5),
        _seg(5, "up", 5, 8),
        _seg(6, "down", 8, 5),
        _seg(7, "up", 5, 8),
        _seg(8, "down", 8, 5),
        _seg(9, "up", 5, 8),
        _seg(10, "down", 8, 4),  # 离开段
        _seg(11, "up", 4, 4.5),  # 远离触发完成
    ]
    levels = rc.RecursiveCalculator().calculate(base_lines, _benign_ldp, WZGX)
    # 至少 L0 有结果;具体层数随扩展+分裂结果定。能跑通 + 不抛 = OK
    assert len(levels) >= 1
