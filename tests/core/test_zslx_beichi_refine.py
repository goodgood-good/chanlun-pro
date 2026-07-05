"""递归层走势类型的背驰精修(审计 F3):坐实趋势背驰 = 走势类型切点。

原文口径:盘背/趋背是走势分段依据、走势结束点=背驰点。zslx_branch 原为纯几何
摆动分段(背驰不参与边界)——背驰后继续创新高的部分会被并进同一走势类型;
精修后在坐实趋势背驰的中枢处切开,后续另起(「背了又背对应不同走势」)。
只切同向趋势背驰(kind=qs、非 provisional、离开段读法);盘整背驰(pz)不切。
"""
from chanlun.core.zslx_branch import ZslxBranchCalculator
from chanlun.core.zs_branch import DivergenceResult


class _K:
    def __init__(self, idx):
        self.index = idx
        self.k_index = idx


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
        self.zs_high = max(s_v, e_v)
        self.zs_low = min(s_v, e_v)
        self.done = True


class _ZS:
    def __init__(self, lo, hi, i0):
        """三段核心的完成中枢:核心区/包络 = [lo,hi],时间从 i0 起。"""
        self.level = None
        self.done = True
        self.zd, self.zg = float(lo), float(hi)
        self.dd, self.gg = float(lo), float(hi)
        self.start = _Line("up", i0 - 10, lo - 2.0, i0, lo)        # 进入段(向上入区间)
        self.lines = [
            _Line("up", i0, lo, i0 + 10, hi),
            _Line("down", i0 + 10, hi, i0 + 20, lo),
            _Line("up", i0 + 20, lo, i0 + 30, hi),
        ]
        self.end = _Line("up", i0 + 30, lo, i0 + 40, hi + 2.0)     # 离开段(向上冲出)


def _dv_qs(zs, provisional=False, kind="qs"):
    return DivergenceResult(is_beichi=True, kind=kind, compare_seg=zs.start,
                            leave_seg=zs.end, provisional=provisional)


def _three_up_zss():
    """三个依次上行、本体分离的中枢(z2.dd > z1.gg …) → 几何法=单一 trend_up 段。"""
    return [_ZS(10, 20, 100), _ZS(25, 35, 200), _ZS(40, 50, 300)]


def test_settled_qs_beichi_splits_zslx():
    zss = _three_up_zss()
    divs = [None, _dv_qs(zss[1]), None]        # 中间中枢坐实趋势背驰
    wts = ZslxBranchCalculator().calculate(zss, divs)
    assert len(wts) == 2, "背驰中枢处必须切开(背驰点=走势结束点)"
    assert wts[0].zss[-1] is zss[1] and wts[0].done is True
    assert wts[1].zss == [zss[2]] and wts[1].done is False


def test_no_beichi_keeps_single_zslx():
    zss = _three_up_zss()
    wts = ZslxBranchCalculator().calculate(zss, [None, None, None])
    assert len(wts) == 1


def test_provisional_or_pz_not_split():
    zss = _three_up_zss()
    for divs in ([None, _dv_qs(zss[1], provisional=True), None],
                 [None, _dv_qs(zss[1], kind="pz"), None]):
        wts = ZslxBranchCalculator().calculate(zss, divs)
        assert len(wts) == 1, "provisional/盘整背驰不得切段"


def test_turnover_type_beichi_not_split():
    zss = _three_up_zss()
    dv = DivergenceResult(is_beichi=True, kind="qs", compare_seg=zss[0].end,
                          leave_seg=zss[1].start, provisional=False)  # 转折型:背驰段=进入段
    wts = ZslxBranchCalculator().calculate(zss, [None, dv, None])
    assert len(wts) == 1, "转折型背驰(背驰段=进入段)不是本中枢离开切点"


def test_tail_zs_beichi_no_internal_split():
    zss = _three_up_zss()
    divs = [None, None, _dv_qs(zss[2])]        # 段末中枢背驰=段自然终点,无内部切点
    wts = ZslxBranchCalculator().calculate(zss, divs)
    assert len(wts) == 1