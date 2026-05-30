"""tests/core/test_zslx_branch.py — P4a 走势类型划分 TDD。

自带 _seg/_make_zs/_dv helper（自包含，不依赖其它 test 文件）。受控 ZS 序列喂入，
确定性复现走势类型边界（绕开笔划分浮点敏感——输入即确定性中枢）。
"""
from __future__ import annotations

from chanlun.core.cl_interface import CLKline, FX, XD, ZS, ZSLX
from chanlun.core import zslx_branch
from chanlun.core.zs_branch import DivergenceResult


def _seg(index: int, _type: str, start_val: float, end_val: float) -> XD:
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


def _make_zs(start_seg, core_segs, zd, zg) -> ZS:
    z = ZS(zs_type="xd", start=start_seg)
    z.lines = list(core_segs)
    z.zd, z.zg = zd, zg
    z._bounds_dirty = True
    z.update_boundaries()
    return z


def _dv(is_beichi: bool, kind: str = "qs") -> DivergenceResult:
    s = _seg(0, "up", 1, 2)
    return DivergenceResult(is_beichi=is_beichi, kind=kind, compare_seg=s, leave_seg=s, provisional=False)


# 一个本体在 [lo,hi] 的标准中枢（进入段 + 3 段核心震荡）
def _zs_at(base_idx, entry, lo, hi):
    mid = (lo + hi) / 2
    core = [_seg(base_idx + 1, "down", hi, lo), _seg(base_idx + 2, "up", lo, hi),
            _seg(base_idx + 3, "down", hi, lo)]
    return _make_zs(entry, core, lo, hi)


# ---- Task 1: _finalize ----
def test_finalize_single_zhongshu_is_consolidation():
    z = _zs_at(0, _seg(0, "up", 2, 5), 5, 8)
    zslx = zslx_branch.ZslxBranchCalculator._finalize([z], 0, None, done=False)
    assert zslx.zslx_type == "盘整"
    assert zslx.zss == [z]
    assert zslx.done is False
    assert zslx.zs_high == z.gg and zslx.zs_low == z.dd      # 单中枢包络
    assert zslx.start_line is z.start                         # 进入段 a


def test_finalize_uptrend_two_zhongshu():
    z1 = _zs_at(0, _seg(0, "up", 2, 5), 5, 8)
    z2 = _zs_at(10, _seg(10, "up", 8, 16), 16, 19)
    zslx = zslx_branch.ZslxBranchCalculator._finalize([z1, z2], 0, "trend_up", done=True)
    assert zslx.zslx_type == "上涨" and zslx._type == "up"
    assert zslx.zs_high == max(z1.gg, z2.gg)
    assert zslx.zs_low == min(z1.dd, z2.dd)
    assert zslx.start_line is z1.start                        # 第一中枢进入段
    assert zslx.end_line is z2.lines[-1]                      # 末中枢末段(z.end 缺→fallback)
