"""Phase 0 递归骨架契约 测试：protocols / state / indicators / resample。

重点：
  - 下游可见性铁律（confirmed_only 经 ChanState 暴露）；
  - 状态转移合法性守卫；
  - 指标/聚合的**因果性**（截断等价 = C1，EMA/聚合只用 ≤t）。
"""

import pytest

from chan.indicators import ema, macd
from chan.protocols import Structure, assert_legal_transition
from chan.resample import resample
from chan.state import ChanState
from chan.types import (
    Bar,
    Bi,
    ChanStatus,
    Direction,
    Fenxing,
    FxType,
)


def _bar(i, lo, hi):
    return Bar(idx=i, dt=None, o=lo, h=hi, l=lo, c=hi)


def _fx(k, fx_type, status=ChanStatus.CONFIRMED):
    return Fenxing(fx_type=fx_type, k_idx=k, high=k + 1.0, low=float(k),
                   confirm_bar=k + 1, status=status)


# ── protocols：结构对象满足 Structure 形状（带 status + confirm_bar）──
def test_structures_satisfy_protocol():
    fx = _fx(3, FxType.DING)
    bi = Bi(direction=Direction.UP, start_fx=_fx(0, FxType.DI), end_fx=fx,
            high=5.0, low=0.0, confirm_bar=4)
    assert isinstance(fx, Structure)
    assert isinstance(bi, Structure)


# ── protocols：状态转移合法性守卫 ──
def test_legal_transition_guard():
    # 合法：PROVISIONAL --falsified--> INVALIDATED
    assert_legal_transition(ChanStatus.PROVISIONAL, "falsified", ChanStatus.INVALIDATED)
    # 非法：CONFIRMED 不能再被证伪
    with pytest.raises(ValueError, match="非法状态转移"):
        assert_legal_transition(ChanStatus.CONFIRMED, "falsified", ChanStatus.INVALIDATED)


# ── state：不可变 + 版本化（evolve 产新对象，原对象不变）──
def test_state_is_immutable_and_versioned():
    s0 = ChanState()
    assert s0.version == 0 and s0.last_bar_idx == -1
    bars = (_bar(0, 1, 2), _bar(1, 2, 3))
    s1 = s0.evolve(bars=bars)
    assert s1.version == 1 and s1.last_bar_idx == 1
    # 原对象未被修改（不可变）
    assert s0.version == 0 and s0.bars == ()
    assert s1.bars == bars


# ── state：confirmed() 过滤非 CONFIRMED（下游可见性铁律）──
def test_state_confirmed_filters_pending():
    fxs = (
        _fx(1, FxType.DING, ChanStatus.CONFIRMED),
        _fx(2, FxType.DI, ChanStatus.PENDING),
        _fx(3, FxType.DING, ChanStatus.PROVISIONAL),
    )
    s = ChanState().evolve(levels={0: fxs})
    visible = s.confirmed(0)
    assert len(visible) == 1 and visible[0].k_idx == 1
    assert len(s.all_structures(0)) == 3   # 调试视图含全部


def test_state_truncated_bars():
    bars = tuple(_bar(i, i, i + 1) for i in range(6))
    s = ChanState().evolve(bars=bars)
    assert [b.idx for b in s.truncated_bars(3)] == [0, 1, 2, 3]


# ── indicators：EMA 基本性质 + 因果截断等价（C1）──
def test_ema_basic():
    assert ema([], 3) == []
    assert ema([5.0], 3) == [5.0]
    out = ema([1.0, 2.0, 3.0], 1)   # α=1 → 跟随当前值
    assert out == [1.0, 2.0, 3.0]


def test_ema_macd_causal_truncation_equivalence():
    closes = [10, 11, 10.5, 12, 13, 12.5, 14, 13.5, 15, 16, 15.5, 17, 18, 17, 19, 20]
    full_dif, full_dea, full_hist = macd(closes)
    for t in range(1, len(closes) + 1):
        d, e, h = macd(closes[:t])
        # 截断到 t 的指标 == 全量指标的前 t 个（EMA 因果 → 无未来函数）
        assert d == pytest.approx(full_dif[:t])
        assert e == pytest.approx(full_dea[:t])
        assert h == pytest.approx(full_hist[:t])


# ── resample：OHLC 聚合 + 未收满棒丢弃 + 因果前缀 ──
def test_resample_aggregates_ohlc():
    bars = [_bar(0, 1, 4), _bar(1, 2, 5), _bar(2, 0, 3),   # 桶1
            _bar(3, 3, 6), _bar(4, 1, 7), _bar(5, 2, 4)]   # 桶2
    out = resample(bars, factor=3)
    assert len(out) == 2
    assert (out[0].o, out[0].h, out[0].l, out[0].c, out[0].idx) == (1, 5, 0, 3, 2)
    assert (out[1].o, out[1].h, out[1].l, out[1].c, out[1].idx) == (3, 7, 1, 4, 5)


def test_resample_drops_incomplete_tail():
    bars = [_bar(i, i, i + 1) for i in range(7)]   # 7 根、factor=3 → 2 满桶 + 1 残桶
    full = resample(bars, factor=3, drop_incomplete=True)
    assert len(full) == 2                            # 残桶(idx6)被丢弃
    kept = resample(bars, factor=3, drop_incomplete=False)
    assert len(kept) == 3                            # 不丢则保留残桶


def test_resample_causal_prefix():
    bars = [_bar(i, i, i + 1) for i in range(12)]
    full = resample(bars, factor=3)
    # 喂到第 t 根时的高周期棒 = 全量高周期棒里收盘 idx≤t 的前缀（C1）
    for t in range(len(bars)):
        trunc = resample(bars[: t + 1], factor=3)
        expected = [b for b in full if b.idx <= t]
        assert [(b.idx, b.o, b.h, b.l, b.c) for b in trunc] == \
               [(b.idx, b.o, b.h, b.l, b.c) for b in expected]
