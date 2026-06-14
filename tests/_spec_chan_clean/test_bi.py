"""§1.3 笔划分 测试。

fixture 真值由**定义推导**（A-21）：清晰锯齿走势 → 笔逐段交替；
gap/老新笔/同性去重 用直接构造的 merged+分型做单元隔离。

★ test_truncation_equivalence = C1 第一红线（无未来函数）的 A3 筐一客观判据：
  截断到 t 的**已确认**笔前缀，必须等于全量计算里 confirm_bar≤t 的同一前缀。
"""

from chan.bi import find_bis, find_bis_from_bars
from chan.config import DEFAULT, ChanConfig
from chan.types import Bar, ChanStatus, Direction, Fenxing, FxType, MergedK, confirmed_only


# ── 端到端 fixture：5 端点 / 4 笔 的清晰锯齿（无包含，每腿 5 根）──
def _zigzag_bars():
    rows = []  # (low, high)
    # 下跌到底A(idx4)
    rows += [(19, 20), (18, 19), (17, 18), (16, 17), (15, 16)]
    # 上涨到顶B(idx9)
    rows += [(16, 17), (17, 18), (18, 19), (19, 20), (20, 21)]
    # 下跌到底C(idx14)
    rows += [(19, 20), (18, 19), (17, 18), (16, 17), (15, 16)]
    # 上涨到顶D(idx19)
    rows += [(16, 17), (17, 18), (18, 19), (19, 20), (20, 21)]
    # 下跌到底E(idx24) + 两根确认底E
    rows += [(19, 20), (18, 19), (17, 18), (16, 17), (15, 16)]
    rows += [(16, 17), (17, 18)]
    return [Bar(idx=i, dt=None, o=lo, h=hi, l=lo, c=hi) for i, (lo, hi) in enumerate(rows)]


def _sig(bi):
    """笔的稳定指纹（端点位置/方向/高低/确认 bar），用于跨截断比对。"""
    return (bi.start_idx, bi.end_idx, int(bi.direction), bi.high, bi.low, bi.confirm_bar)


# ── 基础：锯齿出 4 笔，方向交替，末笔 PENDING，余 CONFIRMED ──
def test_clean_zigzag_pens():
    bis = find_bis_from_bars(_zigzag_bars(), DEFAULT)
    assert len(bis) == 4
    dirs = [b.direction for b in bis]
    assert dirs == [Direction.UP, Direction.DOWN, Direction.UP, Direction.DOWN]
    # 端点：底A(4)→顶B(9)→底C(14)→顶D(19)→底E(24)
    assert [(b.start_idx, b.end_idx) for b in bis] == [(4, 9), (9, 14), (14, 19), (19, 24)]
    assert [b.status for b in bis[:3]] == [ChanStatus.CONFIRMED] * 3
    assert bis[3].status is ChanStatus.PENDING
    # 已锁定笔的 confirm_bar = 埋葬时点（首个后继分型确认 bar，非端分型 bar 本身）
    assert bis[0].confirm_bar == 15   # 终端点 顶B 被 底C 埋葬
    assert bis[1].confirm_bar == 20   # 终端点 底C 被 顶D 埋葬
    assert bis[2].confirm_bar == 25   # 终端点 顶D 被 底E 埋葬


# ── ★ C1 / A3 筐一：截断等价（已确认笔前缀稳定，无未来函数）──
def _assert_truncation_equivalent(bars):
    full = find_bis_from_bars(bars, DEFAULT)
    full_confirmed = [b for b in full if b.status is ChanStatus.CONFIRMED]
    for t in range(3, len(bars)):
        trunc = find_bis_from_bars(bars[: t + 1], DEFAULT)
        trunc_confirmed = [b for b in trunc if b.status is ChanStatus.CONFIRMED]
        expected = [b for b in full_confirmed if b.confirm_bar <= t]
        assert [_sig(b) for b in trunc_confirmed] == [_sig(b) for b in expected], (
            f"t={t}: 截断已确认前缀 != 全量同前缀（无未来函数被破坏）"
        )


def test_truncation_equivalence():
    _assert_truncation_equivalent(_zigzag_bars())


def _turns_bars(turns):
    """turns=[(kind,level,leg_len)]，kind∈{'D'底,'T'顶}：按目标极值线性推进生成 bar。"""
    centers = []
    cur = turns[0][1] - turns[0][2] if turns[0][0] == "D" else turns[0][1] + turns[0][2]
    for _kind, level, ln in turns:
        step = (level - cur) / ln
        for _ in range(ln):
            cur += step
            centers.append(round(cur, 4))
        cur = level
    return [Bar(idx=i, dt=None, o=x - 0.5, h=x + 0.5, l=x - 0.5, c=x + 0.5)
            for i, x in enumerate(centers)]


# ── ★ 对抗 fixture（这两个曾抓出 pop 分支的未来函数 bug，必须常驻回归）──
def test_truncation_equivalence_deep_reversal():
    # 急反转：顶26 后 2 根暴跌到底8（深破前低）——旧 pop 分支会把顶26吞入下跌、泄漏未来
    _assert_truncation_equivalent(_turns_bars([
        ("D", 10, 5), ("T", 18, 4), ("D", 12, 6), ("T", 22, 5), ("D", 15, 4),
        ("T", 26, 6), ("D", 8, 3), ("T", 20, 5), ("D", 13, 4), ("T", 24, 6), ("D", 11, 5),
    ]))


def test_truncation_equivalence_double_cascade():
    # 连环急跌（多级回写压力）
    _assert_truncation_equivalent(_turns_bars([
        ("D", 10, 4), ("T", 16, 4), ("D", 11, 4), ("T", 20, 4), ("D", 13, 4), ("T", 24, 4),
        ("D", 6, 2), ("T", 9, 2), ("D", 3, 2), ("T", 22, 5), ("D", 14, 4), ("T", 28, 5), ("D", 12, 4),
    ]))


# ── confirmed_only 把临时笔挡在下游之外 ──
def test_confirmed_only_excludes_pending():
    bis = find_bis_from_bars(_zigzag_bars(), DEFAULT)
    visible = confirmed_only(bis)
    assert len(visible) == 3
    assert all(b.status is ChanStatus.CONFIRMED for b in visible)


# ── 直接构造：新笔间隔不足（中心间原始 bar <3）→ 不成笔 ──
def _direct_merged(specs):
    """specs=[(low,high,span_list)] → [MergedK]。"""
    return [MergedK(high=hi, low=lo, span=list(sp), direction=Direction.UP)
            for (lo, hi, sp) in specs]


def _fx(merged, k_idx, fx_type, confirm_bar):
    m = merged[k_idx]
    return Fenxing(fx_type=fx_type, k_idx=k_idx, high=m.high, low=m.low,
                   confirm_bar=confirm_bar, status=ChanStatus.CONFIRMED)


def test_new_bi_gap_too_close_rejected():
    # 顶@k0、底@k2：中心下标距=2 <3（共用合并 K，违结合律）→ 任何模式都不成笔
    merged = _direct_merged([(19, 20, [0]), (14, 16, [1]), (10, 11, [2])])
    fxs = [_fx(merged, 0, FxType.DING, 5), _fx(merged, 2, FxType.DI, 9)]
    assert find_bis(fxs, merged, DEFAULT) == []


# ── 直接构造：老笔需 ≥1 独立合并 K（中心距≥4），新笔放松到原始≥3 ──
def test_old_mode_stricter_than_new():
    # 中心距=3（不共用，①过）；中间两 shoulder 合并 K 共 3 根原始 bar
    merged = _direct_merged([
        (19, 20, [0]),       # k0 顶center
        (15, 18, [1, 2]),    # k1 shoulder（span 2 原始 bar）
        (12, 14, [3]),       # k2 shoulder（span 1 原始 bar）
        (10, 11, [4]),       # k3 底center
    ])
    fxs = [_fx(merged, 0, FxType.DING, 5), _fx(merged, 3, FxType.DI, 9)]
    # 新笔：原始 between = 4-0-1 = 3 ≥3 → 成 1 笔
    assert len(find_bis(fxs, merged, ChanConfig(bi_type="new"))) == 1
    # 老笔：中心距=3 <4（无独立合并 K）→ 不成笔
    assert find_bis(fxs, merged, ChanConfig(bi_type="old")) == []


# ── 直接构造：同性质相邻分型只留更极端者（課77 步骤②）──
def test_same_type_dedup_keeps_more_extreme():
    # 底@k0、底@k4(更低)、顶@k8：应取更低的底k4 与顶k8 成一笔（底k0 被X）
    merged = _direct_merged([
        (12, 13, [0]),       # k0 底（较高）
        (12.5, 13.5, [1]),
        (12.4, 13.4, [2]),
        (12.3, 13.3, [3]),
        (10, 11, [4]),       # k4 底（更低）— 保留
        (11, 12, [5]),
        (12, 13, [6]),
        (13, 14, [7]),
        (15, 16, [8]),       # k8 顶 — 保留
    ])
    fxs = [
        _fx(merged, 0, FxType.DI, 3),
        _fx(merged, 4, FxType.DI, 7),
        _fx(merged, 8, FxType.DING, 11),
    ]
    bis = find_bis(fxs, merged, DEFAULT)
    assert len(bis) == 1
    assert bis[0].start_idx == 4 and bis[0].end_idx == 8   # 底k4→顶k8
    assert bis[0].direction is Direction.UP
