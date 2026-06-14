"""§1.1 含处理 + A-8 起点定向 测试。

fixture 真值由**定义推导**（A-21），不照抄任何 lesson 配图。
"""

import pytest

from chan.config import ChanConfig, DEFAULT
from chan.inclusion import process_inclusion, assert_post_adjusted, _contains, _find_anchor_start
from chan.types import Bar, Direction


def _bars(rows):
    """rows = [(idx, low, high), ...] → [Bar]（dt 占位）。"""
    return [Bar(idx=i, dt=None, o=lo, h=hi, l=lo, c=hi) for (i, lo, hi) in rows]


# ── A-8 起点定向：开头即歧义包含（fixture #1，Round5靶1 实测逼出的硬情形）──
def test_anchor_skips_leading_inclusion_and_drops_prefix():
    # idx1 母线最大，2/3/4 层层被包含；4-5、5-6 才无包含 → 起点三K组=(4,5,6)。
    bars = _bars([
        (1, 9.00, 12.00),
        (2, 9.50, 11.50),   # 含于1
        (3, 9.80, 11.00),   # 含于2
        (4, 9.70, 11.20),   # 含3
        (5, 10.50, 12.50),  # 与4无包含
        (6, 11.50, 13.50),  # 与5无包含
    ])
    assert _find_anchor_start(bars, eps=0.0) == 3  # 0-based：bar idx=4
    merged = process_inclusion(bars, DEFAULT)
    # 起点前 bar1/2/3 整体丢弃；其后三根均无包含 → 三个单根合并K
    assert [m.span for m in merged] == [[4], [5], [6]]
    assert merged[0].direction is Direction.UP  # 4→5 高低双升


# ── 含处理：向上方向的包含合并取值（向上取两低之"高"）──
def test_up_merge_takes_higher_low():
    bars = _bars([
        (1, 10.0, 11.0),
        (2, 10.2, 11.2),    # 无包含(升)
        (3, 10.4, 11.4),    # 无包含(升)
        (4, 10.5, 11.3),    # 被3包含 → 向上合并
    ])
    merged = process_inclusion(bars, DEFAULT)
    assert len(merged) == 3
    last = merged[-1]
    assert last.span == [3, 4]
    assert last.high == 11.4          # 向上取两高之高
    assert last.low == 10.5           # 向上取两低之"较高"者
    assert last.direction is Direction.UP


# ── 含处理：向下方向的包含合并取值（向下取两高之"低"）──
def test_down_merge_takes_lower_high():
    bars = _bars([
        (1, 11.0, 12.0),
        (2, 10.5, 11.5),    # 无包含(跌)
        (3, 10.0, 11.0),    # 无包含(跌)
        (4, 10.2, 10.8),    # 被3包含 → 向下合并
    ])
    merged = process_inclusion(bars, DEFAULT)
    last = merged[-1]
    assert last.span == [3, 4]
    assert last.low == 10.0           # 向下取两低之低
    assert last.high == 10.8          # 向下取两高之"较低"者
    assert last.direction is Direction.DOWN


# ── 包含相等三形态（課69:571）：都算包含 ──
def test_equal_forms_are_inclusion():
    # gn=gn-1 且 dn>dn-1
    assert _contains(11.0, 9.0, 11.0, 9.5, eps=0.0)
    # dn=dn-1 且 gn<gn-1
    assert _contains(11.0, 9.0, 10.5, 9.0, eps=0.0)
    # 全等
    assert _contains(11.0, 9.0, 11.0, 9.0, eps=0.0)
    # 真不包含（交叉）
    assert not _contains(11.0, 9.0, 11.5, 9.5, eps=0.0)


# ── 无任何无包含三K组 → 当下无法定向 → 空（PENDING 由上层处理）──
def test_all_inclusive_returns_empty():
    bars = _bars([(1, 9.0, 12.0), (2, 9.5, 11.5), (3, 9.8, 11.0)])  # 层层包含
    assert process_inclusion(bars, DEFAULT) == []


# ── 后复权红线（C1）：前复权配置被拦截 ──
def test_pre_adjust_rejected():
    with pytest.raises(ValueError, match="未来函数"):
        assert_post_adjusted(ChanConfig(adjust="pre"))
    assert_post_adjusted(ChanConfig(adjust="post"))  # 后复权放行
