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
    """513100 扩展: 摆动分段(进入段xd6 + 下xd7-9/上xd10-12/盘xd13-15) → [1.713,1.737]。"""
    res = three_segment_interval(_xds_513100())
    assert res is not None
    zd, zg = res
    assert round(zd, 4) == 1.7130
    assert round(zg, 4) == 1.7370


def _xds_301004_z9z11():
    """301004 z9-11 区域线段 xd63-76(QMT 真实数据)。前 3 段走势:
    下(到xd66=38.00)/上(到xd69=41.78)/下(到xd74=39.01,xd75创更高高点41.14打断)。"""
    vals = [
        ("up", 41.380, 41.730), ("down", 40.100, 41.730), ("up", 40.100, 41.680),
        ("down", 38.000, 41.680), ("up", 38.000, 41.590), ("down", 39.530, 41.590),
        ("up", 39.530, 41.780), ("down", 41.000, 41.780), ("up", 41.000, 41.620),
        ("down", 40.400, 41.620), ("up", 40.400, 40.700), ("down", 39.010, 40.700),
        ("up", 39.010, 41.140), ("down", 39.730, 41.140),
    ]
    return [_L(t, lo, hi) for t, lo, hi in vals]


def test_three_segment_interval_301004_z9z11():
    """301004 z9-11 扩展: 第3段下跌走势在 xd74=39.01 被 xd75 更高高点打断
    → 中枢 [39.01, 41.73](用户图形确认),不再被全段最低 38.06 撑宽。"""
    res = three_segment_interval(_xds_301004_z9z11())
    assert res is not None
    zd, zg = res
    assert round(zd, 4) == 39.0100
    assert round(zg, 4) == 41.7300


def test_three_segment_uptrend_too_few_swings_none():
    """单边上涨只切出 1 段上涨走势, 不足「进入段 + 3 走势」→ None。"""
    lines = [
        _L("up", 1.00, 1.10), _L("down", 1.05, 1.10), _L("up", 1.05, 1.20),
        _L("down", 1.15, 1.20), _L("up", 1.15, 1.30), _L("down", 1.25, 1.30),
    ]
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
