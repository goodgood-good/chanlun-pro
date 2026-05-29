"""tests/core/test_zs_branch.py — P1 中枢多假设结构核(zs_branch) TDD。

线段 fixture 沿用 tests/core/test_zs_calculator.py 的 `_seg` 范式：直接构造
受控 XD 序列喂入引擎，不走整条 K 线流水线，确定性复现各种结构（同时绕开
「笔划分浮点敏感」的坑——P1 的输入就是确定性线段）。

口径（与宪法 §3.5 / zs_calculator 一致）：
- 成中枢的重叠用严格 `<`（ZD<ZG 才算非退化重叠）。
- 延伸/扩张的「触及」用闭区间 `<=`。
- 最小中枢 = 4 段重叠（含离开段，L0 既定口径）。
"""

from __future__ import annotations

from chanlun.core.cl_interface import CLKline, FX, XD
from chanlun.core import zs_branch


def _seg(index: int, _type: str, start_val: float, end_val: float) -> XD:
    """合成线段(XD)，沿用 tests/core/test_zs_calculator.py 范式。

    - up 段：起点底分型(低)、终点顶分型(高)；down 段相反。
    - zs_high/zs_low 取端点 max/min（已完成段口径）。
    - 端点 K 索引随 index 递增，使增量定位可用。
    """

    def _fx(kidx: int, val: float, ftype: str) -> FX:
        k = CLKline(k_index=kidx, date=None, h=val, l=val, o=val, c=val, a=0.0, klines=[])
        return FX(_type=ftype, k=k, klines=[k], val=val)

    if _type == "up":
        start, end = _fx(index, start_val, "di"), _fx(index + 1, end_val, "ding")
    else:
        start, end = _fx(index, start_val, "ding"), _fx(index + 1, end_val, "di")

    xd = XD(start=start, end=end, _type=_type, index=index)
    xd.done = True
    xd.zs_high = max(start_val, end_val)
    xd.zs_low = min(start_val, end_val)
    return xd


# ---------------------------------------------------------------------------
# Task 1: 核心区间 core_interval
# ---------------------------------------------------------------------------
def test_core_interval_overlap():
    a = _seg(0, "up", 4, 8)     # [4,8]
    b = _seg(1, "down", 8, 5)   # [5,8]
    c = _seg(2, "up", 5, 10)    # [5,10]
    # ZD=max(4,5,5)=5, ZG=min(8,8,10)=8
    assert zs_branch.core_interval(a, b, c) == (5, 8)


def test_core_interval_no_overlap_returns_none():
    a = _seg(0, "up", 1, 3)     # [1,3]
    b = _seg(1, "down", 3, 2)   # [2,3]
    c = _seg(2, "up", 5, 9)     # [5,9] —— 与前两段无共同重叠
    assert zs_branch.core_interval(a, b, c) is None


# ---------------------------------------------------------------------------
# Task 2: 包络 envelope
# ---------------------------------------------------------------------------
def test_envelope_min_low_max_high():
    lines = [_seg(0, "up", 4, 8), _seg(1, "down", 8, 3), _seg(2, "up", 3, 11)]
    # DD=min(4,3,3)=3, GG=max(8,8,11)=11
    assert zs_branch.envelope(lines) == (3, 11)


# ---------------------------------------------------------------------------
# Task 3: 触及 touches（闭区间口径）
# ---------------------------------------------------------------------------
def test_touches_closed_interval():
    # 触边即算（闭区间）：段 [8,10] 与核心 [5,8] 在 8 处相切 → 触及
    seg_edge = _seg(0, "up", 8, 10)
    assert zs_branch.touches(seg_edge, 5, 8) is True
    # 完全在外 → 不触
    seg_out = _seg(1, "up", 9, 12)
    assert zs_branch.touches(seg_out, 5, 8) is False


# ---------------------------------------------------------------------------
# Task 4: 数据模型 ZsHypothesis / ZsBranchResult
# ---------------------------------------------------------------------------
def test_dataclasses_construct():
    from chanlun.core.cl_interface import ZS
    zs = ZS(zs_type="xd", start=None)
    h = zs_branch.ZsHypothesis(zs=zs, node1="core")
    assert h.node1 == "core" and h.rel_prev is None and h.upgrade is False
    res = zs_branch.ZsBranchResult(done_zss=[], live=[h], freeze_idx=0)
    assert res.live[0] is h and res.freeze_idx == 0
