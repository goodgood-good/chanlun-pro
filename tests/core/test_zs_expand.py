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
