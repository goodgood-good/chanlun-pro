"""tests/core/test_zs_calculator.py — 中枢识别(ZsCalculator)回归测试。

锁定缺陷：「恰好三段线段重叠」的中枢被 ZsCalculator 丢弃。

缠论原文（《缠中说禅股市技术理论解释2017》走势分解章）：
    「站在最小分析级别的角度，每一线段就是其次级别走势类型，
      三个线段重合部分就构成最小分析级别的走势中枢。」

即三段线段重叠即成中枢。旧实现把「最后一个重叠段」当离开段并排除在
核心段之外，使最小中枢被迫需要 4 段重叠，导致：
  1. 三段重叠 + 第四段离开中枢 → 中枢被丢弃（三买的基础中枢凭空消失）；
  2. 单调性破坏：三段已 pending 的中枢，追加一根离开段后反被删除；
  3. 四段重叠时核心段计数少 1 段。

这些用例直接构造受控线段序列喂入真实 ``ZsCalculator``，不走整条 K 线流水线，
以确定性地复现「三段重叠 + 离开」结构。

子项目①（进入段原文化）新增用例：中枢可以位于序列开头、没有进入段
（``start=None``）——原文中枢定义只讲「3+ 连续次级别走势类型重叠」，不含进入段。
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


def test_three_overlapping_segments_plus_departure_form_one_zhongshu():
    """三段重叠 + 第四段离开中枢 → 应识别出 1 个中枢（缠论：三段即成中枢）。"""
    lines = _three_overlap_segments()
    lines.append(_seg(4, "down", 10, 8.5))  # 离开段：回调到 8.5，不跌破 zg=8

    zss = ZsCalculator().calculate(lines)

    assert len(zss) == 1, "三段重叠区 [5,8] 应构成 1 个中枢"
    zs = zss[0]
    assert zs.done is True
    assert (zs.zg, zs.zd) == (8, 5)
    assert [l.index for l in zs.lines] == [1, 2, 3], "核心段应为笔1/2/3"
    # 离开段 = 最后一个核心段（bs_point_calculator 依据 zs.end.type 判离开方向）
    assert zs.end is zs.lines[-1]
    assert zs.end.type == "up", "向上离开 → zs.end.type 须为 up（三买判定依赖）"


def test_pending_three_segment_zhongshu_survives_appended_departure():
    """单调性：三段 pending 中枢，追加一根离开段后应「完成」而非被删除。"""
    calc = ZsCalculator()

    base = _three_overlap_segments()
    zss_pending = calc.calculate(base)
    assert len(zss_pending) == 1, "三段重叠（数据到此为止）应为 1 个 pending 中枢"
    assert zss_pending[0].done is False
    assert [l.index for l in zss_pending[0].lines] == [1, 2, 3]

    # 同一计算器追加离开段（走增量路径）
    zss_done = calc.calculate(base + [_seg(4, "down", 10, 8.5)])
    assert len(zss_done) == 1, "追加离开段后中枢必须仍在（不得被删除）"
    assert zss_done[0].done is True
    assert [l.index for l in zss_done[0].lines] == [1, 2, 3]


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
    """连续两个中枢都应被识别（旧实现因丢弃首个三段中枢而错乱）。"""
    lines = [
        _seg(0, "down", 9, 4),
        _seg(1, "up", 4, 8),
        _seg(2, "down", 8, 5),
        _seg(3, "up", 5, 9),        # 中枢1 核心，区间 [5,8]
        _seg(4, "down", 9, 8.5),    # 中枢1 离开段
        _seg(5, "up", 8.5, 10),
        _seg(6, "down", 10, 8.7),
        _seg(7, "up", 8.7, 12),     # 中枢2 核心，区间 [8.7,9]
        _seg(8, "down", 12, 10.5),  # 中枢2 离开段
    ]

    zss = ZsCalculator().calculate(lines)

    assert len(zss) == 2, "应识别出 2 个连续中枢"
    assert all(zs.done for zs in zss)
    assert (zss[0].zg, zss[0].zd) == (8, 5)
    assert (zss[1].zg, zss[1].zd) == (9, 8.7)


def test_three_segments_form_zhongshu_with_no_entry():
    """子项目①：恰好三段重叠、序列之前无任何线段 → 1 个 pending 中枢，无进入段。

    缠论中枢定义只讲「3+ 连续次级别走势类型重叠」，不含进入段；
    旧实现 ``len(lines) < 4`` 直接返回空，与「三段重叠即成中枢」冲突。
    """
    lines = [
        _seg(0, "up", 4, 8),
        _seg(1, "down", 8, 5),
        _seg(2, "up", 5, 10),
    ]
    zss = ZsCalculator().calculate(lines)
    assert len(zss) == 1, "三段重叠即成中枢，不应因缺进入段而返回空"
    zs = zss[0]
    assert zs.start is None, "序列开头的中枢没有进入段"
    assert (zs.zg, zs.zd) == (8, 5)
    assert [l.index for l in zs.lines] == [0, 1, 2]


def test_zhongshu_at_data_start_completes_without_entry():
    """子项目①：开头三段重叠 + 第四段离开 → 完成的中枢，进入段为 None。"""
    lines = [
        _seg(0, "up", 4, 8),
        _seg(1, "down", 8, 5),
        _seg(2, "up", 5, 10),
        _seg(3, "down", 10, 8.5),  # 离开段
    ]
    zss = ZsCalculator().calculate(lines)
    assert len(zss) == 1
    zs = zss[0]
    assert zs.start is None
    assert zs.done is True
    assert (zs.zg, zs.zd) == (8, 5)
    assert [l.index for l in zs.lines] == [0, 1, 2]
    assert zs.end is zs.lines[-1]


def test_require_alternation_false_allows_same_direction_core():
    """require_alternation=False：同向三段重叠也能成中枢（供 ④ 的 L≥1 扫描用）。"""
    # 三段同为 up、范围都含 [5,8]——方向不交替
    lines = [_seg(0, "up", 5, 8), _seg(1, "up", 5, 8), _seg(2, "up", 5, 8)]
    assert ZsCalculator(require_alternation=True).calculate(lines) == []
    zss = ZsCalculator(require_alternation=False).calculate(lines)
    assert len(zss) == 1, "关闭交替检查后，同向三段重叠应成中枢"
    assert (zss[0].zg, zss[0].zd) == (8, 5)


def test_require_alternation_defaults_true():
    """默认 require_alternation=True：行为与原实现一致（交替检查照旧）。"""
    lines = [_seg(0, "up", 5, 8), _seg(1, "up", 5, 8), _seg(2, "up", 5, 8)]
    assert ZsCalculator().calculate(lines) == []
