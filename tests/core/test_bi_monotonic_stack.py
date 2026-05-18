"""tests/core/test_bi_monotonic_stack.py — 笔端点单调栈算法测试。

覆盖:
- 单元: _build_endpoint_stack 的弹出 / 不弹 / 同类压缩 / 栈不足四条路径。
- 回归: 513100 真实 1m 行情, 验证 14:27 修复 + 09:31 保持原著。
- 增量等价: 513100 分批喂入与全量 process_klines 的 cl_snapshot 一致性。

fixture 用 parquet (非 csv): csv 往返的 ~4e-16 浮点噪声会让缠论包含处理
的笔结果漂移, parquet 二进制存 float64 可 bit-exact 复现生产数据。
"""
from __future__ import annotations

import pathlib

import pandas as pd
import pytest

from tests.core.conftest import DEFAULT_CL_CONFIG, cl_snapshot
from chanlun.core.bi_calculator import BiCalculator
from chanlun.core.cl import CL
from chanlun.core.cl_interface import CLKline, FX

_FIXTURE = (
    pathlib.Path(__file__).resolve().parent.parent
    / "fixtures" / "klines" / "a_SH_513100_1m.parquet"
)


# --------------------------------------------------------------------------
# 单元测试: _build_endpoint_stack
# --------------------------------------------------------------------------
def _mk_fx(fx_type: str, cl_index: int, val: float) -> FX:
    """构造一个只填了 type / val / k.index 的最小 FX (单调栈只用这三者)。"""
    k = CLKline(
        k_index=cl_index, date=None,
        h=val, l=val, o=val, c=val, a=0.0,
        klines=[], index=cl_index, _n=1,
    )
    return FX(_type=fx_type, k=k, klines=[], val=val, index=0)


def _endpoints(fxs):
    """跑单调栈, 返回 (type, cl_index, val) 三元组列表, 便于断言。"""
    bc = BiCalculator(bi_mode="strict")
    stack = bc._build_endpoint_stack(fxs)
    return [(fx.type, fx.k.index, fx.val) for fx in stack]


def test_pop_redundant_endpoint():
    """情形③末分支: 栈顶被新分型与其同类前驱夹击 → 弹出栈顶与 prev。

    F2(ding95) 不比 A=F0(ding100) 高 (非真顶), F3(di70) 比 prev=F1(di85) 低,
    → 处理 F3 时弹出 F2、F1, F3 与 F0 成笔。
    """
    fxs = [
        _mk_fx("ding", 0, 100.0),
        _mk_fx("di", 10, 85.0),
        _mk_fx("ding", 20, 95.0),
        _mk_fx("di", 23, 70.0),
    ]
    assert _endpoints(fxs) == [("ding", 0, 100.0), ("di", 23, 70.0)]


def test_keep_real_endpoint():
    """情形③丢弃分支: 栈顶是创新高的真端点 → 丢弃新分型。

    F2(ding100) 比 A=F0(ding90) 高 (真顶), 故 F3 够不着 F2 时直接丢弃 F3。
    """
    fxs = [
        _mk_fx("ding", 0, 90.0),
        _mk_fx("di", 10, 80.0),
        _mk_fx("ding", 20, 100.0),
        _mk_fx("di", 23, 70.0),
    ]
    assert _endpoints(fxs) == [
        ("ding", 0, 90.0), ("di", 10, 80.0), ("ding", 20, 100.0),
    ]


def test_same_type_compression():
    """情形①: 连续同类分型保留更极端者。

    F1、F2 同为 di, F2 更低 → F2 取代 F1。
    """
    fxs = [
        _mk_fx("ding", 0, 100.0),
        _mk_fx("di", 10, 85.0),
        _mk_fx("di", 14, 80.0),
        _mk_fx("ding", 24, 99.0),
    ]
    assert _endpoints(fxs) == [
        ("ding", 0, 100.0), ("di", 14, 80.0), ("ding", 24, 99.0),
    ]


def test_stack_too_short_discards():
    """情形③ R1: 栈元素 < 3 无法判定 → 丢弃不成笔的新分型。

    F2 与 F1 距离 2 (<4) 不成笔, 此时栈只有 [F0,F1] → 丢弃 F2。
    """
    fxs = [
        _mk_fx("ding", 0, 100.0),
        _mk_fx("di", 10, 85.0),
        _mk_fx("ding", 12, 95.0),
    ]
    assert _endpoints(fxs) == [("ding", 0, 100.0), ("di", 10, 85.0)]


# --------------------------------------------------------------------------
# 真实数据回归: 513100 1m
# --------------------------------------------------------------------------
def _load_513100() -> CL:
    if not _FIXTURE.exists():
        pytest.skip(f"缺少 fixture: {_FIXTURE}")
    df = pd.read_parquet(_FIXTURE)
    cd = CL("SH.513100", "1m", dict(DEFAULT_CL_CONFIG))
    cd.process_klines(df)
    return cd


def test_513100_1427_endpoint_corrected():
    """14:27 真 bug: 下降笔端点应取真实最低 14:27 / 2.0850。

    伪顶 ding14:23 遮挡, 未修复代码错误地把端点定在 14:15 / 2.0900。
    """
    cd = _load_513100()
    lo = pd.Timestamp("2026-05-15 14:20:00+08:00")
    hi = pd.Timestamp("2026-05-15 14:35:00+08:00")
    downs = [
        b for b in cd.get_bis()
        if b.type == "down" and lo <= b.end.k.date <= hi
    ]
    assert len(downs) == 1, f"预期 14:20~14:35 有且仅有一笔 down 终点, 实际 {len(downs)}"
    bi = downs[0]
    assert bi.end.k.date == pd.Timestamp("2026-05-15 14:27:00+08:00")
    assert abs(bi.end.val - 2.0850) < 1e-6
    # 负向回归: 14:15 是修复前 bug 的错误端点, 修复后不应再是任何笔的起点或终点
    wrong = pd.Timestamp("2026-05-15 14:15:00+08:00")
    assert all(
        b.start.k.date != wrong and b.end.k.date != wrong
        for b in cd.get_bis()
    ), "14:15 不应再是任何笔的端点（修复前 bug 的错误端点）"


def test_513100_0931_keeps_yuanzhu():
    """09:31 非 bug: 隔夜跳空致顶底共用 K 线, 笔保持原著行为。

    本测试断言的下降笔区间是 14:53 → 09:36。函数名中的 09:31 指这一笔
    内部、05-18 开盘隔夜跳空形成的更低底 (2.0630): 它与上游真顶 ding14:53
    的顶分型共用同一根缠论 K 线, 按原著 [118]「共用 K 线不算一笔」无法成为
    笔端点, 故笔仍终于 09:36 / 2.0700。
    反向回归: 防止算法过度修复把 09:31 也「修」成笔端点。
    """
    cd = _load_513100()
    start = pd.Timestamp("2026-05-15 14:53:00+08:00")
    downs = [
        b for b in cd.get_bis()
        if b.type == "down" and b.start.k.date == start
    ]
    assert len(downs) == 1, f"预期起点 14:53 有且仅有一笔 down, 实际 {len(downs)}"
    bi = downs[0]
    assert bi.end.k.date == pd.Timestamp("2026-05-18 09:36:00+08:00")
    assert abs(bi.end.val - 2.0700) < 1e-6


def test_513100_incremental_equals_full():
    """513100 1m: 分批喂入与一次性全量, cl_snapshot 必须完全一致。

    单调栈 _build_endpoint_stack 是无跨调用状态的全量重放; 增量路径
    _try_incremental_extend 命中后仍调用 _rebuild_from_fxs, 结果应与
    一次性全量 process_klines 完全相同。
    """
    if not _FIXTURE.exists():
        pytest.skip(f"缺少 fixture: {_FIXTURE}")
    df = pd.read_parquet(_FIXTURE)

    cd_full = CL("SH.513100", "1m", dict(DEFAULT_CL_CONFIG))
    cd_full.process_klines(df)

    cd_batch = CL("SH.513100", "1m", dict(DEFAULT_CL_CONFIG))
    n = len(df)
    step = max(1, n // 12)
    for upto in range(step, n + 1, step):
        cd_batch.process_klines(df.iloc[:upto])
    cd_batch.process_klines(df)  # 确保末根被喂入

    assert cl_snapshot(cd_batch) == cl_snapshot(cd_full)
