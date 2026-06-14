"""§3 中枢关系（中心定理二）测试。★历史"全图3层皆错"高危区。

口径铁律：用**瞬间波动区间 [DD,GG]**（严格档 GD），非中枢区间 [ZD,ZG]；
相邻中枢 [DD,GG] 相交（含相切）= 升级（非趋势）。
"""

from chan.types import ChanStatus, Direction, Zhongshu
from chan.upgrade import is_upgrade, qs_relation

U, D = Direction.UP, Direction.DOWN


def _zs(direction, zd, zg, dd, gg, cb=1):
    return Zhongshu(direction=direction, start_seg=0, end_seg=2, zg=zg, zd=zd, gg=gg, dd=dd,
                    confirm_bar=cb, status=ChanStatus.CONFIRMED, closed=True)


# ── 趋势延续：后中枢整体在前之上/下（[DD,GG] 不相交）──
def test_trend_up_down():
    a = _zs(U, 10, 12, 9, 13)
    assert qs_relation(a, _zs(U, 15, 17, 14, 18)) is Direction.UP    # 后DD14>前GG13
    assert qs_relation(a, _zs(D, 4, 6, 3, 7)) is Direction.DOWN      # 后GG7<前DD9
    assert not is_upgrade(a, _zs(U, 15, 17, 14, 18))


# ── 升级：[DD,GG] 相交 → 非趋势（None）──
def test_upgrade_overlap():
    a = _zs(U, 10, 12, 9, 13)
    b = _zs(U, 11, 13, 10, 14)   # [10,14] 与 [9,13] 相交
    assert qs_relation(a, b) is None
    assert is_upgrade(a, b)


# ── 相切也算升级（后DD=前GG，課54:285"哪怕一个价位"）──
def test_touch_is_upgrade():
    a = _zs(U, 10, 12, 9, 13)
    b = _zs(U, 14, 16, 13, 17)   # 后DD=13=前GG
    assert qs_relation(a, b) is None and is_upgrade(a, b)


# ── 口径校验：用 GG/DD 而非 ZG/ZD（防历史 GD/ZGD 口径 bug）──
def test_uses_gg_dd_not_zg_zd():
    # 两中枢"中枢区间 [ZD,ZG] 不相交"但"瞬间波动 [DD,GG] 相交" → 严格档=升级(None)
    a = _zs(U, 10, 12, 8, 14)    # ZG=12, GG=14
    b = _zs(U, 13, 15, 11, 17)   # ZD=13>前ZG12(中枢区间不交)；DD=11<前GG14(波动相交)
    # 若误用 ZG/ZD：前ZG12<后ZD13 → 会判 UP(趋势)；正确(GG/DD)：前GG14>后DD11 → 升级 None
    assert qs_relation(a, b) is None
