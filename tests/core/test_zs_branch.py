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


# ---------------------------------------------------------------------------
# Task 5: ZsBranchCalculator — 成中枢 + 右边缘 H1/H2 分叉
# ---------------------------------------------------------------------------
def test_right_edge_h1_h2_fork():
    # 进入段(在中枢上方不重叠) + 4 段重叠核心 [5,8]，数据到此为止
    lines = [
        _seg(0, "down", 10, 9),   # 进入段（[9,10] 不与 [5,8] 重叠）
        _seg(1, "up", 4, 8),      # 核心 a
        _seg(2, "down", 8, 5),    # 核心 b
        _seg(3, "up", 5, 10),     # 核心 c
        _seg(4, "down", 10, 6),   # 第4段重叠核心 [5,8]（触及）→ H1/H2 歧义
    ]
    res = zs_branch.ZsBranchCalculator().calculate(lines)
    assert res.done_zss == []                       # 右边缘未确认，无冻结中枢
    nodes = sorted(h.node1 for h in res.live)
    assert nodes == ["core", "leave"]               # H1 + H2 两分支
    for h in res.live:
        assert (h.zs.zd, h.zs.zg) == (5, 8)         # zg/zd 恒由前三段定
    h1 = next(h for h in res.live if h.node1 == "core")
    h2 = next(h for h in res.live if h.node1 == "leave")
    assert h1.zs.done is False and h2.zs.done is True


# ---------------------------------------------------------------------------
# Task 6: 回试 ZG 坍缩（节点①，中心定理一）
# ---------------------------------------------------------------------------
def test_extend_confirms_prev_as_core_keeps_two_branches():
    base = [
        _seg(0, "down", 10, 9), _seg(1, "up", 4, 8),
        _seg(2, "down", 8, 5), _seg(3, "up", 5, 10), _seg(4, "down", 10, 6),
    ]
    base.append(_seg(5, "up", 6, 9))   # 第5段 [6,9] 触核心 [5,8] → seg4 确认核心、中枢长到5段
    res = zs_branch.ZsBranchCalculator().calculate(base)
    assert res.done_zss == []                        # 仍未离开，无冻结
    assert sorted(h.node1 for h in res.live) == ["core", "leave"]  # 新末段(seg5)再分叉
    for h in res.live:
        assert len(h.zs.lines) == 5                  # seg4 已并入核心(5段)
        assert (h.zs.zd, h.zs.zg) == (5, 8)


def test_leave_then_new_structure_freezes_zhongshu():
    base = [
        _seg(0, "down", 10, 9), _seg(1, "up", 4, 8),
        _seg(2, "down", 8, 5), _seg(3, "up", 5, 10), _seg(4, "down", 10, 6),
        _seg(5, "up", 9, 14),                         # 离开核心[5,8]：[9,14] 不触 [5,8]
        _seg(6, "down", 14, 11), _seg(7, "up", 11, 15), _seg(8, "down", 15, 12),  # 新结构
    ]
    res = zs_branch.ZsBranchCalculator().calculate(base)
    assert len(res.done_zss) == 1                    # 原 [5,8] 中枢冻结
    assert res.done_zss[0].done is True
    assert (res.done_zss[0].zd, res.done_zss[0].zg) == (5, 8)
    assert len(res.done_zss[0].lines) == 4           # 核心 seg1-4


# ---------------------------------------------------------------------------
# Task 7: 节点② 延伸≤5 / 9 段升级标记（第33课）
# ---------------------------------------------------------------------------
def _overlap_core(n_core: int):
    """构造「进入段 + n_core 段全部重叠核心 [5,8]」的线段序列。"""
    segs = [_seg(0, "down", 10, 9)]  # 进入段（[9,10] 不与 [5,8] 重叠）
    vals = [(4, 8)] + [(8, 5), (5, 8)] * 8  # 首段定下沿4，其后 down/up 在 [5,8] 内交替
    for k in range(1, n_core + 1):
        s, e = vals[k - 1]
        segs.append(_seg(k, "up" if s < e else "down", s, e))
    return segs


def test_node2_upgrade_flag_at_9_segments():
    res = zs_branch.ZsBranchCalculator().calculate(_overlap_core(9))
    assert any(h.upgrade for h in res.live), "9 段核心应触发升级标记"


def test_node2_no_upgrade_at_8_segments():
    res = zs_branch.ZsBranchCalculator().calculate(_overlap_core(8))
    assert all(not h.upgrade for h in res.live)


# ---------------------------------------------------------------------------
# Task 8: 节点③ 趋势/扩张分类（中心定理二，包络口径）
# ---------------------------------------------------------------------------
def test_node3_classify_trend_and_expand():
    from chanlun.core.cl_interface import ZS

    def _zs(dd, gg):
        z = ZS(zs_type="xd", start=None)
        z.dd, z.gg = dd, gg
        return z

    assert zs_branch.classify_rel(_zs(0, 5), _zs(6, 10)) == "trend_up"    # 后DD6 > 前GG5
    assert zs_branch.classify_rel(_zs(6, 10), _zs(0, 5)) == "trend_down"  # 后GG5 < 前DD6
    assert zs_branch.classify_rel(_zs(0, 5), _zs(4, 9)) == "expand"       # 包络相交 [4,5]


def test_node3_rel_prev_set_on_live_branches():
    # 复用 leave 场景：done=[中枢1 包络4..10], live=中枢2(包络9..15) 的分支
    base = [
        _seg(0, "down", 10, 9), _seg(1, "up", 4, 8),
        _seg(2, "down", 8, 5), _seg(3, "up", 5, 10), _seg(4, "down", 10, 6),
        _seg(5, "up", 9, 14), _seg(6, "down", 14, 11), _seg(7, "up", 11, 15), _seg(8, "down", 15, 12),
    ]
    res = zs_branch.ZsBranchCalculator().calculate(base)
    # 中枢1 包络[4,10] 与 中枢2 包络[9,15] 相交[9,10] → expand（中心定理二·包络口径）
    assert all(h.rel_prev == "expand" for h in res.live)


# ---------------------------------------------------------------------------
# Task 9: 左侧冻结 freeze_idx + 合法性不变量
# ---------------------------------------------------------------------------
def test_freeze_idx_marks_settled_prefix():
    base = [
        _seg(0, "down", 10, 9), _seg(1, "up", 4, 8), _seg(2, "down", 8, 5),
        _seg(3, "up", 5, 10), _seg(4, "down", 10, 6),
    ]
    res = zs_branch.ZsBranchCalculator().calculate(base)
    # 仅右边缘 pending：freeze_idx 指向核心起点(=1)，其前(进入段0)为 settled 前缀
    assert res.freeze_idx == 1


def test_done_zs_invariants():
    base = [
        _seg(0, "down", 10, 9), _seg(1, "up", 4, 8), _seg(2, "down", 8, 5),
        _seg(3, "up", 5, 10), _seg(4, "down", 10, 6),
        _seg(5, "up", 9, 14), _seg(6, "down", 14, 11), _seg(7, "up", 11, 15), _seg(8, "down", 15, 12),
    ]
    res = zs_branch.ZsBranchCalculator().calculate(base)
    for z in res.done_zss:
        assert len(z.lines) >= 4 and z.zd < z.zg


# ---------------------------------------------------------------------------
# 修复(评审 C1): 本体包络 —— 离开段不得撑大包络、否则趋势恒判不出
# ---------------------------------------------------------------------------
def test_body_envelope_excludes_breakout_leg():
    """中枢本体包络只取前3段定义段，剔除离开段的远摆。"""
    from chanlun.core.cl_interface import ZS
    zs = ZS(zs_type="xd", start=None)
    zs.lines = [_seg(0, "up", 5, 8), _seg(1, "down", 8, 5), _seg(2, "up", 5, 8),
                _seg(3, "up", 5, 22)]   # 第4段=离开段冲到22
    zs.dd, zs.gg = 5, 22                 # 完整包络被撑到22
    assert zs_branch.body_envelope(zs) == (5, 8)   # 本体不含离开段的22


def test_node3_clean_uptrend_is_trend_not_expand():
    """干净上涨趋势(中枢1[5,8] → 离开段冲到22 → 中枢2[19,22])必须判 trend_up，非 expand。"""
    lines = [
        _seg(0, "up", 5, 8), _seg(1, "down", 8, 5), _seg(2, "up", 5, 8), _seg(3, "down", 8, 5),
        _seg(4, "up", 5, 22),                       # 离开段冲到22
        _seg(5, "down", 22, 19), _seg(6, "up", 19, 22), _seg(7, "down", 22, 19), _seg(8, "up", 19, 22),
    ]
    res = zs_branch.ZsBranchCalculator().calculate(lines)
    assert len(res.done_zss) == 1                    # 中枢1 已完成
    # 中枢1 本体[5,8] 远低于 中枢2 本体[19,22] → 上涨趋势
    assert all(h.rel_prev == "trend_up" for h in res.live), \
        f"应判 trend_up，实得 {[h.rel_prev for h in res.live]}"


def test_two_done_zhongshu_form_uptrend():
    """两个都完成的中枢构成上涨趋势(评审 H2: ≥2 中枢覆盖)。
    中枢1[5,8] / 中枢2[16,19] 本体不相交、依次抬高 → trend_up。"""
    lines = [
        _seg(0, "up", 5, 8), _seg(1, "down", 8, 5), _seg(2, "up", 5, 8), _seg(3, "down", 8, 5),
        _seg(4, "up", 5, 19),                                       # 中枢1 离开段(=中枢2进入)
        _seg(5, "down", 19, 16), _seg(6, "up", 16, 19), _seg(7, "down", 19, 16), _seg(8, "up", 16, 19),
        _seg(9, "up", 16, 30),                                      # 中枢2 离开段
        _seg(10, "down", 30, 27), _seg(11, "up", 27, 30), _seg(12, "down", 30, 27),  # 中枢3雏形(3段,未成立)
    ]
    res = zs_branch.ZsBranchCalculator().calculate(lines)
    assert len(res.done_zss) == 2, f"应有2个完成中枢，实得 {len(res.done_zss)}"
    z1, z2 = res.done_zss
    assert (z1.zd, z1.zg) == (5, 8)
    assert (z2.zd, z2.zg) == (16, 19)
    assert zs_branch.classify_rel(z1, z2) == "trend_up"


def test_c2_entry_promotion_via_delegation():
    """评审 C2: 委托 ZsCalculator 后，开头5段重叠的首段被提升为进入段(核心4段)，
    与证过的引擎一致——不再是手搓的5段。中枢间夹回抽段也不再走样。"""
    lines = [
        _seg(0, "up", 5, 8), _seg(1, "down", 8, 5), _seg(2, "up", 5, 8), _seg(3, "down", 8, 5),
        _seg(4, "up", 5, 12), _seg(5, "down", 12, 11),       # 离开段 + 回抽段
        _seg(6, "up", 11, 14), _seg(7, "down", 14, 11), _seg(8, "up", 11, 14), _seg(9, "down", 14, 11),
    ]
    res = zs_branch.ZsBranchCalculator().calculate(lines)
    assert len(res.done_zss) == 1
    z1 = res.done_zss[0]
    assert (z1.zd, z1.zg) == (5, 8)
    assert len(z1.lines) == 3                 # 进入段提升 + 离开段剥离 → 本体3段
    assert all(h.rel_prev == "trend_up" for h in res.live)   # 中枢2在上 → 趋势


def test_correct_exit_strips_breakout_leg():
    """离开段(定向冲出区间)从中枢本体剥出：本体=lines[:-1]，z.end=离开段，gg 收紧。"""
    s1 = _seg(1, "down", 8, 7); s2 = _seg(2, "up", 7, 9); s3 = _seg(3, "down", 9, 7)
    exit_leg = _seg(4, "up", 7, 15)            # 离开段：从区间[7,8]定向冲出到15
    zs = _make_zs(_seg(0, "up", 5, 8), [s1, s2, s3, exit_leg], 7, 8)
    zs.done = True
    c = zs_branch.correct_exit(zs, 3)
    assert len(c.lines) == 3                    # 离开段剥出 → 本体3段
    assert c.lines[-1] is s3                    # 本体末段 = 最后一根震荡
    assert c.end is exit_leg                    # 离开段记为 z.end
    assert c.gg < 15                            # gg 收紧到本体(不含离开段的15)


def test_correct_exit_no_strip_when_last_inside_zone():
    """末段终点仍在区间内(不是离开段)→不剥。"""
    s1 = _seg(1, "down", 8, 7); s2 = _seg(2, "up", 7, 9)
    s3 = _seg(3, "down", 9, 7); s4 = _seg(4, "up", 7, 8)
    zs = _make_zs(_seg(0, "up", 5, 8), [s1, s2, s3, s4], 7, 8)
    zs.done = True
    c = zs_branch.correct_exit(zs, 3)
    assert len(c.lines) == 4                    # 末段终点 8 在区间内 → 不剥


def test_fork_branches_have_independent_lines():
    """评审 High-2: H1/H2 各持独立 lines 容器，改一支不污染另一支。"""
    lines = [
        _seg(0, "down", 10, 9), _seg(1, "up", 4, 8),
        _seg(2, "down", 8, 5), _seg(3, "up", 5, 10), _seg(4, "down", 10, 6),
    ]
    res = zs_branch.ZsBranchCalculator().calculate(lines)
    h1, h2 = res.live
    assert h1.zs.lines is not h2.zs.lines
    n1 = len(h1.zs.lines)
    h2.zs.lines.append(h2.zs.lines[-1])       # 改 H2 的 lines
    assert len(h1.zs.lines) == n1             # H1 不受影响


def test_core_swing_into_next_zhongshu_is_expand():
    """评审 High-1 驳回: 中枢1 核心段震荡摸到 23(瞬间波动 5~23)，中枢2[19,22] 落其内
    → 按原文 line 9980(趋势中枢瞬间波动不重叠) 这是扩张、非趋势。body_envelope 计入
    核心远摆是正确的(区别于离开段：离开段是趋势腿、被 lines[:3] 剔除)。"""
    lines = [
        _seg(0, "up", 5, 8), _seg(1, "down", 8, 5), _seg(2, "up", 5, 23), _seg(3, "down", 23, 6),
        _seg(4, "up", 6, 8), _seg(5, "down", 8, 5),
        _seg(6, "up", 5, 21),
        _seg(7, "down", 21, 19), _seg(8, "up", 19, 22), _seg(9, "down", 22, 19), _seg(10, "up", 19, 22),
    ]
    res = zs_branch.ZsBranchCalculator().calculate(lines)
    assert len(res.done_zss) == 1
    assert res.done_zss[0].gg == 23                       # 中枢1 瞬间波动确实到 23
    assert all(h.rel_prev == "expand" for h in res.live)  # 中枢2 在中枢1 波动内 → 扩张


# ---------------------------------------------------------------------------
# 进入段校正（审图 #3：升/跌入段被误当核心）—— 原文第20课回升/回调形成 + line 21650
# ---------------------------------------------------------------------------
def _make_zs(start_seg, core_segs, zd, zg):
    from chanlun.core.cl_interface import ZS
    z = ZS(zs_type="xd", start=start_seg)
    z.lines = list(core_segs)
    z.zd, z.zg = zd, zg
    z._bounds_dirty = True
    z.update_boundaries()
    return z


def test_correct_entry_shifts_when_entry_diverges():
    """引擎认的进入段背离中枢[7,8](向下远离)→真进入段是升入段s1,中枢起点右移到第一根下降段。"""
    entry_wrong = _seg(0, "down", 6, 5)      # 引擎认的"进入段"：向下、远离中枢[7,8]
    s1 = _seg(1, "up", 5, 8)                  # 真·升入段（从 5 升进区间上沿 8）
    s2 = _seg(2, "down", 8, 7); s3 = _seg(3, "up", 7, 9)
    s4 = _seg(4, "down", 9, 7); s5 = _seg(5, "up", 7, 8)
    zs = _make_zs(entry_wrong, [s1, s2, s3, s4, s5], 7, 8)
    c = zs_branch.correct_entry(zs, 4)
    assert c.start is s1                      # 进入段改成升入段
    assert c.lines[0] is s2                   # 中枢从第一根下降段起（回调形成）
    assert len(c.lines) == 4
    assert (c.zd, c.zg) == (7, 8)


def test_correct_entry_no_shift_when_entry_toward():
    """进入段朝中枢走(向上升入区间)→引擎认对了,不动。"""
    entry_ok = _seg(0, "up", 5, 8)            # 进入段：向上、升进中枢[7,8]
    s1 = _seg(1, "down", 8, 7); s2 = _seg(2, "up", 7, 9)
    s3 = _seg(3, "down", 9, 7); s4 = _seg(4, "up", 7, 8)
    zs = _make_zs(entry_ok, [s1, s2, s3, s4], 7, 8)
    c = zs_branch.correct_entry(zs, 4)
    assert c.start is entry_ok                # 不变
    assert c.lines[0] is s1


# ===========================================================================
# P3: 实时内联背驰（H2a 耦合）
# ===========================================================================
def test_divergence_result_dataclass():
    cseg = _seg(0, "up", 5, 8)
    lseg = _seg(1, "up", 5, 12)
    dv = zs_branch.DivergenceResult(
        is_beichi=True, kind="pz", compare_seg=cseg, leave_seg=lseg, provisional=True
    )
    assert dv.is_beichi is True and dv.kind == "pz" and dv.provisional is True
    assert dv.compare_seg is cseg and dv.leave_seg is lseg


def test_hypothesis_divergence_defaults_none():
    from chanlun.core.cl_interface import ZS
    zs = ZS(zs_type="xd", start=None)
    h = zs_branch.ZsHypothesis(zs=zs, node1="leave")
    assert h.divergence is None                      # 默认 None（退化）


def test_result_done_divergence_defaults_empty():
    res = zs_branch.ZsBranchResult(done_zss=[], live=[], freeze_idx=0)
    assert res.done_divergence == []                 # 默认空列表（退化）


# ===========================================================================
# P3 Task 2: ZsBranchCalculator 构造注入 ld_provider/frequency/wzgx
# ===========================================================================
def test_calculator_construct_with_ld_provider():
    calc = zs_branch.ZsBranchCalculator(
        ld_provider=lambda s, e: {"hist": {"up_sum": 0, "down_sum": 0}, "dif": {"max": 0, "min": 0}},
        frequency="1m",
    )
    assert calc.ld_provider is not None
    assert calc.frequency == "1m"
    from chanlun.core.cl_interface import Config
    assert calc.wzgx == Config.ZS_WZGX_ZGD.value          # 默认 ZGD


def test_no_ld_provider_yields_none_divergence():
    """退化：无 ld_provider → done_divergence 全 None、live 各 divergence 全 None。"""
    lines = [
        _seg(0, "up", 5, 8), _seg(1, "down", 8, 5), _seg(2, "up", 5, 8),
        _seg(3, "down", 8, 5), _seg(4, "up", 5, 12),
    ]
    res = zs_branch.ZsBranchCalculator().calculate(lines)   # 无 ld_provider
    assert all(d is None for d in res.done_divergence)
    assert all(h.divergence is None for h in res.live)
