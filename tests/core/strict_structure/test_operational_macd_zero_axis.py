"""L103:40-76 selected-period zero-axis risk is not a formal point rule."""

from datetime import datetime, timedelta, timezone

import pytest

from chanlun.core.strict_structure.operational_risk import (
    apply_declared_minimum_operating_frequency,
    macd_zero_axis_operational_state,
)


def test_closed_bar_and_declared_minimum_period_are_independent_inputs():
    base = datetime(2026, 9, 28, 9, 35, tzinfo=timezone.utc)
    bars = (base, base + timedelta(minutes=5), base + timedelta(minutes=10))
    dif, dea = (-0.5, -0.2, 9.0), (-0.4, -0.1, 9.0)
    kwargs = dict(physical_frequency="5m", as_of=bars[-1],
                  closed_through=bars[1],
                  bar_known_times=(bars[1], bars[-1], None))

    observed = macd_zero_axis_operational_state(bars, dif, dea, **kwargs)
    assert observed["indicator_state"] == "both_below_zero"
    assert observed["bar_market_at"] == bars[1]
    assert observed["known_at"] == bars[-1]
    assert observed["risk_filter_state"] == "not_applied_without_declared_minimum_period"
    assert not observed["tradable"] and observed["formal_structure_independent"]
    assert observed["higher_skill_divergence_path_independent"]

    selected = macd_zero_axis_operational_state(
        bars, dif, dea, declared_minimum_frequency="5m", **kwargs,
    )
    assert selected["risk_filter_state"] == "basic_avoid_on_selected_period"
    other = macd_zero_axis_operational_state(
        bars, dif, dea, declared_minimum_frequency="30m", **kwargs,
    )
    assert other["indicator_state"] == "both_below_zero"
    assert other["risk_filter_state"] == "not_applicable_to_other_period"
    premature = macd_zero_axis_operational_state(
        bars, dif, dea, **{**kwargs, "as_of": bars[1]},
    )
    assert premature["risk_filter_state"] == "pending_closed_bar"


def test_zero_contact_and_missing_causal_indicator_do_not_claim_basic_avoid():
    base = datetime(2026, 9, 28, 9, 35, tzinfo=timezone.utc)
    bars = (base, base + timedelta(minutes=5))
    kwargs = dict(physical_frequency="5m", declared_minimum_frequency="5m",
                  as_of=bars[-1], closed_through=bars[-1])
    boundary = macd_zero_axis_operational_state(bars, (-0.2, 0.0), (-0.1, -0.1), **kwargs)
    assert boundary["indicator_state"] == "mixed_or_on_zero"
    assert boundary["zero_axis_boundary"] is True
    assert boundary["risk_filter_state"] == "selected_period_zero_axis_boundary_pending"

    above = macd_zero_axis_operational_state(bars, (0.1, 0.3), (0.2, 0.1), **kwargs)
    assert above["indicator_state"] == "both_above_zero"
    assert above["risk_filter_state"] == "basic_zero_axis_risk_not_triggered"
    missing = macd_zero_axis_operational_state(bars, (0.1,), (0.2, 0.1), **kwargs)
    assert missing["risk_filter_state"] == "pending_indicator_evidence"
    unclosed = macd_zero_axis_operational_state(
        bars, (-0.2, -0.3), (-0.1, -0.4),
        **{**kwargs, "closed_through": None},
    )
    assert unclosed["risk_filter_state"] == "pending_closed_bar"


def test_saved_indicator_fact_only_applies_the_user_period_after_causal_recheck():
    bar = datetime(2026, 9, 28, 9, 35, tzinfo=timezone.utc)
    known = bar + timedelta(minutes=5)
    raw = macd_zero_axis_operational_state(
        (bar,), (-0.2,), (-0.1,), physical_frequency="5m",
        as_of=known, closed_through=bar,
        bar_known_times=(known,),
    )
    fact = {**raw, "bar_market_at": int(bar.timestamp()),
            "known_at": int(known.timestamp())}
    kwargs = dict(physical_frequency="5m", minimum_frequency="5m",
                  source_closed_at=int(known.timestamp()))
    assessed = apply_declared_minimum_operating_frequency(fact, **kwargs)
    assert assessed["risk_filter_state"] == "basic_avoid_on_selected_period"
    assert assessed["declared_minimum_frequency"] == "5m"
    assert fact["declared_minimum_frequency"] is None
    assert apply_declared_minimum_operating_frequency(
        fact, **{**kwargs, "minimum_frequency": "30m"}
    )["risk_filter_state"] == "not_applicable_to_other_period"
    with pytest.raises(ValueError, match="future"):
        apply_declared_minimum_operating_frequency(
            fact, **{**kwargs, "source_closed_at": int(bar.timestamp())}
        )
    with pytest.raises(ValueError, match="contradicts"):
        apply_declared_minimum_operating_frequency(
            {**fact, "dif": 0.2}, **kwargs,
        )
