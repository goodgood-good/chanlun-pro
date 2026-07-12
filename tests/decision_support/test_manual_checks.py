from __future__ import annotations

from dataclasses import FrozenInstanceError, replace
from datetime import timedelta

import pytest

from chanlun.decision_support.manual_checks import ManualCheckSnapshot
from chanlun.decision_support.rule_context import (
    LevelEvaluationFacts,
    RuleRuntimeFacts,
    build_rule_evaluation_context,
)


_CHECK_ID = "chart.structure_confirmed"
_EVIDENCE_IDS = ("evidence:lesson-20-main",)


def _facts() -> RuleRuntimeFacts:
    return RuleRuntimeFacts(
        fundamental_ok=True,
        comparison_ok=True,
        market_liquid=True,
        risk_allowed=True,
        latest_price=10.0,
        level_facts=(
            LevelEvaluationFacts("30m", 2, 80, True),
            LevelEvaluationFacts("5m", 1, 120, True),
        ),
    )


def _snapshot(event, bound_fingerprint: str, **changes) -> ManualCheckSnapshot:
    values = {
        "manual_check_id": _CHECK_ID,
        "value": True,
        "operator_id": "operator.lc",
        "recorded_at": event.observed_at + timedelta(seconds=1),
        "event_id": event.event_id,
        "context_fingerprint": bound_fingerprint,
        "evidence_ids": _EVIDENCE_IDS,
    }
    values.update(changes)
    return ManualCheckSnapshot(**values)


def test_manual_check_snapshot_is_strict_immutable_audit_record(
    make_decision_event,
) -> None:
    event = make_decision_event()
    snapshot = _snapshot(event, "sha256:" + "a" * 64)

    assert snapshot.evidence_ids == _EVIDENCE_IDS
    assert snapshot.fingerprint.startswith("sha256:")
    with pytest.raises(FrozenInstanceError):
        snapshot.value = False


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"value": 1}, "value must be boolean"),
        ({"operator_id": ""}, "operator_id must be a non-empty string"),
        ({"recorded_at": None}, "recorded_at must be a datetime"),
        ({"event_id": ""}, "event_id must be a non-empty string"),
        ({"context_fingerprint": "forged"}, "context_fingerprint"),
        ({"evidence_ids": ()}, "evidence_ids must contain"),
        (
            {"evidence_ids": ("evidence:a", "evidence:a")},
            "evidence_ids contains duplicates",
        ),
    ],
)
def test_manual_check_snapshot_rejects_incomplete_audit_fields(
    make_decision_event,
    changes,
    message,
) -> None:
    event = make_decision_event()

    with pytest.raises(ValueError, match=message):
        _snapshot(event, "sha256:" + "a" * 64, **changes)


def test_rule_runtime_facts_reject_bare_boolean_manual_checks() -> None:
    with pytest.raises(
        ValueError,
        match="manual_checks must contain ManualCheckSnapshot",
    ):
        RuleRuntimeFacts(manual_checks={_CHECK_ID: True})


def test_rule_runtime_facts_reject_manual_snapshot_subclass(
    make_decision_event,
) -> None:
    class ForgedManualCheckSnapshot(ManualCheckSnapshot):
        pass

    event = make_decision_event()
    forged = ForgedManualCheckSnapshot(
        manual_check_id=_CHECK_ID,
        value=True,
        operator_id="operator.forged",
        recorded_at=event.observed_at,
        event_id=event.event_id,
        context_fingerprint="sha256:" + "a" * 64,
        evidence_ids=_EVIDENCE_IDS,
    )

    with pytest.raises(
        ValueError,
        match="manual_checks must contain ManualCheckSnapshot",
    ):
        RuleRuntimeFacts(manual_checks=(forged,))


def test_context_accepts_only_snapshot_bound_to_current_event_and_input(
    make_decision_event,
) -> None:
    event = make_decision_event()
    base_facts = _facts()
    base_context = build_rule_evaluation_context(event, base_facts)
    snapshot = _snapshot(event, base_context.manual_check_input_fingerprint)

    context = build_rule_evaluation_context(
        event,
        replace(base_facts, manual_checks=(snapshot,)),
    )

    assert context.manual_check_audit.snapshots == (snapshot,)
    assert context.manual_check_audit.event_id == event.event_id
    assert (
        context.manual_check_audit.context_fingerprint
        == base_context.manual_check_input_fingerprint
    )
    assert context.data_fingerprint != base_context.data_fingerprint


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"event_id": "event:stale"}, "manual check event_id mismatch"),
        (
            {"context_fingerprint": "sha256:" + "b" * 64},
            "manual check context fingerprint mismatch",
        ),
    ],
)
def test_context_rejects_stale_or_cross_event_manual_check(
    make_decision_event,
    changes,
    message,
) -> None:
    event = make_decision_event()
    facts = _facts()
    base_context = build_rule_evaluation_context(event, facts)
    snapshot = _snapshot(
        event,
        base_context.manual_check_input_fingerprint,
        **changes,
    )

    with pytest.raises(ValueError, match=message):
        build_rule_evaluation_context(
            event,
            replace(facts, manual_checks=(snapshot,)),
        )


def test_context_rejects_manual_check_recorded_before_event(
    make_decision_event,
) -> None:
    event = make_decision_event()
    facts = _facts()
    base_context = build_rule_evaluation_context(event, facts)
    snapshot = _snapshot(
        event,
        base_context.manual_check_input_fingerprint,
        recorded_at=event.observed_at - timedelta(microseconds=1),
    )

    with pytest.raises(ValueError, match="manual check predates event"):
        build_rule_evaluation_context(
            event,
            replace(facts, manual_checks=(snapshot,)),
        )


def test_context_rejects_duplicate_manual_check_identifiers(
    make_decision_event,
) -> None:
    event = make_decision_event()
    facts = _facts()
    base_context = build_rule_evaluation_context(event, facts)
    first = _snapshot(event, base_context.manual_check_input_fingerprint)
    second = replace(first, operator_id="operator.reviewer")

    with pytest.raises(ValueError, match="duplicate manual_check_id"):
        build_rule_evaluation_context(
            event,
            replace(facts, manual_checks=(first, second)),
        )
