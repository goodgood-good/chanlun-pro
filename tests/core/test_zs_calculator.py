"""tests/core/test_zs_calculator.py — 中枢识别(ZsCalculator)回归测试。

中枢成立的最小线段数（项目既定口径）：

  L0 线段中枢 = 至少 **4** 条线段重叠。线段是最低级别的走势类型，4 条
  线段重叠才构成 1min 级别中枢。这是项目**有意偏离原文**的口径——原文
  《缠中说禅股市技术理论解释2017》第三章·第二十五节原话是「三个线段
  重合部分就构成最小分析级别的走势中枢」(3 段)；本项目按既定口径取 4 段。
  `ZsCalculator(min_zs_lines=...)` 参数化：L0 默认 4，④ 递归 L≥1（构成段
  是走势类型）按原文「3 个次级别走势类型重叠成中枢」取 3。

离开段（最后一个重叠段）计入核心 ``center.lines``，故「4 段重叠」即
``len(center.lines) >= 4``。恰好 3 段重叠、第 4 段即离开 → 不构成中枢。

这些用例直接构造受控线段序列喂入真实 ``ZsCalculator``，不走整条 K 线
流水线，以确定性地复现各种「N 段重叠 + 离开」结构。

进入段可选：中枢可位于序列开头、没有进入段（``start=None``）——中枢
定义不含进入段。
"""

from __future__ import annotations

from chanlun.core.cl_interface import CLKline, FX, XD
from chanlun.core.zs_calculator import ZsCalculator


def _seg(index: int, _type: str, start_val: float, end_val: float) -> XD:
    """构造一根线段(XD)。

    - up 段：起点为底分型(低)、终点为顶分型(高)；down 段相反。
    - zs_high/zs_low 按「已完成段」口径取端点 max/min（见 xd_calculator）。
    - 端点 K 索引随 index 递增，使增量定位 (``_locate_line``) 可用。
    """

    def _fx(kidx: int, val: float, ftype: str) -> FX:
        k = CLKline(k_index=kidx, date=None, h=val, l=val, o=val, c=val, a=0.0, klines=[])
        return FX(_type=ftype, k=k, klines=[k], val=val)

    if _type == "up":
        start = _fx(index, start_val, "di")
        end = _fx(index + 1, end_val, "ding")
    else:
        start = _fx(index, start_val, "ding")
        end = _fx(index + 1, end_val, "di")

    xd = XD(start=start, end=end, _type=_type, index=index)
    xd.done = True
    xd.zs_high = max(start_val, end_val)
    xd.zs_low = min(start_val, end_val)
    return xd


def _three_overlap_segments() -> list[XD]:
    """进入段 + 三段重叠核心，重叠区间 [5, 8]。

    进入段刻意取在中枢 [5,8] 之上、与之不重叠——这样核心仍是笔1/2/3，
    用于测「中枢带一个（区间外的）进入段」；「无进入段」另有专门用例。
    """
    return [
        _seg(0, "down", 10, 9),  # 进入段（在中枢 [5,8] 之上，不与之重叠）
        _seg(1, "up", 4, 8),     # 核心 a
        _seg(2, "down", 8, 5),   # 核心 b
        _seg(3, "up", 5, 10),    # 核心 c
    ]


def test_three_overlapping_segments_plus_departure_not_zhongshu():
    """恰好三段重叠 + 第四段离开 → 不构成中枢（最小中枢需 4 段重叠）。"""
    lines = _three_overlap_segments()
    lines.append(_seg(4, "down", 10, 8.5))  # 第四段回调到 8.5、不与 [5,8] 重叠 → 离开

    zss = ZsCalculator().calculate(lines)

    assert zss == [], "仅三段重叠（第四段即离开）→ 不足 4 段，不构成中枢"


def test_three_segment_overlap_is_not_zhongshu():
    """三段重叠（数据到此为止）→ 不足 4 段，连 pending 中枢都不构成。"""
    assert ZsCalculator().calculate(_three_overlap_segments()) == []


def test_four_segment_pending_zhongshu_completes_on_appended_departure():
    """增量：四段重叠的 pending 中枢，追加离开段后应「完成」而非被删除。"""
    calc = ZsCalculator()

    base = _three_overlap_segments() + [_seg(4, "down", 10, 3)]  # 进入段 + 四段核心
    zss_pending = calc.calculate(base)
    assert len(zss_pending) == 1, "四段重叠（数据到此为止）应为 1 个 pending 中枢"
    assert zss_pending[0].done is False
    assert [l.index for l in zss_pending[0].lines] == [1, 2, 3, 4]

    # 同一计算器追加离开段（走增量路径）
    zss_done = calc.calculate(base + [_seg(5, "up", 3, 4.5)])
    assert len(zss_done) == 1, "追加离开段后中枢必须仍在（不得被删除）"
    assert zss_done[0].done is True
    assert [l.index for l in zss_done[0].lines] == [1, 2, 3, 4]


def test_fourth_overlapping_segment_counts_as_core():
    """四段重叠 + 第五段离开 → 核心段应含全部 4 段重叠线段（旧实现少算 1 段）。"""
    lines = _three_overlap_segments()
    lines.append(_seg(4, "down", 10, 3))   # 第四段，仍与 [5,8] 重叠
    lines.append(_seg(5, "up", 3, 4.5))    # 第五段，整体在 zd=5 之下 → 离开

    zss = ZsCalculator().calculate(lines)

    assert len(zss) == 1
    zs = zss[0]
    assert zs.done is True
    assert [l.index for l in zs.lines] == [1, 2, 3, 4], "四段重叠应全部计入核心段"
    assert zs.end is zs.lines[-1]


def test_two_consecutive_zhongshu_are_both_identified():
    """连续两个中枢都应被识别（旧实现因丢弃首个三段中枢而错乱）。

    原文(kobo.125.1)ZG=min(g₁,g₂)、ZD=max(d₁,d₂)——只取前 2 段。
    中枢1 前 2 段 seg(0)/seg(1) → zg=min(9,8)=8, zd=max(4,4)=4。
    中枢2 前 2 段 seg(4)/seg(5) → zg=min(9,10)=9, zd=max(8.5,8.5)=8.5。
    """
    lines = [
        _seg(0, "down", 9, 4),
        _seg(1, "up", 4, 8),
        _seg(2, "down", 8, 5),
        _seg(3, "up", 5, 9),        # 中枢1 核心末段
        _seg(4, "down", 9, 8.5),    # 中枢1 离开段(也是中枢2 首段)
        _seg(5, "up", 8.5, 10),
        _seg(6, "down", 10, 8.7),
        _seg(7, "up", 8.7, 12),     # 中枢2 核心末段
        _seg(8, "down", 12, 10.5),  # 中枢2 离开段
    ]

    zss = ZsCalculator().calculate(lines)

    assert len(zss) == 2, "应识别出 2 个连续中枢"
    assert all(zs.done for zs in zss)
    assert (zss[0].zg, zss[0].zd) == (8, 4)
    assert (zss[1].zg, zss[1].zd) == (9, 8.5)


def test_four_segments_form_zhongshu_with_no_entry():
    """恰好四段重叠、序列之前无任何线段 → 1 个 pending 中枢，无进入段。

    中枢定义不含进入段；min_zs_lines=4 的门槛对「开头无进入段」中枢同样
    适用，恰好四段重叠即起步。
    """
    lines = [
        _seg(0, "up", 4, 8),
        _seg(1, "down", 8, 5),
        _seg(2, "up", 5, 10),
        _seg(3, "down", 10, 6),
    ]
    zss = ZsCalculator().calculate(lines)
    assert len(zss) == 1, "四段重叠即成中枢，不应因缺进入段而返回空"
    zs = zss[0]
    assert zs.start is None, "序列开头的中枢没有进入段"
    assert zs.done is False
    assert zs.end is lines[-1], "四段重叠时最后一段就是离开段"
    assert (zs.zg, zs.zd) == (8, 5)
    assert [l.index for l in zs.lines] == [0, 1, 2, 3]


def test_five_segments_at_data_start_promotes_first_to_entry():
    """开头五段连续重叠时,第 1 段应作为进入段,后 4 段构成中枢。"""
    lines = [
        _seg(0, "up", 4, 8),
        _seg(1, "down", 8, 5),
        _seg(2, "up", 5, 10),
        _seg(3, "down", 10, 6),
        _seg(4, "up", 6, 9),
    ]

    zss = ZsCalculator().calculate(lines)

    assert len(zss) == 1
    zs = zss[0]
    assert zs.start is lines[0]
    assert zs.done is False
    assert zs.end is lines[-1]
    assert (zs.zg, zs.zd) == (8, 5)
    assert [l.index for l in zs.lines] == [1, 2, 3, 4]


def test_five_segments_at_data_start_then_departure_uses_first_as_entry():
    """开头五段重叠并出现后续脱离时,第 1 段仍应保留为进入段。"""
    lines = [
        _seg(0, "up", 4, 8),
        _seg(1, "down", 8, 5),
        _seg(2, "up", 5, 10),
        _seg(3, "down", 10, 6),
        _seg(4, "up", 6, 9),
        _seg(5, "down", 4.5, 3),
    ]

    zss = ZsCalculator().calculate(lines)

    assert len(zss) == 1
    zs = zss[0]
    assert zs.start is lines[0]
    assert zs.done is True
    assert zs.end is lines[4]
    assert (zs.zg, zs.zd) == (8, 5)
    assert [l.index for l in zs.lines] == [1, 2, 3, 4]


def test_zhongshu_at_data_start_completes_without_entry():
    """开头四段重叠 + 第五段离开 → 完成的中枢，进入段为 None。"""
    lines = [
        _seg(0, "up", 4, 8),
        _seg(1, "down", 8, 5),
        _seg(2, "up", 5, 10),
        _seg(3, "down", 10, 3),    # 第四段，仍与 [5,8] 重叠 → 计入核心
        _seg(4, "up", 3, 4.5),     # 第五段，整体在 zd=5 之下 → 离开
    ]
    zss = ZsCalculator().calculate(lines)
    assert len(zss) == 1
    zs = zss[0]
    assert zs.start is None
    assert zs.done is True
    assert (zs.zg, zs.zd) == (8, 5)
    assert [l.index for l in zs.lines] == [0, 1, 2, 3]
    assert zs.end is zs.lines[-1]


def test_require_alternation_false_allows_same_direction_core():
    """require_alternation=False：同向四段重叠也能成中枢（供 ④ 的 L≥1 扫描用）。"""
    # 四段同为 up、范围都含 [5,8]——方向不交替
    lines = [_seg(i, "up", 5, 8) for i in range(4)]
    assert ZsCalculator(require_alternation=True).calculate(lines) == []
    zss = ZsCalculator(require_alternation=False).calculate(lines)
    assert len(zss) == 1, "关闭交替检查后，同向四段重叠应成中枢"
    assert (zss[0].zg, zss[0].zd) == (8, 5)


def test_require_alternation_defaults_true():
    """默认 require_alternation=True：同向段不交替 → 交替检查照旧拦下。"""
    lines = [_seg(i, "up", 5, 8) for i in range(4)]
    assert ZsCalculator().calculate(lines) == []
