from chanlun.core.cl_interface import ZS
from chanlun.core.zs_expand import is_zs_expand, materialize_expansions


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


def test_materialize_expand_three_zhongshu():
    # 3 个相邻定理二扩展中枢 → 1 个高级别中枢(子中枢包络重合)
    z0 = _zs(zd=10, zg=12, dd=9, gg=13)
    z1 = _zs(zd=7, zg=9, dd=8, gg=11)         # z0-z1: 包络重叠[9,11]+核心区分离(9<10) → 扩展
    z2 = _zs(zd=13, zg=14, dd=8.5, gg=12)      # z1-z2: 包络重叠+核心区分离(z2.zd=13>z1.zg=9) → 扩展
    out = materialize_expansions([z0, z1, z2])
    assert len(out) == 1
    hi = out[0]
    # 核心区=重合：ZD=max(9,8,8.5)=9，ZG=min(13,11,12)=11
    assert hi.zd == 9 and hi.zg == 11
    # 包络=并集：DD=min(9,8,8.5)=8，GG=max(13,11,12)=13
    assert hi.dd == 8 and hi.gg == 13
    assert hi.done is True                      # 3 子中枢(=9段) = 完成式
    assert hi.expanded_with == [z0, z1, z2]


def test_materialize_forming_two_zhongshu():
    # 仅 2 个扩展中枢 → forming(done=False)
    z0 = _zs(zd=10, zg=12, dd=9, gg=13)
    z1 = _zs(zd=7, zg=9, dd=8, gg=11)
    out = materialize_expansions([z0, z1])
    assert len(out) == 1
    assert out[0].done is False                 # 2 子中枢 < 3 = 进行式
    assert out[0].zd == 9 and out[0].zg == 11    # 重合 [max(9,8), min(13,11)]
    assert out[0].expanded_with == [z0, z1]


def test_materialize_no_expand_skipped():
    # 趋势(包络分离)：不产升级中枢
    z0 = _zs(zd=10, zg=12, dd=9, gg=13)
    z1 = _zs(zd=15, zg=17, dd=14, gg=18)
    assert materialize_expansions([z0, z1]) == []


def test_materialize_degenerate_no_common_overlap_skipped():
    # 3 中枢两两扩展，但无共同核心重合(zd>=zg) → 不实体化(退化)
    z0 = _zs(zd=9.5, zg=10.5, dd=9, gg=11)
    z1 = _zs(zd=11.5, zg=12.5, dd=10, gg=14)    # z0-z1: 包络重叠[10,11]+核心区分离(11.5>10.5)
    z2 = _zs(zd=14, zg=15, dd=13, gg=16)         # z1-z2: 包络重叠[13,14]+核心区分离(14>12.5)
    # ZD=max(9,10,13)=13 >= ZG=min(11,14,16)=11 → 退化，跳过
    assert materialize_expansions([z0, z1, z2]) == []
