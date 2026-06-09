"""tests/core/test_zslx_branch.py — P4a 走势类型划分 TDD。

自带 _seg/_make_zs/_dv helper（自包含，不依赖其它 test 文件）。受控 ZS 序列喂入，
确定性复现走势类型边界（绕开笔划分浮点敏感——输入即确定性中枢）。
"""
from __future__ import annotations

from chanlun.core.cl_interface import CLKline, FX, XD, ZS
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


def test_calculate_swing_reversal_through_overlap_splits():
    """反转处中枢重叠 → classify_rel 失明(返回 expand,看不见反转),本体摆动靠『本体分离』切。
    上涨 z1,z2,z3(峰本体[27,30]) 后 z4[25,28] 与峰重叠(expand,classify_rel 不切),
    z5[15,18] 本体跌穿峰本体下沿(gg=18<峰 dd=27) → 在峰 z3 确认反转(line24736 第二段[z5]确认)。
    边界落峰 z3 → z4 归入下跌段(非上涨)。这是旧 classify_rel 算法看不见的反转(原文
    line24727 本体分离=中枢关系;line30931 升级把次级别当线段、高低点=端点)。"""
    z1 = _zs_at(0, _seg(0, "up", 2, 5), 5, 8)
    z2 = _zs_at(10, _seg(10, "up", 8, 16), 16, 19)
    z3 = _zs_at(20, _seg(20, "up", 19, 27), 27, 30)          # 峰,本体[27,30]
    z4 = _zs_at(30, _seg(30, "up", 24, 25), 25, 28)          # 本体[25,28]∩z3[27,30] → expand
    z5 = _zs_at(40, _seg(40, "down", 19, 18), 15, 18)        # 本体[15,18],gg=18 < 峰 dd=27 → 反转
    wts = zslx_branch.ZslxBranchCalculator().calculate([z1, z2, z3, z4, z5], [None] * 5)
    assert len(wts) == 2
    assert wts[0].zslx_type == "上涨" and wts[0].zss == [z1, z2, z3]   # 边界落峰 z3,z4 不并入
    assert wts[1].zslx_type == "下跌" and wts[1].zss == [z4, z5]       # z4 归下跌段
    assert wts[0].done is True and wts[1].done is False


def test_calculate_subsplit_trend_with_internal_consolidation():
    """同级别分解(原文 line24735 不延伸/允许盘整+盘整;line24727 3段重合=中枢;line24728
    延伸成6段=2盘整;line30927 选最优=中枢震荡最清晰):一个上涨趋势中段若含『连续重叠中枢
    (≥3 个本体相交=同级别中枢=盘整震荡)』→ 拆成 上涨+盘整+上涨,暴露内部中枢震荡。
    z1,z2 抬高(上涨腿) → z3,z4,z5 同本体重叠(盘整中枢) → z6,z7 再抬高(上涨腿)。"""
    z1 = _zs_at(0, _seg(0, "up", 1, 4), 2, 5)
    z2 = _zs_at(10, _seg(10, "up", 5, 9), 8, 11)            # trend_up
    z3 = _zs_at(20, _seg(20, "up", 11, 13), 12, 15)         # trend_up(进盘整)
    z4 = _zs_at(30, _seg(30, "up", 13, 14), 12, 15)         # 本体[12,15]∩z3 → expand
    z5 = _zs_at(40, _seg(40, "down", 15, 13), 12, 15)       # 本体[12,15]∩z4 → expand(≥2连续=盘整)
    z6 = _zs_at(50, _seg(50, "up", 16, 21), 20, 23)         # trend_up(出盘整,再抬高)
    z7 = _zs_at(60, _seg(60, "up", 23, 29), 28, 31)         # trend_up
    wts = zslx_branch.ZslxBranchCalculator().calculate([z1, z2, z3, z4, z5, z6, z7], [None] * 7)
    assert [w.zslx_type for w in wts] == ["上涨", "盘整", "上涨"]
    assert wts[0].zss == [z1, z2] and wts[1].zss == [z3, z4, z5] and wts[2].zss == [z6, z7]
    assert wts[2].done is False and wts[0].done is True       # 仅末段未完成


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


def test_calculate_midtrend_beichi_new_high_continues():
    """中途背驰但其后创新高 → 趋势延续不切(原文 line20108:背驰后反弹创新极值则趋势延续;
    line22547 趋势靠背驰『转折』终结=须价格真反转)。z3 离开段背驰,但 z4 本体再抬高(创新高)
    → 本体摆动无反转 → 一个上涨走势类型。"""
    z1 = _zs_at(0, _seg(0, "up", 2, 5), 5, 8)
    z2 = _zs_at(10, _seg(10, "up", 8, 16), 16, 19)
    z3 = _zs_at(20, _seg(20, "up", 19, 27), 27, 30)
    z4 = _zs_at(30, _seg(30, "up", 30, 38), 38, 41)
    dv = [None, None, _dv(True), None]                   # z3 处背驰,但 z4 创新高(本体抬高)
    wts = zslx_branch.ZslxBranchCalculator().calculate([z1, z2, z3, z4], dv)
    assert len(wts) == 1                                  # 不切,上涨趋势延续
    assert wts[0].zslx_type == "上涨"
    assert wts[0].zss == [z1, z2, z3, z4] and wts[0].done is False


def test_calculate_continuous_lower_lows_is_one_downtrend():
    """连续下台阶(中枢本体依次创新低、无反转)= 一个下跌走势类型。本体摆动不在中途背驰处
    切——价格没反转、继续创新低(line20108:背驰后仍创新极值则趋势延续);只有本体分离反转
    才切(line22547 趋势靠背驰『转折』终结=须价格真反转;line7264 连续走势类型必不同类型,
    故同向下跌不可能是多个独立完成走势类型)。"""
    # 5 个依次下台阶(本体分离 trend_down)的中枢, z2 处 qs 背驰(底背驰但下跌继续)
    z0 = _zs_at(0, _seg(0, "down", 46, 43), 40, 43)
    z1 = _zs_at(10, _seg(10, "down", 43, 33), 30, 33)
    z2 = _zs_at(20, _seg(20, "down", 33, 23), 20, 23)
    z3 = _zs_at(30, _seg(30, "down", 23, 13), 10, 13)
    z4 = _zs_at(40, _seg(40, "down", 13, 3), 0, 3)
    dv = [None, None, _dv(True, "qs"), None, None]    # z2 qs 背驰切出 下跌|下跌
    wts = zslx_branch.ZslxBranchCalculator().calculate([z0, z1, z2, z3, z4], dv)
    assert len(wts) == 1                              # 合并:连续同向下跌 = 一个扩展走势类型
    assert wts[0].zslx_type == "下跌" and wts[0]._type == "down"
    assert wts[0].zss == [z0, z1, z2, z3, z4]         # 5 个中枢全并入
    assert wts[0].done is False                       # 末走势类型未完成
    assert wts[0].zs_low == min(z.dd for z in [z0, z1, z2, z3, z4])   # 包络覆盖全段
    assert wts[0].zs_high == max(z.gg for z in [z0, z1, z2, z3, z4])


def test_calculate_trailing_expand_absorbed_into_trend():
    """下跌趋势末尾的 expand 中枢(本体相交、未反转)并入该下跌走势类型。本体摆动只在
    『本体分离反转』处切边界,扩张中枢无反转故不另起(原文 line24140 多义性:已完成走势有
    多种合法分解,本体摆动选『摆动反转』为界;line21637 中枢扩展属盘整=仍在本走势类型内)。"""
    z1 = _zs_at(0, _seg(0, "down", 20, 17), 14, 17)
    z2 = _zs_at(10, _seg(10, "down", 17, 11), 8, 11)         # trend_down vs z1 → 下跌
    z3 = _zs_at(20, _seg(20, "down", 11, 9), 8, 11)          # 本体[8,11]∩z2[8,11] → expand,未反转
    dv = [None, _dv(True, "qs"), None]
    wts = zslx_branch.ZslxBranchCalculator().calculate([z1, z2, z3], dv)
    assert len(wts) == 1                                     # 无本体分离反转 → 一个下跌走势类型
    assert wts[0].zslx_type == "下跌" and wts[0].zss == [z1, z2, z3]


def test_calculate_pz_beichi_does_not_terminate():
    """盘整背驰(pz)是中枢震荡内的力度衰减、非走势类型边界 → 不切；只趋势背驰(qs)才切
    (原文 7260/22415:走势类型边界=趋势完成=趋势背驰)。"""
    z1 = _zs_at(0, _seg(0, "up", 2, 5), 5, 8)
    z2 = _zs_at(10, _seg(10, "up", 8, 16), 16, 19)
    z3 = _zs_at(20, _seg(20, "up", 19, 27), 27, 30)
    z4 = _zs_at(30, _seg(30, "up", 30, 38), 38, 41)
    dv = [None, None, _dv(True, "pz"), None]             # z3 处盘整背驰(pz)
    wts = zslx_branch.ZslxBranchCalculator().calculate([z1, z2, z3, z4], dv)
    assert len(wts) == 1                                 # pz 不切,上涨趋势延续
    assert wts[0].zss == [z1, z2, z3, z4]
    assert wts[0].zslx_type == "上涨"
