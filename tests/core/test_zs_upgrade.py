"""tests/core/test_zs_upgrade.py — P9 中枢升级·扩展(line4898 3段重合)。

513100 真实 QMT 数据(z1+z2 涉及线段 xd6-15)当 oracle: 用户标注 下xd7-9/上xd10-12/盘xd13-15,
区间 [1.713,1.737](用户多轮确认的正确值)。
"""
from chanlun.core.cl_interface import ZS
from chanlun.core.zs_upgrade import is_kuozhan, kuozhan_zhongshu, three_segment_interval


class _L:
    """最小线段桩：type / zs_low / zs_high。"""

    def __init__(self, t, lo, hi):
        self.type, self.zs_low, self.zs_high = t, lo, hi


def _xds_513100():
    """513100 z1+z2 涉及线段 xd6-15(QMT 真实数据,(start_val→end_val) 转 zs_low/zs_high)。"""
    return [
        _L("up", 1.664, 1.737),    # xd6
        _L("down", 1.689, 1.737),  # xd7
        _L("up", 1.689, 1.711),    # xd8
        _L("down", 1.666, 1.711),  # xd9  ← 底
        _L("up", 1.666, 1.693),    # xd10
        _L("down", 1.672, 1.693),  # xd11
        _L("up", 1.672, 1.756),    # xd12 ← 顶
        _L("down", 1.713, 1.756),  # xd13
        _L("up", 1.713, 1.742),    # xd14
        _L("down", 1.729, 1.742),  # xd15
    ]


def test_three_segment_interval_513100():
    """513100 扩展: 底=xd9 顶=xd12 切 3 段(xd6-9/xd10-12/xd13-15) → [1.713,1.737]。"""
    res = three_segment_interval(_xds_513100())
    assert res is not None
    zd, zg = res
    assert round(zd, 4) == 1.7130
    assert round(zg, 4) == 1.7370


def test_three_segment_degenerate_returns_none():
    """3 段无共同重合(后段低点高过前段高点) → 退化 None。"""
    lines = [
        _L("up", 1.00, 1.10), _L("down", 1.05, 1.10), _L("up", 1.05, 1.20),
        _L("down", 1.15, 1.20), _L("up", 1.15, 1.30), _L("down", 1.25, 1.30),
    ]  # 底=idx1(1.05) 顶=idx4(1.30): 段[1.0,1.1]/[1.05,1.3]/[1.25,1.3] → zd=1.25>=zg=1.1
    assert three_segment_interval(lines) is None


def test_three_segment_too_few_lines_none():
    assert three_segment_interval([_L("up", 1, 2), _L("down", 1, 2)]) is None


def _zs(zd, zg, dd, gg, done=True):
    z = ZS(zs_type="xd", start=None)
    z.zd, z.zg, z.dd, z.gg, z.done = zd, zg, dd, gg, done
    return z


def test_is_kuozhan_513100_z1_z2_true():
    """z1[核1.689-1.711 包1.664-1.737] + z2[核1.729-1.742 包1.713-1.756]:
    包络重叠[1.713,1.737] + 核心区分离(1.711<1.729) = 扩展。"""
    z1 = _zs(1.689, 1.711, 1.664, 1.737)
    z2 = _zs(1.729, 1.742, 1.713, 1.756)
    assert is_kuozhan(z1, z2) is True


def test_is_kuozhan_trend_false():
    """包络分离(后中枢整体在上) = 趋势, 非扩展。"""
    z1 = _zs(1.689, 1.711, 1.664, 1.737)
    z2 = _zs(2.00, 2.10, 1.90, 2.20)
    assert is_kuozhan(z1, z2) is False


def test_kuozhan_zhongshu_513100_z1_z2():
    """z1(线段 xd6-11)+z2(线段 xd13-15)成组 → 1 个扩展中枢 [1.713,1.737]。"""
    xds = _xds_513100()                  # xd6..xd15
    z1 = _zs(1.689, 1.711, 1.664, 1.737)
    z1.lines = xds[0:6]                  # xd6-11
    z2 = _zs(1.729, 1.742, 1.713, 1.756)
    z2.lines = xds[7:10]                 # xd13-15
    out = kuozhan_zhongshu([z1, z2], xds)
    assert len(out) == 1
    assert round(out[0].zd, 4) == 1.7130 and round(out[0].zg, 4) == 1.7370
    assert out[0].expanded_with == [z1, z2]
    assert out[0].dd <= out[0].zd and out[0].zg <= out[0].gg    # 核心区 ⊂ 包络


def test_kuozhan_zhongshu_trend_no_group():
    """相邻中枢是趋势(包络分离)→ 不成组、无扩展中枢。"""
    xds = _xds_513100()
    z1 = _zs(1.689, 1.711, 1.664, 1.737)
    z1.lines = xds[0:6]
    z2 = _zs(2.00, 2.10, 1.90, 2.20)     # 整体在上 = 趋势
    z2.lines = xds[7:10]
    assert kuozhan_zhongshu([z1, z2], xds) == []
