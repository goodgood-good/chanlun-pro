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
    # zs_high/zs_low = 端点 max/min（中枢重叠判定的依据；classify_rel/包络都读它）
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


# ---- Task 2: calculate 状态机 ----
def test_calculate_empty_returns_empty():
    assert zslx_branch.ZslxBranchCalculator().calculate([], []) == []


def test_calculate_single_zhongshu_unfinished_consolidation():
    z = _zs_at(0, _seg(0, "up", 2, 5), 5, 8)
    wts = zslx_branch.ZslxBranchCalculator().calculate([z], [None])
    assert len(wts) == 1
    assert wts[0].zslx_type == "盘整" and wts[0].done is False   # 末个未完成


def test_calculate_uptrend_three_zhongshu_one_zslx():
    """3 个依次抬高的同向中枢 → 1 个上涨趋势(末个 done=False)。"""
    z1 = _zs_at(0, _seg(0, "up", 2, 5), 5, 8)
    z2 = _zs_at(10, _seg(10, "up", 8, 16), 16, 19)
    z3 = _zs_at(20, _seg(20, "up", 19, 27), 27, 30)
    wts = zslx_branch.ZslxBranchCalculator().calculate([z1, z2, z3], [None, None, None])
    assert len(wts) == 1
    assert wts[0].zslx_type == "上涨" and wts[0]._type == "up"
    assert wts[0].zss == [z1, z2, z3] and wts[0].done is False


def test_calculate_direction_break_splits_two_zslx():
    """上涨趋势(z1,z2) 后接下跌中枢 z3 → 方向断裂 → 切 2 个走势类型。"""
    z1 = _zs_at(0, _seg(0, "up", 2, 5), 5, 8)
    z2 = _zs_at(10, _seg(10, "up", 8, 16), 16, 19)
    z3 = _zs_at(20, _seg(20, "down", 16, 8), 5, 8)      # 本体跌回 [5,8] → trend_down vs cur_dir trend_up
    wts = zslx_branch.ZslxBranchCalculator().calculate([z1, z2, z3], [None, None, None])
    assert len(wts) == 2
    assert wts[0].zslx_type == "上涨" and wts[0].done is True and wts[0].zss == [z1, z2]
    assert wts[1].zslx_type == "盘整" and wts[1].done is False and wts[1].zss == [z3]


def test_calculate_expand_does_not_split():
    """两个本体相交(expand)的中枢——expand 不是方向反转 → 不切，并入同一走势类型。
    (原文第20课走势级别延续定理一：更大级别中枢产生前本级走势类型延续；升级留 P4b。)"""
    z1 = _zs_at(0, _seg(0, "up", 2, 5), 5, 8)
    z2 = _zs_at(10, _seg(10, "up", 6, 7), 6, 9)         # 本体[6,9] 与 z1[5,8] 相交 → expand
    wts = zslx_branch.ZslxBranchCalculator().calculate([z1, z2], [None, None])
    assert len(wts) == 1                                 # expand 不切
    assert wts[0].zss == [z1, z2]
    assert wts[0].zslx_type == "盘整"                    # 无趋势方向(仅扩张)→ 盘整


def test_calculate_expand_midtrend_continues():
    """上涨趋势中途出现 expand(中枢扩张)→ 不切断趋势，走势类型延续(走势级别延续定理一)。"""
    z1 = _zs_at(0, _seg(0, "up", 2, 5), 5, 8)
    z2 = _zs_at(10, _seg(10, "up", 8, 16), 16, 19)      # trend_up(本体分离、抬高)
    z3 = _zs_at(20, _seg(20, "up", 17, 18), 17, 20)     # 本体[17,20]与z2[16,19]相交→expand
    z4 = _zs_at(30, _seg(30, "up", 21, 28), 28, 31)     # trend_up(相对z3抬高)
    wts = zslx_branch.ZslxBranchCalculator().calculate([z1, z2, z3, z4], [None] * 4)
    assert len(wts) == 1                                 # expand 不切断上涨趋势
    assert wts[0].zslx_type == "上涨"
    assert wts[0].zss == [z1, z2, z3, z4]


def test_calculate_beichi_terminates_trend():
    """上涨趋势在 z3 离开段背驰(done_divergence[2].is_beichi) → 走势类型在 z3 终结。"""
    z1 = _zs_at(0, _seg(0, "up", 2, 5), 5, 8)
    z2 = _zs_at(10, _seg(10, "up", 8, 16), 16, 19)
    z3 = _zs_at(20, _seg(20, "up", 19, 27), 27, 30)
    z4 = _zs_at(30, _seg(30, "up", 30, 38), 38, 41)
    dv = [None, None, _dv(True), None]                   # z3 处背驰
    wts = zslx_branch.ZslxBranchCalculator().calculate([z1, z2, z3, z4], dv)
    assert len(wts) == 2
    assert wts[0].zss == [z1, z2, z3] and wts[0].done is True   # 背驰终结
    assert wts[1].zss == [z4] and wts[1].done is False          # z4 另起
    assert wts[1].zslx_type == "盘整"          # 背驰后新起的单中枢 = 盘整
