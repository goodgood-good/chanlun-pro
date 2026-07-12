from __future__ import annotations

from dataclasses import replace

import pytest

from chanlun.decision_support.event_factory import bind_rule_evaluation
from chanlun.decision_support.models import LevelSnapshot
from chanlun.decision_support.rule_cards import (
    FieldResolutionStatus,
    resolve_project_field,
)
from chanlun.decision_support.rule_context import (
    LevelEvaluationFacts,
    RuleRuntimeFacts,
    build_rule_evaluation_context,
)


def _facts(*, latest_price: float | None = 10.0) -> RuleRuntimeFacts:
    return RuleRuntimeFacts(
        fundamental_ok=True,
        comparison_ok=True,
        market_liquid=True,
        risk_allowed=True,
        latest_price=latest_price,
        level_facts=(
            LevelEvaluationFacts(
                frequency="30m",
                level=2,
                completed_bar_count=80,
                latest_bar_closed=True,
            ),
            LevelEvaluationFacts(
                frequency="5m",
                level=1,
                completed_bar_count=120,
                latest_bar_closed=True,
            ),
        ),
    )


def test_context_maps_event_and_runtime_facts_exactly(make_decision_event) -> None:
    event = make_decision_event(price=10.0, stop_below=9.0)

    context = build_rule_evaluation_context(event, _facts())

    assert context["signal"]["bs_type"] == "3buy"
    assert context["signal"]["level"] == 1
    assert context["levels"]["1"]["completed_bar_count"] == 120
    assert context["levels"]["1"]["latest_bar_closed"] is True
    assert context["levels"]["2"]["direction"] == "neutral"
    assert context["market"]["is_tradeable"] is True
    assert context["market"]["liquid"] is True
    assert context["risk"]["allowed"] is True
    assert context["risk"]["stop_breached"] is False


def test_context_missing_runtime_fact_remains_indeterminate(
    make_decision_event,
) -> None:
    event = make_decision_event()
    facts = RuleRuntimeFacts()

    context = build_rule_evaluation_context(event, facts)

    assert resolve_project_field(
        context, "levels.1.completed_bar_count"
    ).status is FieldResolutionStatus.NULL
    assert resolve_project_field(
        context, "risk.stop_breached"
    ).status is FieldResolutionStatus.NULL
    assert resolve_project_field(
        context, "fundamental.ok"
    ).status is FieldResolutionStatus.NULL


def test_context_rejects_ambiguous_numeric_level(make_decision_event) -> None:
    event = make_decision_event()
    duplicate_numeric_level = LevelSnapshot(
        frequency="1m",
        level=1,
        direction="up",
        completed=True,
        segment_start=9.5,
        segment_end=10.0,
        zs_zd=9.6,
        zs_zg=9.9,
    )
    ambiguous = replace(event, levels=(*event.levels, duplicate_numeric_level))

    with pytest.raises(ValueError, match="ambiguous numeric level"):
        build_rule_evaluation_context(ambiguous, _facts())


def test_context_does_not_alias_mutable_runtime_mappings(
    make_decision_event,
) -> None:
    event = make_decision_event()
    source = [
        LevelEvaluationFacts(
            frequency="5m",
            level=1,
            completed_bar_count=120,
            latest_bar_closed=True,
        )
    ]
    facts = RuleRuntimeFacts(level_facts=source)

    context = build_rule_evaluation_context(event, facts)
    source[0] = replace(source[0], completed_bar_count=1)

    assert context["levels"]["1"]["completed_bar_count"] == 120
    with pytest.raises(TypeError):
        context["levels"]["1"]["completed_bar_count"] = 1


def test_context_facts_change_data_fingerprint(make_decision_event) -> None:
    event = make_decision_event()

    first = build_rule_evaluation_context(event, _facts(latest_price=10.0))
    second = build_rule_evaluation_context(event, _facts(latest_price=8.0))

    assert first.data_fingerprint != second.data_fingerprint
    assert first["risk"]["stop_breached"] is False
    assert second["risk"]["stop_breached"] is True


def test_context_input_fingerprint_ignores_storage_data_identity(
    make_decision_event,
) -> None:
    event = make_decision_event()
    rebound = replace(
        event,
        data_fingerprint="sha256:" + "f" * 64,
    )

    first = build_rule_evaluation_context(event, _facts())
    second = build_rule_evaluation_context(rebound, _facts())

    assert (
        first.manual_check_input_fingerprint
        == second.manual_check_input_fingerprint
    )


def test_context_input_fingerprint_survives_rule_binding(
    make_decision_event,
    make_rule_evaluation,
) -> None:
    event = make_decision_event()
    first = build_rule_evaluation_context(event, _facts())
    evaluation = replace(
        make_rule_evaluation(event),
        evaluation_input_fingerprint=first.manual_check_input_fingerprint,
    )

    bound = bind_rule_evaluation(event, evaluation)
    second = build_rule_evaluation_context(bound, _facts())

    assert bound.event_id != event.event_id
    assert bound.data_fingerprint == first.manual_check_input_fingerprint
    assert (
        second.manual_check_input_fingerprint
        == first.manual_check_input_fingerprint
    )


def test_context_does_not_infer_risk_allowed_from_market_tradability(
    make_decision_event,
) -> None:
    event = make_decision_event()
    facts = RuleRuntimeFacts(risk_allowed=None)

    context = build_rule_evaluation_context(event, facts)

    assert context["market"]["is_tradeable"] is True
    assert context["risk"]["allowed"] is None
