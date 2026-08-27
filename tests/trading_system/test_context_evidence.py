from __future__ import annotations

from dataclasses import replace
from datetime import timedelta
from decimal import Decimal

from chanlun.decision_support.trading_system.context_evidence import (
    SamePeriodTechnicalContext,
    SignalContextAssessment,
    assess_signal_context,
    signal_context_risk_scale,
)
from tests.trading_system.helpers import AS_OF, neutral_context


def _evidence(
    frequency: str,
    *,
    bullish: bool,
) -> SamePeriodTechnicalContext:
    return SamePeriodTechnicalContext(
        frequency=frequency,  # type: ignore[arg-type]
        observed_at=AS_OF,
        source_bar_count=30,
        close=12.0 if bullish else 8.0,
        ma5=11.0 if bullish else 9.0,
        ma10=10.0,
        close_vs_ma5="above" if bullish else "below",
        ma5_vs_ma10="ma5_above_ma10" if bullish else "ma5_below_ma10",
        ma_cross="golden" if bullish else "death",
        consecutive_closes_vs_ma5=3 if bullish else -3,
        fractal_type="bottom" if bullish else "top",
        fractal_state="pen_locked",
        fractal_anchor_at=AS_OF - timedelta(minutes=2),
        fractal_confirmed_at=AS_OF - timedelta(minutes=1),
        fractal_price=7.5 if bullish else 12.5,
        latest_pen_direction="up" if bullish else "down",
        latest_pen_locked=True,
        reason_codes=(),
    )


def test_third_buy_uses_daily_and_thirty_minute_ma_alignment_as_grade_only() -> None:
    assessment = assess_signal_context(
        side="buy",
        point_type="3buy",
        daily_evidence=_evidence("d", bullish=True),
        thirty_minute_evidence=_evidence("30m", bullish=True),
        daily_structure=neutral_context("d"),
        thirty_minute_structure=neutral_context("30m"),
    )

    assert assessment.grade == "A"
    assert assessment.daily_stance == "supportive"
    assert assessment.thirty_minute_stance == "supportive"
    assert assessment.document()["ma_and_fractal_can_define_signal"] is False
    assert assessment.document()["weekly_or_monthly_used"] is False


def test_first_buy_does_not_use_ma_alignment_as_a_confirmation_rule() -> None:
    adverse_ma_with_supportive_structure = replace(
        _evidence("d", bullish=True),
        close=8.0,
        ma5=9.0,
        ma10=10.0,
        close_vs_ma5="below",
        ma5_vs_ma10="ma5_below_ma10",
        ma_cross="death",
        consecutive_closes_vs_ma5=-3,
    )
    assessment = assess_signal_context(
        side="buy",
        point_type="1buy",
        daily_evidence=adverse_ma_with_supportive_structure,
        thirty_minute_evidence=replace(
            adverse_ma_with_supportive_structure,
            frequency="30m",
        ),
        daily_structure=neutral_context("d"),
        thirty_minute_structure=neutral_context("30m"),
    )

    assert assessment.grade == "A"
    assert "D_MA_CONTEXT_ONLY_FOR_FIRST_POINT" in assessment.reason_codes
    assert not any("MA5_OPPOSES" in code for code in assessment.reason_codes)


def test_sell_context_is_the_directional_mirror_of_buy_context() -> None:
    assessment = assess_signal_context(
        side="sell",
        point_type="3sell",
        daily_evidence=_evidence("d", bullish=False),
        thirty_minute_evidence=_evidence("30m", bullish=False),
        daily_structure=neutral_context("d"),
        thirty_minute_structure=neutral_context("30m"),
    )

    assert assessment.grade == "A"
    assert assessment.daily_stance == "supportive"
    assert assessment.thirty_minute_stance == "supportive"


def test_third_point_cannot_receive_a_grade_when_ma10_is_unavailable() -> None:
    incomplete_daily = replace(
        _evidence("d", bullish=True),
        ma10=None,
        ma5_vs_ma10="unresolved",
        ma_cross="unresolved",
    )
    assessment = assess_signal_context(
        side="buy",
        point_type="3buy",
        daily_evidence=incomplete_daily,
        thirty_minute_evidence=_evidence("30m", bullish=True),
        daily_structure=neutral_context("d"),
        thirty_minute_structure=neutral_context("30m"),
    )

    assert assessment.grade == "B"
    assert "D_MA5_MA10_REQUIRED_FOR_THIRD_POINT" in assessment.reason_codes


def test_context_document_maps_each_physical_period_directly_to_5m_and_1m() -> None:
    document = _evidence("d", bullish=True).document()

    assert document["lower_confirmation_frequencies"] == ["5m", "1m"]
    assert "weekly" not in document
    assert "monthly" not in document


def test_context_risk_scale_is_shared_and_unresolved_is_fail_small() -> None:
    assert signal_context_risk_scale(None) == Decimal("0.50")
    assert signal_context_risk_scale(
        SignalContextAssessment("A", "supportive", "supportive", ())
    ) == Decimal("1.00")
    assert signal_context_risk_scale(
        SignalContextAssessment("B", "neutral", "supportive", ())
    ) == Decimal("0.75")
    assert signal_context_risk_scale(
        SignalContextAssessment("C", "adverse", "adverse", ())
    ) == Decimal("0.50")
