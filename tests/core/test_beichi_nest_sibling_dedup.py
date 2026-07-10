# -*- coding: utf-8 -*-
"""beichi_nest 同父兄弟去重(终检 R12-2)。

同一高级别离开段内多个同向已坐实次级别背驰,只有落在离开段末端(转折点)的一个是
区间套收敛确认点 → operable;中途同向背驰不是。旧码 parent.children.append(lo) 对每个
兄弟都挂 → interval_nest operable=innermost&&nested 使所有叶子兄弟都 operable=True
(中途买点被 nest-soft 门放大到 ratio*1.0,忠实应 *0.75)。修复:同父只保留 leave_seg
结束 k_index 最大(末端)一个,其余落森林根(depth=1→非 operable)。
"""
import types

from chanlun.core.beichi_nest import BeichiNestCalculator
from chanlun.core.interval_nest import IntervalNestCalculator


def _dv(start_k, end_k, direction):
    k0 = types.SimpleNamespace(k=types.SimpleNamespace(k_index=start_k))
    k1 = types.SimpleNamespace(k=types.SimpleNamespace(k_index=end_k))
    leave = types.SimpleNamespace(start=k0, end=k1, _type=direction)
    return types.SimpleNamespace(leave_seg=leave, is_beichi=True, provisional=False)


def _level(level, dvs):
    return types.SimpleNamespace(level=level, done_divergence=list(dvs))


def _operable(levels):
    forest = BeichiNestCalculator().calculate(levels)
    reads = IntervalNestCalculator().calculate(forest)
    return [r for r in reads if r.operable]


def test_sibling_dedup_keeps_only_last_end():
    # L1 父 down 背驰 [0,100];L0 两子 down:B0a 中途[10,40] / B0b 末端[60,95]
    l0 = _level(0, [_dv(10, 40, "down"), _dv(60, 95, "down")])
    l1 = _level(1, [_dv(0, 100, "down")])
    ops = _operable([l0, l1])
    # 旧码 2 个 operable;修复后恰 1 个 = 末端 B0b(end_k=95)
    assert len(ops) == 1, f"应恰 1 个 operable(末端), 实得 {len(ops)}"
    assert ops[0].node.divergence.leave_seg.end.k.k_index == 95


def test_sibling_dedup_last_end_regardless_of_list_order():
    # 列表把末端放前:B0b[55,90] 先, B0a[10,40] 后 → 仍留 end_k 最大者(90)
    l0 = _level(0, [_dv(55, 90, "down"), _dv(10, 40, "down")])
    l1 = _level(1, [_dv(0, 100, "down")])
    ops = _operable([l0, l1])
    assert len(ops) == 1
    assert ops[0].node.divergence.leave_seg.end.k.k_index == 90


def test_single_child_still_operable():
    # 单子无需去重:正常 operable(不回归)
    l0 = _level(0, [_dv(60, 95, "down")])
    l1 = _level(1, [_dv(0, 100, "down")])
    ops = _operable([l0, l1])
    assert len(ops) == 1
    assert ops[0].node.divergence.leave_seg.end.k.k_index == 95


def test_opposite_direction_not_merged():
    # 反向次级别背驰不挂同父(不同 _type):down 父下只 B0a 一子 → operable;
    # up B0b 无 up 父落根 → 非 operable。证去重只在同向兄弟间。
    l0 = _level(0, [_dv(10, 40, "down"), _dv(60, 95, "up")])
    l1 = _level(1, [_dv(0, 100, "down")])
    ops = _operable([l0, l1])
    assert len(ops) == 1
    assert ops[0].node.divergence.leave_seg.end.k.k_index == 40


def test_three_level_midway_sibling_subtree_not_operable():
    """R1-F1-1: ≥3 层链上中途兄弟的整棵子树都不得 operable。

    L2 P ⊃ {L1 中途 A(带 L0 子 a0), L1 末端 B(带 L0 子 b0)}:A 落败后若只"不挂 A"
    而不摘 a0,a0 仍 depth=2+innermost+nested → operable → 同一 P 离开段内 2 个
    operable,破「收敛单点」不变量(中途买点继续吃 ratio*1.0)。修复后恰 1 个=b0。"""
    a0 = _dv(10, 30, "down")
    b0 = _dv(70, 95, "down")
    l0 = _level(0, [a0, b0])
    l1 = _level(1, [_dv(5, 40, "down"), _dv(60, 98, "down")])   # A 中途 / B 末端
    l2 = _level(2, [_dv(0, 100, "down")])                        # P
    ops = _operable([l0, l1, l2])
    assert len(ops) == 1, f"应恰 1 个 operable(P 段收敛单点), 实得 {len(ops)}"
    assert ops[0].node.divergence.leave_seg.end.k.k_index == 95   # b0(P→B→b0 链最内)


def test_two_parents_each_keep_own_tail_operable():
    """R1-F4-1 钉扎: 去重按父隔离(id(parent) 分组),不同父各自保留自己的末端子。

    mutant(按 level-pair 全局取末端、无视父分组)会让早父的末端子丢 operable,
    此前 4 用例全单父钉不住该语义。两父场景: 两个末端子必须都 operable。"""
    l0 = _level(0, [_dv(10, 40, "down"), _dv(60, 95, "down"),
                    _dv(210, 240, "down"), _dv(260, 290, "down")])
    l1 = _level(1, [_dv(0, 100, "down"), _dv(200, 300, "down")])
    ops = _operable([l0, l1])
    ends = sorted(r.node.divergence.leave_seg.end.k.k_index for r in ops)
    assert ends == [95, 290], f"两父各留己末端子 operable, 实得 {ends}"
