"""中枢关系三分类对齐原文中心定理二(L020)。

「后 GG<前 DD ⟺ 下跌及其延续;后 DD>前 GG ⟺ 上涨及其延续;
 后 ZG<前 ZD 且后 GG≥前 DD(或对称) ⟺ 形成高级别的走势中枢」。
即:波动区间 GG/DD(=**全部本体段**包络,离开段已剥)完全分离=趋势;核心区[ZD,ZG]
分离但波动重叠=升级(expand);核心区重叠=延伸(上游已合并,残余亦归 expand)。
原实现用「前 3 段本体包络」——**延伸段的波动被错误剔除**(原文 GG=max(gn) 遍历
中枢内所有 Z 段,含延伸段),升级情形会被误判为趋势。
"""
from chanlun.core.zs_branch import classify_rel


class _Seg:
    def __init__(self, lo, hi):
        self.zs_low, self.zs_high = float(lo), float(hi)


class _ZS:
    def __init__(self, zd, zg, dd, gg, seg_spans):
        self.zd, self.zg, self.dd, self.gg = map(float, (zd, zg, dd, gg))
        self.lines = [_Seg(lo, hi) for lo, hi in seg_spans]


def test_trend_up_requires_full_gg_dd_separation():
    prev = _ZS(12, 18, 10, 20, [(10, 18), (12, 20), (11, 19)])
    cur = _ZS(25, 33, 22, 35, [(22, 33), (25, 35), (24, 32)])   # cur.DD(22) > prev.GG(20)
    assert classify_rel(prev, cur) == "trend_up"


def test_trend_down_symmetric():
    prev = _ZS(25, 33, 22, 35, [(22, 33), (25, 35), (24, 32)])
    cur = _ZS(12, 18, 10, 20, [(10, 18), (12, 20), (11, 19)])
    assert classify_rel(prev, cur) == "trend_down"


def test_extension_wave_kills_false_trend():
    """延伸段远波(第 4 段到 23)并入 GG 后,cur.DD(21) 不再高于 prev.GG → 非趋势。

    旧「前 3 段包络」口径:p_gg=20 < c_dd=21 → 误判 trend_up;
    定理二口径:prev.GG=23 ≥ cur.DD=21 且核心分离(cur.ZD 22 > prev.ZG 18) → expand(升级)。
    """
    prev = _ZS(12, 18, 10, 23, [(10, 18), (12, 20), (11, 19), (13, 23)])
    cur = _ZS(22, 30, 21, 32, [(21, 30), (22, 32), (23, 29)])
    assert classify_rel(prev, cur) == "expand"


def test_core_separated_but_waves_overlap_is_expand():
    prev = _ZS(12, 18, 10, 22, [(10, 18), (12, 20), (11, 19), (14, 22)])
    cur = _ZS(19, 24, 15, 25, [(15, 24), (19, 25), (16, 23)])   # 核心分离,波动重叠
    assert classify_rel(prev, cur) == "expand"


def test_core_overlap_is_expand_family():
    prev = _ZS(12, 18, 10, 20, [(10, 18), (12, 20), (11, 19)])
    cur = _ZS(15, 21, 13, 23, [(13, 21), (15, 23), (14, 20)])   # 核心重叠=延伸族
    assert classify_rel(prev, cur) == "expand"