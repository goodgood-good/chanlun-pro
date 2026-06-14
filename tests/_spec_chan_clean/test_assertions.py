"""§0.5 A3 门禁 测试。用形式化截断等价断言复核确定性结构（笔/线段）= 单点收口 C1。"""

import pytest

from chan.assertions import assert_truncation_stable, truncation_violations
from chan.bi import find_bis_from_bars
from chan.duan import find_duans_from_bars
from chan.types import Bar


def _turns_bars(turns):
    centers = [turns[0][1]]
    cur = turns[0][1]
    for _k, level, ln in turns[1:]:
        step = (level - cur) / ln
        for _ in range(ln):
            cur += step
            centers.append(round(cur, 4))
        cur = level
    return [Bar(idx=i, dt=None, o=x - 0.5, h=x + 0.5, l=x - 0.5, c=x + 0.5)
            for i, x in enumerate(centers)]


_DEEP_REVERSAL = [("D", 10, 5), ("T", 18, 4), ("D", 12, 6), ("T", 22, 5), ("D", 15, 4),
                  ("T", 26, 6), ("D", 8, 3), ("T", 20, 5), ("D", 13, 4), ("T", 24, 6), ("D", 11, 5)]


def _bi_key(b):
    return (b.start_idx, b.end_idx, int(b.direction), b.high, b.low, b.confirm_bar)


def _duan_key(d):
    return (d.start_bi, d.end_bi, int(d.direction), d.start_value, d.end_value, d.confirm_bar)


# ── 形式化门禁：笔（含对抗 deep_reversal）截断等价通过 ──
def test_bi_truncation_gate():
    bars = _turns_bars(_DEEP_REVERSAL)
    assert_truncation_stable(find_bis_from_bars, bars, _bi_key, min_t=3)


# ── 形式化门禁：线段截断等价通过 ──
def test_duan_truncation_gate():
    bars = _turns_bars([("D", 10, 1), ("T", 13, 5), ("D", 11, 5), ("T", 16, 5), ("D", 13, 5),
                        ("T", 20, 5), ("D", 16, 5), ("T", 18, 5), ("D", 11, 5), ("T", 14, 5),
                        ("D", 6, 5), ("T", 9, 5), ("D", 7, 5), ("T", 13, 5), ("D", 11, 5), ("T", 22, 5)])
    assert_truncation_stable(find_duans_from_bars, bars, _duan_key, min_t=5)


# ── 门禁能抓泄漏：一个故意带未来函数的 compute 被截断断言识破 ──
def test_gate_catches_leak():
    bars = _turns_bars(_DEEP_REVERSAL)

    def leaky(bs):
        # 未来函数：无论喂多少 bar，都返回全量算出的笔 → 截断时提前出现 → 泄漏
        return find_bis_from_bars(bars)

    assert truncation_violations(leaky, bars, _bi_key, min_t=3)  # 非空=抓到泄漏
    with pytest.raises(AssertionError, match="截断等价被破坏"):
        assert_truncation_stable(leaky, bars, _bi_key, min_t=3)
