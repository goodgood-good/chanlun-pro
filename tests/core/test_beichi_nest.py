"""tests/core/test_beichi_nest.py — P4c 背驰贯通 TDD。

受控 fake DivergenceResult（leave_seg 用受控 K 线序号区间）+ fake LevelResult，
绕开笔划分浮点敏感。
"""
from __future__ import annotations

from chanlun.core.cl_interface import CLKline, FX, XD
from chanlun.core.zs_branch import DivergenceResult
from chanlun.core.beichi_nest import NestedDivergence, BeichiNestCalculator


def _seg(_type: str, s_k: int, e_k: int) -> XD:
    """造 leave_seg(XD)：K 线序号区间 [s_k, e_k]，方向 _type（up/down）。"""
    def _fx(kidx, ftype):
        k = CLKline(k_index=kidx, date=None, h=0.0, l=0.0, o=0.0, c=0.0, a=0.0, klines=[])
        return FX(_type=ftype, k=k, klines=[k], val=0.0)
    if _type == "up":
        start, end = _fx(s_k, "di"), _fx(e_k, "ding")
    else:
        start, end = _fx(s_k, "ding"), _fx(e_k, "di")
    xd = XD(start=start, end=end, _type=_type, index=0)
    xd.done = True
    return xd


def _dv(_type, s_k, e_k, is_beichi=True, provisional=False) -> DivergenceResult:
    """造 DivergenceResult：leave_seg 时间 [s_k,e_k]、方向 _type；compare_seg 占位同段。"""
    c = _seg(_type, s_k, e_k)
    return DivergenceResult(is_beichi=is_beichi, kind="qs",
                            compare_seg=c, leave_seg=c, provisional=provisional)


def _node(level, zi, _type, s_k, e_k) -> NestedDivergence:
    return NestedDivergence(level=level, zs_index=zi, divergence=_dv(_type, s_k, e_k))


def test_span_returns_kline_index_range():
    calc = BeichiNestCalculator()
    assert calc._span(_dv("up", 3, 7)) == (3, 7)


def test_find_parent_strict_contain_same_dir():
    lo = _node(0, 0, "up", 3, 5)
    hi = _node(1, 0, "up", 1, 8)
    assert BeichiNestCalculator()._find_parent(lo, [hi]) is hi


def test_find_parent_opposite_dir_none():
    lo = _node(0, 0, "up", 3, 5)
    hi = _node(1, 0, "down", 1, 8)
    assert BeichiNestCalculator()._find_parent(lo, [hi]) is None


def test_find_parent_not_contained_none():
    # lo 右界 9 超出 hi 右界 8 → 非严格包含
    lo = _node(0, 0, "up", 3, 9)
    hi = _node(1, 0, "up", 1, 8)
    assert BeichiNestCalculator()._find_parent(lo, [hi]) is None


def test_find_parent_boundary_flush_ok():
    # 边界贴合（lo_e == hi_e，同一转折点）应算包含
    lo = _node(0, 0, "up", 3, 8)
    hi = _node(1, 0, "up", 1, 8)
    assert BeichiNestCalculator()._find_parent(lo, [hi]) is hi


def test_find_parent_innermost_wins():
    # lo[4,5] 同时落在 hi_a[1,8]、hi_b[3,6] → 取最内层 hi_b（跨度小）
    lo = _node(0, 0, "up", 4, 5)
    hi_a = _node(1, 0, "up", 1, 8)
    hi_b = _node(1, 1, "up", 3, 6)
    assert BeichiNestCalculator()._find_parent(lo, [hi_a, hi_b]) is hi_b


from chanlun.core.recursive_branch import LevelResult


def _lr(level, dvs) -> LevelResult:
    """造 fake LevelResult：只填 level + done_divergence(其余占位,calculate 不碰)。"""
    return LevelResult(level=level, zss=[], done_divergence=list(dvs), zslxs=[], upgrade_idx=[])


def test_calculate_empty_returns_empty():
    assert BeichiNestCalculator().calculate([]) == []


def test_calculate_basic_nesting():
    # L0[3,5]up 落在 L1[1,8]up → 挂为 child；顶层森林只剩 L1
    levels = [_lr(0, [_dv("up", 3, 5)]), _lr(1, [_dv("up", 1, 8)])]
    forest = BeichiNestCalculator().calculate(levels)
    assert len(forest) == 1
    assert forest[0].level == 1
    assert len(forest[0].children) == 1
    assert forest[0].children[0].level == 0


def test_calculate_opposite_dir_both_top():
    # 异向 → 不挂，两者皆顶层
    levels = [_lr(0, [_dv("up", 3, 5)]), _lr(1, [_dv("down", 1, 8)])]
    forest = BeichiNestCalculator().calculate(levels)
    assert len(forest) == 2


def test_calculate_not_contained_both_top():
    # L0 右界超出 → 不挂
    levels = [_lr(0, [_dv("up", 3, 9)]), _lr(1, [_dv("up", 1, 8)])]
    forest = BeichiNestCalculator().calculate(levels)
    assert len(forest) == 2


def test_calculate_dangling_low_is_root():
    # L0 背驰无 L1 父(L1 异向) → L0 自成顶层根
    levels = [_lr(0, [_dv("up", 3, 5)]), _lr(1, [_dv("down", 1, 8)])]
    forest = BeichiNestCalculator().calculate(levels)
    levels_in_forest = sorted(n.level for n in forest)
    assert levels_in_forest == [0, 1]


def test_calculate_provisional_excluded():
    # provisional=True 不入森林
    levels = [_lr(0, [_dv("up", 3, 5, provisional=True)])]
    assert BeichiNestCalculator().calculate(levels) == []


def test_calculate_non_beichi_excluded():
    # is_beichi=False 不入森林
    levels = [_lr(0, [_dv("up", 3, 5, is_beichi=False)])]
    assert BeichiNestCalculator().calculate(levels) == []


def test_calculate_none_divergence_skipped():
    # done_divergence 含 None(该中枢无背驰) → 跳过不报错
    levels = [_lr(0, [None, _dv("up", 3, 5)]), _lr(1, [_dv("up", 1, 8)])]
    forest = BeichiNestCalculator().calculate(levels)
    assert len(forest) == 1 and forest[0].level == 1


def test_calculate_three_level_chain():
    # L0[4,5] ⊂ L1[3,6] ⊂ L2[1,9] 同向 → 深度 3 链:L2 根→L1→L0
    levels = [
        _lr(0, [_dv("up", 4, 5)]),
        _lr(1, [_dv("up", 3, 6)]),
        _lr(2, [_dv("up", 1, 9)]),
    ]
    forest = BeichiNestCalculator().calculate(levels)
    assert len(forest) == 1
    l2 = forest[0]
    assert l2.level == 2 and len(l2.children) == 1
    l1 = l2.children[0]
    assert l1.level == 1 and len(l1.children) == 1
    assert l1.children[0].level == 0
