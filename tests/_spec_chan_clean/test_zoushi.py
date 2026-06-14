"""§1.5/§4.1 走势类型划分 测试（形态学：中枢关系驱动；背驰=Phase2d 后叠加）。"""

from chan.config import DEFAULT
from chan.types import ChanStatus, Direction, Zhongshu, ZoushiKind
from chan.zoushi import find_zoushi

U, D = Direction.UP, Direction.DOWN


def _zs(direction, zd, zg, dd, gg, cb, ss):
    return Zhongshu(direction=direction, start_seg=ss, end_seg=ss + 2, zg=zg, zd=zd, gg=gg, dd=dd,
                    confirm_bar=cb, status=ChanStatus.CONFIRMED, closed=True)


# ── 上涨趋势：≥2 依次抬高的同向中枢 = 一个上涨走势类型 ──
def test_uptrend_is_one_zoushi():
    zss = [_zs(U, 10, 12, 9, 13, 1, 0), _zs(U, 15, 17, 14, 18, 2, 3), _zs(U, 20, 22, 19, 23, 3, 6)]
    out = find_zoushi(zss, DEFAULT)
    assert len(out) == 1
    z = out[0]
    assert z.kind is ZoushiKind.UP and z.direction is U
    assert (z.low, z.high) == (9, 23)         # 走势区间 = min(dd)~max(gg)
    assert z.status is ChanStatus.PENDING      # 末个在建（无边界中枢）


# ── 下跌趋势 ──
def test_downtrend():
    zss = [_zs(D, 18, 20, 17, 21, 1, 0), _zs(D, 12, 14, 11, 15, 2, 3), _zs(D, 6, 8, 5, 9, 3, 6)]
    out = find_zoushi(zss, DEFAULT)
    assert len(out) == 1 and out[0].kind is ZoushiKind.DOWN and out[0].direction is D


# ── 趋势方向断裂 → 切成两个走势类型 ──
def test_trend_break_splits():
    # 两个抬高的(上涨)中枢 + 第三个掉到下方(下跌关系) → 上涨段在第3中枢处确认结束
    zss = [_zs(U, 10, 12, 9, 13, 1, 0), _zs(U, 15, 17, 14, 18, 2, 3), _zs(D, 3, 5, 2, 6, 3, 6)]
    out = find_zoushi(zss, DEFAULT)
    assert len(out) == 2
    assert out[0].kind is ZoushiKind.UP and out[0].status is ChanStatus.CONFIRMED
    assert out[0].confirm_bar == 3            # 由触发断裂的第3中枢确认
    assert out[1].status is ChanStatus.PENDING


# ── 盘整：单中枢；与下一重叠中枢断裂 ──
def test_panzheng_and_overlap_break():
    z0 = _zs(U, 10, 12, 9, 13, 1, 0)
    z_ov = _zs(U, 11, 13, 10, 14, 5, 3)       # 与 z0 [DD,GG] 相交 → 非趋势 → 断裂
    out = find_zoushi([z0, z_ov], DEFAULT)
    assert len(out) == 2
    assert out[0].kind is ZoushiKind.PANZHENG and out[0].status is ChanStatus.CONFIRMED
    assert out[1].kind is ZoushiKind.PANZHENG and out[1].status is ChanStatus.PENDING


# ── 空 / 单中枢 ──
def test_empty_and_single():
    assert find_zoushi([], DEFAULT) == []
    out = find_zoushi([_zs(U, 10, 12, 9, 13, 1, 0)], DEFAULT)
    assert len(out) == 1 and out[0].kind is ZoushiKind.PANZHENG
    assert out[0].status is ChanStatus.PENDING


# ── ★ 截断稳定（C1）：已确认走势类型前缀不随未来漂移 ──
def test_truncation_stable():
    seq = [_zs(U, 10, 12, 9, 13, 1, 0), _zs(U, 15, 17, 14, 18, 2, 3), _zs(D, 3, 5, 2, 6, 3, 6),
           _zs(D, 1, 2, 0, 2.5, 4, 9), _zs(U, 8, 10, 7, 11, 5, 12)]
    full = [z for z in find_zoushi(seq, DEFAULT) if z.status is ChanStatus.CONFIRMED]

    def sig(z):
        return (z.start_unit, z.end_unit, z.kind.value, int(z.direction), z.confirm_bar)

    for t in range(1, 7):
        trunc = [z for z in find_zoushi([x for x in seq if x.confirm_bar <= t], DEFAULT)
                 if z.status is ChanStatus.CONFIRMED]
        exp = [z for z in full if z.confirm_bar <= t]
        assert [sig(z) for z in trunc] == [sig(z) for z in exp], f"t={t}: 走势类型截断漂移"
