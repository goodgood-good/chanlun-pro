"""小转大候选的背驰-三类点时间/结构关联(审计 F5)。

旧实现 _beichi_leave_dirs 收集全历史坐实趋势背驰方向集合,任意陈年背驰会给
此后所有三类点配对出候选。新口径:只认「时序最后一个坐实趋势背驰」,且其
背驰段终点须不晚于三类点锚(背驰在前、转折在后)。
"""
from chanlun.core.recursive_branch import LevelResult
from chanlun.core.xiaozhuanda_branch import XiaoZhuanDaCalculator
from chanlun.core.zs_branch import DivergenceResult


class _K:
    def __init__(self, k_index):
        self.k_index = k_index
        self.date = None


class _FX:
    def __init__(self, k_index, val):
        self.k = _K(k_index)
        self.val = val


class _Line:
    def __init__(self, type_, s_ki, s_val, e_ki, e_val):
        self._type = type_
        self.type = type_
        self.start = _FX(s_ki, s_val)
        self.end = _FX(e_ki, e_val)


class _ZS:
    def __init__(self, zg, zd, lines, end):
        self.zg = zg
        self.zd = zd
        self.lines = lines
        self.end = end


def _dv(direction, end_ki):
    seg = _Line(direction, end_ki - 5, 0.0, end_ki, 0.0)
    return DivergenceResult(is_beichi=True, kind="qs", compare_seg=seg,
                            leave_seg=seg, provisional=False)


def _sub_level_with_3buy(divergences, anchor_ki=100):
    """次级别:最后中枢向上离开+回试不破 ZG → 三买(anchor k_index=anchor_ki)。"""
    core = [_Line("up", 60, 8.0, 70, 10.0), _Line("down", 70, 10.0, 80, 8.5),
            _Line("up", 80, 8.5, 90, 9.8)]
    leave = _Line("up", 90, 9.0, 95, 15.0)              # 向上离开
    retest = _Line("down", 95, 15.0, anchor_ki, 11.0)   # 回试 11 >= ZG=10 → 3buy
    last_zs = _ZS(zg=10.0, zd=8.0, lines=core + [leave], end=leave)
    units = core + [leave, retest]
    return LevelResult(level=0, zss=[last_zs], done_divergence=list(divergences),
                       zslxs=[], units=units)


def _levels(sub):
    big = LevelResult(level=1, zss=[], done_divergence=[], zslxs=[], units=[])
    return [sub, big]


def test_recent_bottom_beichi_pairs_3buy():
    """最近一次坐实背驰=底背驰(在三类点之前) + 三买 → 产向上候选(回归保护)。"""
    sub = _sub_level_with_3buy([_dv("down", 80)])
    out = XiaoZhuanDaCalculator().calculate(_levels(sub))
    assert len(out) == 1 and out[0].direction == "up"


def test_stale_bottom_beichi_superseded_by_top_beichi():
    """陈年底背驰(k=10)之后又有更近的顶背驰(k=50) → 三买不得再配底背驰产候选。"""
    sub = _sub_level_with_3buy([_dv("down", 10), _dv("up", 50)])
    out = XiaoZhuanDaCalculator().calculate(_levels(sub))
    assert out == []


def test_beichi_after_anchor_not_paired():
    """背驰段终点(k=150)晚于三类点锚(k=100) → 非引发本次转折的背驰,不产候选。"""
    sub = _sub_level_with_3buy([_dv("down", 150)])
    out = XiaoZhuanDaCalculator().calculate(_levels(sub))
    assert out == []