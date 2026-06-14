"""§1.2 分型识别 测试。

fixture 真值由**定义推导**（A-21），在含处理后的合并 K 上判定：
顶分型 = 中心合并 K 高低点皆为相邻三者最高；底分型对称（課62:19）。
"""

from chan.fenxing import find_fenxings
from chan.types import ChanStatus, Direction, FxType, MergedK


def _merged(rows):
    """rows = [(low, high), ...] → [MergedK]（span 用顺序 idx 占位，direction 占位）。"""
    out = []
    for i, (lo, hi) in enumerate(rows):
        out.append(MergedK(high=hi, low=lo, span=[i], direction=Direction.UP))
    return out


# ── 基础：一顶一底 ──
def test_finds_one_ding_one_di():
    merged = _merged([
        (10.0, 11.0),   # 0
        (11.0, 12.0),   # 1  顶（高低双高于左右）
        (10.5, 11.5),   # 2
        (9.5, 10.5),    # 3  底（高低双低于左右）
        (10.0, 11.0),   # 4
    ])
    fxs = find_fenxings(merged)
    assert len(fxs) == 2
    ding, di = fxs
    assert ding.fx_type is FxType.DING and ding.k_idx == 1
    assert di.fx_type is FxType.DI and di.k_idx == 3
    # 价位取中心合并 K 的高低
    assert ding.high == 12.0 and ding.low == 11.0
    assert di.high == 10.5 and di.low == 9.5


# ── 无未来函数：confirm_bar = 第三根（右邻）合并 K 的**首根**原始 bar（稳定，C1）──
def test_confirm_bar_is_right_neighbor_start():
    merged = _merged([(10.0, 11.0), (11.0, 12.0), (10.5, 11.5)])
    # 给中心右邻一个多 bar 的 span：取 start_idx（稳定），非 end_idx（随含处理吸收而漂移）
    merged[2].span = [7, 8, 9]
    fxs = find_fenxings(merged)
    assert len(fxs) == 1
    assert fxs[0].confirm_bar == 7          # c.start_idx（C1 稳定）


# ── 单调上升 → 无分型 ──
def test_monotonic_up_has_no_fenxing():
    merged = _merged([(10.0, 11.0), (10.5, 11.5), (11.0, 12.0), (11.5, 12.5)])
    assert find_fenxings(merged) == []


# ── 全部已确认 ──
def test_all_confirmed():
    merged = _merged([(10.0, 11.0), (11.0, 12.0), (10.5, 11.5)])
    fxs = find_fenxings(merged)
    assert all(f.status is ChanStatus.CONFIRMED for f in fxs)


# ── 少于三根 → 无法判定 → 空 ──
def test_too_short_returns_empty():
    assert find_fenxings(_merged([(10.0, 11.0), (11.0, 12.0)])) == []
    assert find_fenxings([]) == []
