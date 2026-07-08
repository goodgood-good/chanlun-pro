"""R1-C13/C15 原文对齐修正的守护网。

C13(L029:12): 转折型一类点(dv.leave_seg=本中枢进入段)的 rebound_target 应取
转折前趋势最后一个中枢(前一 done 中枢)的 DD/GG——本中枢是背驰后新生的, 其
dd/gg 就在买点极值附近, 目标退化为恒真; 无前中枢时为 None 而非恒真值。
同向(非转折)一类点仍取本中枢 dd/gg(test_o6_o7_fields 既有用例钉住)。
C15(L020:65): 定理二升级判定「后GG>=前DD」含等号——波动区间恰好触及(lo==hi)
按原文=形成高级别中枢, 不得归趋势(与 classify_rel 严格不等号口径一致)。
"""
from types import SimpleNamespace

from chanlun.core.bs_branch import BsBranchCalculator
from chanlun.core.bs1_branch import Bs1BranchCalculator
from chanlun.core.recursive_branch import _dingli2_pair
from chanlun.core.zs_branch import ZsBranchResult, DivergenceResult


class _K:
    def __init__(self, idx):
        self.k_index = idx
        self.index = idx
        self.date = None


class _FX:
    def __init__(self, idx, val):
        self.k = _K(idx)
        self.val = val


class _Line:
    def __init__(self, type_, s_i, s_v, e_i, e_v):
        self.type = type_
        self._type = type_
        self.start = _FX(s_i, s_v)
        self.end = _FX(e_i, e_v)
        self.done = True


class _ZS:
    def __init__(self, zd, zg, dd, gg, i0, enter_dir="down"):
        self.zd, self.zg, self.dd, self.gg = map(float, (zd, zg, dd, gg))
        self.done = True
        self.lines = [_Line("up", i0, zd, i0 + 10, zg),
                      _Line("down", i0 + 10, zg, i0 + 20, zd),
                      _Line("up", i0 + 20, zd, i0 + 30, zg)]
        self.start = _Line(enter_dir, i0 - 10, zg, i0, zd)
        self.end = _Line("down", i0 + 30, zd, i0 + 40, zd - 2)


def _turn_dv(prev, z):
    # 转折型: leave_seg=本中枢进入段(zs_branch._divergence_for 转折分支),
    # compare_seg=前中枢离开段
    return DivergenceResult(is_beichi=True, kind="qs",
                            compare_seg=(prev.end if prev is not None else None),
                            leave_seg=z.start, provisional=False)


def test_turn_type_1buy_rebound_target_uses_prev_zs_dd():
    prev = _ZS(zd=30, zg=36, dd=28, gg=38, i0=50)   # 转折前趋势最后一个中枢(高位)
    z = _ZS(zd=12, zg=18, dd=10, gg=20, i0=100)     # 背驰后新生中枢
    zr = ZsBranchResult(done_zss=[prev, z], live=[], freeze_idx=0,
                        done_divergence=[None, _turn_dv(prev, z)])
    pts = BsBranchCalculator()._first_class(zr)
    assert len(pts) == 1 and pts[0].bs_type == "1buy"
    assert pts[0].rebound_target == 28.0            # prev.DD, 而非 z.dd(10)=恒真退化


def test_turn_type_without_prev_zs_target_is_none():
    z = _ZS(zd=12, zg=18, dd=10, gg=20, i0=100)
    zr = ZsBranchResult(done_zss=[z], live=[], freeze_idx=0,
                        done_divergence=[_turn_dv(None, z)])
    pts = BsBranchCalculator()._first_class(zr)
    assert len(pts) == 1
    assert pts[0].rebound_target is None            # 无前中枢=不可判, 勿给恒真值


def test_bs1_turn_type_rebound_target_uses_prev_zs():
    prev = _ZS(zd=30, zg=36, dd=28, gg=38, i0=50)
    z = _ZS(zd=12, zg=18, dd=10, gg=20, i0=100)
    lr = SimpleNamespace(level=1, zss=[prev, z],
                         done_divergence=[None, _turn_dv(prev, z)])
    pts = Bs1BranchCalculator().calculate([lr])
    assert len(pts) == 1 and pts[0].bs_type == "1buy"
    assert pts[0].rebound_target == 28.0


def test_dingli2_pair_touching_wave_interval_is_upgrade():
    # 核心区分离(b.zg < a.zd) 且 b.gg == a.dd(波动区间恰好触及):
    # L020:65「后ZG<前ZD 且 后GG>=前DD」含等号 → 升级带, 不得归趋势。
    a = SimpleNamespace(zd=20.0, zg=26.0, dd=15.0, gg=30.0)
    b = SimpleNamespace(zd=8.0, zg=12.0, dd=5.0, gg=15.0)   # b.gg==a.dd==15
    band = _dingli2_pair(a, b)
    assert band is not None, "波动区间触及(等号)按原文应构成升级对"
    assert band == (15.0, 15.0)


def test_dingli2_pair_separated_wave_interval_still_trend():
    a = SimpleNamespace(zd=20.0, zg=26.0, dd=15.0, gg=30.0)
    b = SimpleNamespace(zd=8.0, zg=12.0, dd=5.0, gg=14.0)   # b.gg(14) < a.dd(15) 分离
    assert _dingli2_pair(a, b) is None