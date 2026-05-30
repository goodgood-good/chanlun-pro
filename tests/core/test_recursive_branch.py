"""tests/core/test_recursive_branch.py — P4b 递归装配 TDD。

自带 helper（受控 ZS/ZSLX/线段，绕开笔划分浮点敏感）。
"""
from __future__ import annotations

from chanlun.core.cl_interface import CLKline, FX, XD, ZS, ZSLX
from chanlun.core import recursive_branch


def _seg(index, _type, start_val, end_val) -> XD:
    def _fx(kidx, val, ftype):
        k = CLKline(k_index=kidx, date=None, h=val, l=val, o=val, c=val, a=0.0, klines=[])
        return FX(_type=ftype, k=k, klines=[k], val=val)
    if _type == "up":
        start, end = _fx(index, start_val, "di"), _fx(index + 1, end_val, "ding")
    else:
        start, end = _fx(index, start_val, "ding"), _fx(index + 1, end_val, "di")
    xd = XD(start=start, end=end, _type=_type, index=index)
    xd.done = True
    xd.zs_high = max(start_val, end_val)
    xd.zs_low = min(start_val, end_val)
    return xd


def _make_zs(core_segs, zd, zg) -> ZS:
    z = ZS(zs_type="xd", start=None)
    z.lines = list(core_segs)
    z.zd, z.zg = zd, zg
    z._bounds_dirty = True
    z.update_boundaries()
    return z


def _make_zslx(zss, zslx_type, index=99) -> ZSLX:
    z = ZSLX(zslx_level=None, start=zss[0].lines[0].start, end=zss[-1].lines[-1].end,
             start_line=zss[0].lines[0], end_line=zss[-1].lines[-1],
             _type="up", index=index, done=True)
    z.zss = list(zss)
    z.zslx_type = zslx_type
    z.zs_high = max(x.gg for x in zss)
    z.zs_low = min(x.dd for x in zss)
    return z


# ---- _as_units: ZSLX index 重排 ----
def test_as_units_reindexes_and_preserves_original():
    z1 = _make_zslx([_make_zs([_seg(0, "up", 5, 8), _seg(1, "down", 8, 5), _seg(2, "up", 5, 8)], 5, 8)], "盘整", index=5)
    z2 = _make_zslx([_make_zs([_seg(3, "up", 16, 19), _seg(4, "down", 19, 16), _seg(5, "up", 16, 19)], 16, 19)], "盘整", index=3)
    out = recursive_branch._as_units([z1, z2])
    assert [u.index for u in out] == [0, 1]      # 任意输入 index(5,3) → 拷贝重排为 0,1
    assert z1.index == 5 and z2.index == 3       # 原对象 index 不变(浅拷贝隔离)
    assert out[0].zs_high == z1.zs_high          # 包络沿用


# ---- _mark_upgrades: 9段 / expand 标注 ----
def test_mark_upgrades_nine_segments():
    # 9 段中枢 → 升级标注
    nine = [_seg(i, "up" if i % 2 == 0 else "down", 5, 8) for i in range(9)]
    z = _make_zs(nine, 5, 8)
    assert recursive_branch._mark_upgrades([z]) == [0]


def test_mark_upgrades_expand_pair():
    # 两中枢本体相交(expand) → 后者标升级
    z1 = _make_zs([_seg(0, "down", 8, 5), _seg(1, "up", 5, 8), _seg(2, "down", 8, 5)], 5, 8)
    z2 = _make_zs([_seg(3, "down", 9, 6), _seg(4, "up", 6, 9), _seg(5, "down", 9, 6)], 6, 9)  # body[6,9]∩[5,8]
    assert recursive_branch._mark_upgrades([z1, z2]) == [1]


def test_mark_upgrades_clean_trend_none():
    # 干净趋势(本体分离)→ 无升级标注
    z1 = _make_zs([_seg(0, "down", 8, 5), _seg(1, "up", 5, 8), _seg(2, "down", 8, 5)], 5, 8)
    z2 = _make_zs([_seg(3, "down", 19, 16), _seg(4, "up", 16, 19), _seg(5, "down", 19, 16)], 16, 19)
    assert recursive_branch._mark_upgrades([z1, z2]) == []


def test_mark_upgrades_nine_priority_over_expand():
    """一个中枢既 9段又与前驱 expand → 只记一次(走 9段分支)。"""
    z1 = _make_zs([_seg(0, "down", 8, 5), _seg(1, "up", 5, 8), _seg(2, "down", 8, 5)], 5, 8)
    nine = [_seg(i + 3, "up" if i % 2 == 0 else "down", 6, 9) for i in range(9)]  # body[6,9]∩z1[5,8] 且 9段
    z2 = _make_zs(nine, 6, 9)
    assert recursive_branch._mark_upgrades([z1, z2]) == [1]   # z2 记一次(9段优先于 expand)
