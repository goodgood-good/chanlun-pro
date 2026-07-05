"""O2:pending 披露门独立参数(原文 Y1「前三个连续次级别走势类型重叠部分确定」L018:8/L020:2)。

成枢确认门(min_zs_lines,现=5)管 done;pending(已产生未完成)按原文 3 段重叠即已「确定」,
披露门收紧到 5 会让右边缘中枢迟两段才可见(图表当下性+nest 区间套介入延迟)。
pending_min_lines 缺省=None(沿用 min_zs_lines,零行为变化);显式传 3=原文披露口径。
done 判定不受影响(bs/zslx 只读 done_zss)。
"""
from chanlun.core.zs_calculator import ZsCalculator


class _K:
    def __init__(self, idx):
        self.k_index = idx
        self.index = idx


class _FX:
    def __init__(self, idx, val):
        self.k = _K(idx)
        self.val = val


class _Line:
    def __init__(self, i, type_, s_i, s_v, e_i, e_v):
        self.index = i
        self.type = type_
        self._type = type_
        self.start = _FX(s_i, s_v)
        self.end = _FX(e_i, e_v)
        self.zs_low = min(s_v, e_v)
        self.zs_high = max(s_v, e_v)
        self.done = True


def _three_overlap_lines():
    """恰好 3 段重叠(上下上,重叠区 [10,20])——原文口径中枢已「确定」,未完成。"""
    return [_Line(0, "up", 0, 5.0, 10, 20.0),
            _Line(1, "down", 10, 20.0, 20, 10.0),
            _Line(2, "up", 20, 10.0, 30, 22.0)]


def test_default_pending_gate_unchanged():
    zc = ZsCalculator(min_zs_lines=5)
    zc.calculate(_three_overlap_lines())
    assert zc.pending_zs is None          # 默认:披露门=成枢门 5,3 段不披露(现状回归保护)


def test_pending_min_lines_3_discloses_early():
    zc = ZsCalculator(min_zs_lines=5, pending_min_lines=3)
    zc.calculate(_three_overlap_lines())
    assert zc.pending_zs is not None      # 原文口径:3 段重叠即披露 pending
    assert len(zc.pending_zs.lines) == 3
    assert zc.zss == []                   # done 确认门不受影响