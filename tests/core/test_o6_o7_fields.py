"""O6/O7 纯加法字段。

O6(L029:14):一类买点后「反弹一定触及最后一个中枢的 DD」——BuySellPoint.rebound_target
= 末中枢 DD(1buy)/GG(1sell),供策略出场目标与一买质量验证(反弹不触 DD=分解有误)。
O7(L043:20):小转大高发于趋势冲顶/赶底——XiaoZhuanDaCandidate.sub_trend_zs_count
= 次级别末尾连续同向中枢数(≥2=真趋势,越深越接近冲顶/赶底,供候选分级)。
"""
from chanlun.core.bs_branch import BsBranchCalculator
from chanlun.core.zs_branch import ZsBranchResult, DivergenceResult
from chanlun.core.xiaozhuanda_branch import _tail_trend_depth


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
        self.zs_low = min(s_v, e_v)
        self.zs_high = max(s_v, e_v)
        self.done = True


class _ZS:
    def __init__(self, zd, zg, dd, gg, i0, leave_dir="down"):
        self.zd, self.zg, self.dd, self.gg = map(float, (zd, zg, dd, gg))
        self.done = True
        self.lines = [_Line("up", i0, zd, i0 + 10, zg),
                      _Line("down", i0 + 10, zg, i0 + 20, zd),
                      _Line("up", i0 + 20, zd, i0 + 30, zg)]
        self.start = _Line(leave_dir, i0 - 10, zg, i0, zd)
        self.end = _Line(leave_dir, i0 + 30, zd, i0 + 40, zd - 2)


def test_first_class_carries_rebound_target():
    z = _ZS(zd=12, zg=18, dd=10, gg=20, i0=100)
    c = z.end                                      # 向下离开段(背驰段)
    dv = DivergenceResult(is_beichi=True, kind="qs", compare_seg=z.start,
                          leave_seg=c, provisional=False)
    zr = ZsBranchResult(done_zss=[z], live=[], freeze_idx=0, done_divergence=[dv])
    pts = BsBranchCalculator()._first_class(zr)
    assert len(pts) == 1 and pts[0].bs_type == "1buy"
    assert pts[0].rebound_target == 10.0           # 反弹必触及末中枢 DD(L029:14)


def test_tail_trend_depth_counts_consecutive():
    a = _ZS(12, 18, 10, 20, 100)
    b = _ZS(25, 33, 22, 35, 200)                   # b.DD(22)>a.GG(20) trend_up
    c = _ZS(40, 48, 38, 50, 300)                   # c.DD(38)>b.GG(35) trend_up
    assert _tail_trend_depth([a, b, c]) == 3
    d = _ZS(41, 47, 36, 49, 400)                   # 与 c 波动重叠 → 非趋势
    assert _tail_trend_depth([a, b, c, d]) == 1