"""tests/signal_monitor/test_evaluator.py — 中枢-free 信号评估器单测。

策略：grade_signal 用手搓信号做确定性断言；detect/evaluate 用合成 K 线
验证「机器正确性」（产出类型/字段/分级/过滤），不臆断合成噪声上的缠论裁决。
"""
from __future__ import annotations

import pytest

from chanlun.signal_monitor.cl_signal import (
    DIRECTION_BEARISH,
    DIRECTION_BULLISH,
    GRADE_A,
    GRADE_B,
    GRADE_C,
    SIGNAL_KIND_BI_BEICHI,
    SIGNAL_KIND_BI_BREAK,
    SIGNAL_KIND_XD_BEICHI,
    SIGNAL_KINDS,
    ClSignal,
)
from chanlun.signal_monitor.evaluator import (
    ClSignalEvaluator,
    EvaluatorConfig,
    grade_signal,
)
from chanlun.signal_monitor.strength_compare import StrengthCompareResult

LADDER = ["d", "30m", "5m"]


# ----------------------- EvaluatorConfig -----------------------

def test_config_rejects_operation_level_not_in_ladder():
    with pytest.raises(ValueError):
        EvaluatorConfig(operation_level="60m", level_ladder=LADDER)


def test_config_high_and_sub_level():
    cfg = EvaluatorConfig(operation_level="30m", level_ladder=LADDER)
    assert cfg.high_level == "d"
    assert cfg.sub_level == "5m"


def test_config_high_none_when_operation_is_top():
    cfg = EvaluatorConfig(operation_level="d", level_ladder=LADDER)
    assert cfg.high_level is None
    assert cfg.sub_level == "30m"


def test_config_sub_none_when_operation_is_bottom():
    cfg = EvaluatorConfig(operation_level="5m", level_ladder=LADDER)
    assert cfg.sub_level is None


# ----------------------- grade_signal -----------------------

def _strength(score: int) -> StrengthCompareResult:
    return StrengthCompareResult(
        is_beichi=True, made_new_extreme=True, direction="down",
        macd_area_ratio=0.5, macd_peak_ratio=0.5, strength_score=score,
    )


def _signal(**kw) -> ClSignal:
    base = dict(
        market="a", code="X", name="x", operation_level="30m",
        signal_kind=SIGNAL_KIND_BI_BEICHI, direction=DIRECTION_BULLISH,
        line_stable_key="2024-01-01 10:00:00|down",
    )
    base.update(kw)
    return ClSignal(**base)


def test_grade_a_for_xd_beichi():
    """线段背驰 —— 实测最强信号 → A 级。"""
    s = _signal(signal_kind=SIGNAL_KIND_XD_BEICHI, strength=_strength(50))
    grade_signal(s)
    assert s.score == 78  # 78 + round((50-50)/10)=0
    assert s.grade == GRADE_A


def test_grade_b_for_bi_break():
    """笔不创新高/低 —— 实测稳健微正 → B 级。"""
    s = _signal(signal_kind=SIGNAL_KIND_BI_BREAK, strength=None)
    grade_signal(s)
    assert s.score == 62
    assert s.grade == GRADE_B


def test_grade_c_for_bi_beichi():
    """笔背驰 —— 实测无边际优势 → C 级。"""
    s = _signal(signal_kind=SIGNAL_KIND_BI_BEICHI, strength=_strength(50))
    grade_signal(s)
    assert s.score == 40
    assert s.grade == GRADE_C


def test_grade_strength_micro_adjust():
    """背驰强度只做 ±5 内微调，不改变由类型锚定的 grade。"""
    strong = _signal(signal_kind=SIGNAL_KIND_XD_BEICHI, strength=_strength(100))
    weak = _signal(signal_kind=SIGNAL_KIND_XD_BEICHI, strength=_strength(0))
    grade_signal(strong)
    grade_signal(weak)
    assert strong.score == 83  # 78 + 5
    assert weak.score == 73    # 78 - 5
    assert strong.grade == weak.grade == GRADE_A


def test_grade_monotonic_by_kind():
    """重建后分级与实测预测力一致：A(xd_beichi) > B(bi_break) > C(bi_beichi)。"""
    a = grade_signal(_signal(signal_kind=SIGNAL_KIND_XD_BEICHI, strength=_strength(50)))
    b = grade_signal(_signal(signal_kind=SIGNAL_KIND_BI_BREAK, strength=None))
    c = grade_signal(_signal(signal_kind=SIGNAL_KIND_BI_BEICHI, strength=_strength(50)))
    assert a.score > b.score > c.score
    assert (a.grade, b.grade, c.grade) == (GRADE_A, GRADE_B, GRADE_C)


# ----------------------- detect / evaluate -----------------------

def test_detect_returns_valid_signals(cl_with_synthetic_klines):
    cd = cl_with_synthetic_klines(220, multi_freq=True)
    ev = ClSignalEvaluator("a", "TEST.001", "测试")
    for s in ev.detect_beichi_signals(cd, "30m") + ev.detect_structure_signals(cd, "30m"):
        assert isinstance(s, ClSignal)
        assert s.signal_kind in SIGNAL_KINDS
        assert s.direction in (DIRECTION_BULLISH, DIRECTION_BEARISH)
        assert s.operation_level == "30m"


def _three_level_cds(factory):
    return {
        "d": factory(200, multi_freq=True, seed=1),
        "30m": factory(220, multi_freq=True, seed=2),
        "5m": factory(240, multi_freq=True, seed=3),
    }


def test_evaluate_end_to_end(cl_with_synthetic_klines):
    cds = _three_level_cds(cl_with_synthetic_klines)
    cfg = EvaluatorConfig(operation_level="30m", level_ladder=LADDER)
    signals = ClSignalEvaluator("a", "TEST.001", "测试").evaluate(cds, cfg)
    assert isinstance(signals, list)
    for s in signals:
        assert s.operation_level == "30m"
        assert s.grade in (GRADE_A, GRADE_B, GRADE_C)
        assert 0 <= s.score <= 100
        assert s.identity  # 去重键非空


def test_evaluate_min_grade_filters(cl_with_synthetic_klines):
    cds = _three_level_cds(cl_with_synthetic_klines)
    ev = ClSignalEvaluator("a", "TEST.001", "测试")
    all_sigs = ev.evaluate(cds, EvaluatorConfig("30m", LADDER, min_grade=GRADE_C))
    a_only = ev.evaluate(cds, EvaluatorConfig("30m", LADDER, min_grade=GRADE_A))
    assert len(a_only) <= len(all_sigs)
    for s in a_only:
        assert s.grade == GRADE_A


def test_evaluate_missing_operation_level_returns_empty():
    ev = ClSignalEvaluator("a", "X")
    assert ev.evaluate({}, EvaluatorConfig("30m", LADDER)) == []
