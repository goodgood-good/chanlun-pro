"""真实行情数据 A3 无未来函数验收（用仓库内 wtpy 期货 K 线 CSV）。

★ 这是合成 fixture 测不出的 C1 实证（真实噪声数据）：
  - 笔 = **筐一截断等价**稳定（含处理/分型/笔 确定性；fenxing confirm_bar 修复后真实数据 0 违规）。
  - 线段 = **筐三可回溯**（課78:19 逆时间确认，"done 段可变"，不满足筐一），但**无前向泄漏**
    （已确认结构不引用未来 bar）= 满足无未来函数下界。

诚实标注（A-38）：CSV 非 parquet（A-22 浮点精度待 parquet fixture 升级）；期货非 A股后复权。
本测试验"无未来函数"机制，非验信号收益。
"""

import csv
import os

import pytest

from chan.assertions import (
    assert_no_forward_leak,
    assert_truncation_stable,
    forward_leak_violations,
    truncation_violations,
)
from chan.bi import find_bis_from_bars
from chan.duan import find_duans_from_bars
from chan.types import Bar

_CSV = os.path.join(
    os.path.dirname(__file__), "..", "..", "src", "cl_wtpy", "storage", "csv",
    "CFFEX.IF.HOT_m5.csv",
)


def _load(n):
    if not os.path.exists(_CSV):
        pytest.skip("真实数据 CSV 不在（仓库未含 wtpy storage）")
    rows = []
    with open(_CSV, encoding="utf-8") as f:
        rd = csv.reader(f)
        next(rd)
        for r in rd:
            if len(r) < 6:
                continue
            rows.append((float(r[2]), float(r[3]), float(r[4]), float(r[5])))
            if len(rows) >= n:
                break
    return [Bar(idx=i, dt=None, o=o, h=h, l=lo, c=c) for i, (o, h, lo, c) in enumerate(rows)]


_BI_KEY = lambda b: (b.start_idx, b.end_idx, int(b.direction), b.high, b.low, b.confirm_bar)  # noqa: E731


# ── 笔：真实数据上**筐一截断等价**稳定（fenxing confirm_bar=start_idx 修复钉死）──
def test_bi_truncation_stable_on_real_data():
    bars = _load(300)
    assert len(bars) >= 200, "真实数据样本不足"
    assert_truncation_stable(find_bis_from_bars, bars, _BI_KEY, min_t=3)


# ── 笔 + 线段：真实数据上**无前向泄漏**（筐三/最小 C1，不引用未来 bar）──
def test_no_forward_leak_on_real_data():
    bars = _load(300)
    assert_no_forward_leak(find_bis_from_bars, bars, min_t=3)
    assert_no_forward_leak(find_duans_from_bars, bars, min_t=5)
    # 线段端点价只用 ≤confirm_bar 的 bar
    duans = [d for d in find_duans_from_bars(bars) if d.end_bar >= 0]
    assert all(d.end_bar <= d.confirm_bar for d in duans)


# ── 诚实记录：线段在真实数据上**不**满足筐一（可回溯/done段可变，課78:19）──
def test_duan_is_revisable_not_basket1():
    """这不是 bug，是线段逆时间确认的本性；故 C1 用筐三(无前向泄漏)而非筐一验线段。"""
    bars = _load(300)
    dk = lambda d: (d.start_bi, d.end_bi, int(d.direction), d.start_value, d.end_value, d.confirm_bar)  # noqa: E731
    # 真实噪声数据下线段截断等价(筐一)预期**有**违规（可回溯）；前向泄漏(筐三)应为 0。
    assert forward_leak_violations(find_duans_from_bars, bars, min_t=5) == []
    # 若某日线段竟在该样本筐一全稳，本断言会失败提醒复核口径——属预期外的好事，照实暴露。
    assert len(truncation_violations(find_duans_from_bars, bars, dk, min_t=5)) > 0
