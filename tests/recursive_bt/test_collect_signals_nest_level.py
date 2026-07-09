# -*- coding: utf-8 -*-
"""C12: collect_branch_signals 区间套标注按 p.level 构造 _div_key, 而 bs2 跨级二类点的
divergence 注册于次级别(node.level=level-1), 且 nest 森林每次新建对象 → id 与 level-key
均 miss → nest_operable 恒 False, nest 门控下 L1+ 二类信号被恒过滤。修复补 level-1 回退查询。

用假 cd 构造: L1 的 2buy 其 divergence(DV1) 与 nest 读的 node.divergence(DV2)结构相同但
不同对象(模拟 nest 森林重建), node.level=0(=level-1)。修复前 nest_operable=False/depth=0(红),
修复后经 level-1 回退命中 → operable=True/depth=2(绿)。
"""
import datetime as _dt

from chanlun.recursive_bt.engine.engine import collect_branch_signals


class _K:
    def __init__(self, kidx, date):
        self.k_index = kidx
        self.date = date


class _FX:
    def __init__(self, kidx, date, val):
        self.k = _K(kidx, date)
        self.val = val


class _Leave:
    def __init__(self, ltype, s, e):
        self._type = ltype
        self.start = s
        self.end = e


class _Div:
    """结构相同、对象不同 → _div_key 相同(除 level 分量), id 不同。"""
    def __init__(self):
        self.kind = "pz"
        self.leave_seg = _Leave(
            "up",
            _FX(100, _dt.datetime(2026, 7, 1, 9, 30), 10.0),
            _FX(110, _dt.datetime(2026, 7, 1, 10, 0), 12.0),
        )


class _BSP:
    def __init__(self, level, bs_type, div):
        self.level = level
        self.bs_type = bs_type
        self.divergence = div
        self.anchor_fx = _FX(110, _dt.datetime(2026, 7, 1, 10, 0), 12.0)
        self.zs = None
        self.structural_stop_below = None
        self.structural_stop_above = None


class _Node:
    def __init__(self, level, div):
        self.level = level
        self.divergence = div


class _Read:
    def __init__(self, level, div, operable, depth):
        self.node = _Node(level, div)
        self.operable = operable
        self.depth = depth


class _FakeCD:
    """无 get_bis/config → _interval_nest_reads 走 get_branch_interval_nest 回退。
    无 get_branch_dingli2_bspoints → 升级块跳过。"""
    def __init__(self, bsps, reads):
        self._bsps = bsps
        self._reads = reads

    def get_branch_bspoints(self, use_xd=False):
        return self._bsps

    def get_branch_interval_nest(self):
        return self._reads


def test_l1_second_class_nest_resolved_via_level_minus_1():
    div_bsp = _Div()          # bsp 侧 divergence 对象
    div_read = _Div()         # nest 读侧 divergence 对象(结构相同, 不同 id)
    assert id(div_bsp) != id(div_read)
    bsp = _BSP(level=1, bs_type="2buy", div=div_bsp)
    read = _Read(level=0, div=div_read, operable=True, depth=2)  # 注册于 level-1
    cd = _FakeCD([bsp], [read])

    sigs = collect_branch_signals(cd, use_xd=False, annotate_nest=True)
    l1 = [s for s in sigs if (s.level or 0) == 1 and s.bs_type == "2buy"]
    assert len(l1) == 1
    sig = l1[0]
    # 修复前: p.level=1 查 _div_key(1,DV) 而注册在 _div_key(0,DV) → miss → False/0
    # 修复后: level-1 回退 _div_key(0,DV) 命中 → operable=True/depth=2
    assert sig.nest_operable is True, "level-1 回退应命中 nest 读, operable=True"
    assert int(sig.nest_depth) == 2


def test_l0_first_class_not_affected_by_fallback():
    """L0 一类点(level=0)按 p.level 直接命中, level-1(=-1)回退不误伤。"""
    div_bsp = _Div()
    div_read = _Div()
    bsp = _BSP(level=0, bs_type="1buy", div=div_bsp)
    read = _Read(level=0, div=div_read, operable=True, depth=3)  # 注册于 level 0
    cd = _FakeCD([bsp], [read])
    sigs = collect_branch_signals(cd, use_xd=False, annotate_nest=True)
    s0 = [s for s in sigs if (s.level or 0) == 0 and s.bs_type == "1buy"][0]
    assert s0.nest_operable is True
    assert int(s0.nest_depth) == 3