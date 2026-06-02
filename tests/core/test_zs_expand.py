from chanlun.core.cl_interface import ZS
from chanlun.core.zs_expand import is_zs_expand


def _zs(zd, zg, dd, gg, done=True, line_num=3):
    """构造测试用中枢：直接写区间，start=None（被测函数不依赖 start）。"""
    z = ZS(zs_type="xd", start=None)
    z.zd, z.zg, z.dd, z.gg = zd, zg, dd, gg
    z.done = done
    z.line_num = line_num
    return z


def test_expand_core_separated_envelope_overlap_true():
    # 前核心[10,12]包络[9,13]；后核心[7,9]包络[8,11]：核心区分离(9<10)、包络重叠(11>=9)
    prev = _zs(zd=10, zg=12, dd=9, gg=13)
    cur = _zs(zd=7, zg=9, dd=8, gg=11)
    assert is_zs_expand(prev, cur) is True


def test_extend_core_overlap_false():
    # 核心区也重叠(后zg=11>前zd=10) → 延伸，非扩展
    prev = _zs(zd=10, zg=12, dd=9, gg=13)
    cur = _zs(zd=9, zg=11, dd=8, gg=12)
    assert is_zs_expand(prev, cur) is False


def test_trend_envelope_separated_false():
    # 包络分离(后dd=14>前gg=13) → 趋势，非扩展
    prev = _zs(zd=10, zg=12, dd=9, gg=13)
    cur = _zs(zd=15, zg=17, dd=14, gg=18)
    assert is_zs_expand(prev, cur) is False


def test_touch_closed_interval_true():
    # 闭区间触及：后gg=9 == 前dd=9 → 包络触及算重叠；核心区分离 → 扩展
    prev = _zs(zd=10, zg=12, dd=9, gg=13)
    cur = _zs(zd=6, zg=8, dd=5, gg=9)
    assert is_zs_expand(prev, cur) is True


def test_not_done_false():
    prev = _zs(zd=10, zg=12, dd=9, gg=13)
    cur = _zs(zd=7, zg=9, dd=8, gg=11, done=False)
    assert is_zs_expand(prev, cur) is False


def test_prev_not_done_false():
    # prev 未完成 → 不参与扩展判定(扩展是确认结构)
    prev = _zs(zd=10, zg=12, dd=9, gg=13, done=False)
    cur = _zs(zd=7, zg=9, dd=8, gg=11)
    assert is_zs_expand(prev, cur) is False


from chanlun.core.cl_interface import ZSLX
from chanlun.core.zs_expand import materialize_expansions


def _zslx(zs_low, zs_high, zss):
    """构造测试用走势类型：zs_low=DD / zs_high=GG，zss=其中枢列表。"""
    w = ZSLX(zslx_level=None, start=None, end=None)
    w.zs_low, w.zs_high = zs_low, zs_high
    w.zss = list(zss)
    return w


def test_materialize_expand_three_zslx():
    # 2 个扩展中枢 z0,z1，跨越 3 个走势类型 w0,w1,w2(包络分别 [9,13][8,11][8.5,12])
    z0 = _zs(zd=10, zg=12, dd=9, gg=13)
    z1 = _zs(zd=7, zg=9, dd=8, gg=11)
    w0 = _zslx(9, 13, [z0])
    w1 = _zslx(8, 11, [z0, z1])   # 盘整走势类型含两扩展中枢
    w2 = _zslx(8.5, 12, [z1])
    out = materialize_expansions([z0, z1], [w0, w1, w2])
    assert len(out) == 1
    hi = out[0]
    # 核心区=重合：ZG=min(13,11,12)=11，ZD=max(9,8,8.5)=9
    assert hi.zg == 11 and hi.zd == 9
    # 包络=并集：GG=max(13,11,12)=13，DD=min(9,8,8.5)=8
    assert hi.gg == 13 and hi.dd == 8
    assert hi.done is True            # 跨越 3 走势类型 = 完成式
    assert hi.expanded_with == [z0, z1]


def test_materialize_forming_two_zslx():
    # 跨越仅 2 走势类型 → forming(done=False)
    z0 = _zs(zd=10, zg=12, dd=9, gg=13)
    z1 = _zs(zd=7, zg=9, dd=8, gg=11)
    w0 = _zslx(9, 13, [z0])
    w1 = _zslx(8, 11, [z1])
    out = materialize_expansions([z0, z1], [w0, w1])
    assert len(out) == 1 and out[0].done is False


def test_materialize_no_expand_skipped():
    # 趋势(包络分离)：不产升级中枢
    z0 = _zs(zd=10, zg=12, dd=9, gg=13)
    z1 = _zs(zd=15, zg=17, dd=14, gg=18)
    w0 = _zslx(9, 13, [z0])
    w1 = _zslx(14, 18, [z1])
    assert materialize_expansions([z0, z1], [w0, w1]) == []


def test_materialize_extension_nine_lines():
    # 单中枢 9 段延伸：自成一组升级
    z = _zs(zd=10, zg=12, dd=9, gg=13, line_num=9)
    w = _zslx(9, 13, [z])
    out = materialize_expansions([z], [w])
    assert len(out) == 1 and out[0].expanded_with == [z]
