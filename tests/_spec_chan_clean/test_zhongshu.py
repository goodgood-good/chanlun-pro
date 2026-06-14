"""§2 段中枢 测试。

fixture 用**直接构造的线段序列**（A-21 真值由定义推导）：
四指标 ZG/ZD/GG/DD / 延伸 / 离开段封口 / 截断身份稳定 / 段中枢（非笔中枢）。
"""

from chan.config import DEFAULT
from chan.types import ChanStatus, Direction, Duan
from chan.zhongshu import find_zhongshus, find_zhongshus_from_bars

U, D = Direction.UP, Direction.DOWN


def _dn(direction, low, high, confirm_bar):
    sv, ev = (low, high) if direction is U else (high, low)
    return Duan(direction=direction, start_bi=0, end_bi=0, start_value=sv, end_value=ev,
                high=high, low=low, confirm_bar=confirm_bar, status=ChanStatus.CONFIRMED)


def _ident(z):
    return (z.start_seg, z.zd, z.zg, int(z.direction), z.confirm_bar)


# ── 基础段中枢：四指标按定义（課20:63-65）──
def test_basic_zhongshu_indicators():
    # 同向段=seg0,seg2(up)；zg=min(15,14)=14, zd=max(10,11)=11；gg=max(15,14)=15, dd=min(10,11)=10
    segs = [_dn(U, 10, 15, 1), _dn(D, 11, 15, 2), _dn(U, 11, 14, 3), _dn(D, 8, 10, 4)]  # seg3 离开下方
    zs = find_zhongshus(segs, DEFAULT)
    assert len(zs) == 1
    z = zs[0]
    assert (z.zd, z.zg) == (11, 14)
    assert (z.dd, z.gg) == (10, 15)
    assert z.direction is U and (z.start_seg, z.end_seg) == (0, 2)
    assert z.confirm_bar == 3              # 前 3 段确认（課18:19）
    assert z.closed is True                # seg3 离开 → 封口


# ── 延伸（中心定理一）+ 离开段封口；瞬间波动区间随同向段更新 ──
def test_extension_and_closing():
    segs = [
        _dn(U, 10, 15, 1), _dn(D, 11, 15, 2), _dn(U, 11, 14, 3),  # 基础中枢 [11,14]
        _dn(D, 12, 14, 4), _dn(U, 12, 16, 5),                      # 延伸（同向段 seg4 抬 gg→16）
        _dn(D, 5, 9, 6),                                            # 离开段（整体<11）封口
    ]
    z = find_zhongshus(segs, DEFAULT)[0]
    assert (z.zd, z.zg) == (11, 14)        # 中枢区间延伸期不变
    assert z.gg == 16 and z.dd == 10       # 瞬间波动区间随同向段延伸
    assert z.end_seg == 4 and z.seg_count == 5 and z.closed is True


# ── 三段不重合 → 无中枢 ──
def test_no_overlap_no_zhongshu():
    segs = [_dn(U, 10, 12, 1), _dn(D, 12, 14, 2), _dn(U, 15, 20, 3), _dn(D, 18, 22, 4)]
    assert find_zhongshus(segs, DEFAULT) == []


# ── 开放中枢（末个、未封口）阻断前瞻下一中枢（C1）──
def test_open_zhongshu_is_last():
    # 基础中枢后段段重合、无离开段 → 开放，不应再开下一中枢
    segs = [_dn(U, 10, 15, 1), _dn(D, 11, 15, 2), _dn(U, 11, 14, 3),
            _dn(D, 12, 14, 4), _dn(U, 12, 13, 5), _dn(D, 11.5, 13, 6)]
    zs = find_zhongshus(segs, DEFAULT)
    assert len(zs) == 1 and zs[0].closed is False


# ── ★ 截断身份稳定（C1）：中枢存在+[zd,zg] 不随未来漂移 ──
def test_truncation_identity_stable():
    segs = [
        _dn(U, 10, 15, 1), _dn(D, 11, 15, 2), _dn(U, 11, 14, 3), _dn(D, 12, 14, 4),
        _dn(U, 12, 13, 5), _dn(D, 5, 9, 6),  # 中枢1 封口
        _dn(U, 5, 8, 7), _dn(D, 6, 8, 8), _dn(U, 6, 7.5, 9), _dn(D, 4, 6, 10),  # 中枢2
    ]
    full = find_zhongshus(segs, DEFAULT)
    for t in range(1, 11):
        trunc = find_zhongshus([s for s in segs if s.confirm_bar <= t], DEFAULT)
        exp = [z for z in full if z.confirm_bar <= t]
        assert [_ident(z) for z in trunc] == [_ident(z) for z in exp], f"t={t}: 中枢身份漂移"


# ── 課33 封顶：长震荡不被贪吸成巨型中枢（≤8 段=3核心+5延伸）──
def test_max_segs_cap():
    vals = [(U, 11, 14), (D, 11.5, 14), (U, 11.5, 14.5), (D, 11, 14.5), (U, 11, 14),
            (D, 11.5, 14), (U, 11.5, 14.5), (D, 11, 14.5), (U, 11, 14), (D, 11.5, 14),
            (U, 11.5, 14.5)]  # 11 段全重合 [11.5,14]
    segs = [_dn(d, lo, hi, i + 1) for i, (d, lo, hi) in enumerate(vals)]
    zs = find_zhongshus(segs, DEFAULT)
    assert zs[0].seg_count == 8 and zs[0].closed is True   # 封顶在 8 段
    assert zs[0].end_seg == 7
    # 封顶后从末段之后续接，不会吞成一个 11 段巨型中枢
    assert all(z.seg_count <= DEFAULT.zs_max_segs for z in zs)


# ── 不足三段 → 空 ──
def test_too_few_segments():
    assert find_zhongshus([_dn(U, 10, 15, 1), _dn(D, 11, 15, 2)], DEFAULT) == []


# ── 端到端 smoke：bar → … → 段中枢 ──
def test_e2e_smoke():
    turns = [("D", 10, 1), ("T", 13, 5), ("D", 11, 5), ("T", 14, 5), ("D", 11, 5),
             ("T", 13, 5), ("D", 10, 5), ("T", 13, 5), ("D", 11, 5), ("T", 14, 5),
             ("D", 5, 5), ("T", 8, 5)]
    centers = [turns[0][1]]
    cur = turns[0][1]
    from chan.types import Bar
    for _k, level, ln in turns[1:]:
        step = (level - cur) / ln
        for _ in range(ln):
            cur += step
            centers.append(round(cur, 4))
        cur = level
    bars = [Bar(idx=i, dt=None, o=x - 0.5, h=x + 0.5, l=x - 0.5, c=x + 0.5)
            for i, x in enumerate(centers)]
    zs = find_zhongshus_from_bars(bars)
    # 至少识别出一个段中枢，且 zd<=zg 自洽
    assert all(z.zd <= z.zg for z in zs)
