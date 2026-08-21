from dataclasses import replace
from datetime import timedelta

import pytest

from chanlun.core.strict_structure.current_events import TerminalSegmentReference
from chanlun.decision_support.trading_system.five_minute_setup_state import (
    FIVE_MINUTE_SETUP_STATE_CONTRACT,
    canonical_setup_state_document,
    classify_five_minute_setup_state,
    execution_recommendation_label,
    validate_setup_state_document,
)
from chanlun.decision_support.trading_system.human_assisted_decision import (
    point_decision_document,
)
from tests.trading_system.helpers import confirmed_point, provisional_point


def test_forming_and_geometry_ready_provisional_states_are_distinct() -> None:
    forming = classify_five_minute_setup_state(
        point_type="3sell",
        status="provisional",
        evidence_codes=("live_first_return",),
        missing_conditions=("terminal_unit_locked",),
    )
    geometry_ready = classify_five_minute_setup_state(
        point_type="3sell",
        status="provisional",
        evidence_codes=(
            "unfinished_segment_participates",
            "provisional_center_completion",
            "core_boundary_held",
        ),
        missing_conditions=("unfinished_segment_lock",),
    )

    assert forming.formation_state == "forming"
    assert forming.lock_state == "pending"
    assert forming.contains_forming_segment is True
    assert forming.contains_unlocked_segment is True
    assert geometry_ready.formation_state == "geometry_ready"
    assert geometry_ready.lock_state == "pending"
    assert geometry_ready.contains_forming_segment is False
    assert geometry_ready.contains_unlocked_segment is True
    assert geometry_ready.actionable is False


def test_blocked_execution_label_uses_actionable_plain_language() -> None:
    label = execution_recommendation_label("BLOCKED")

    assert label == "当前不满足操作条件，等待结构或数据恢复"
    assert "硬条件" not in label


def test_geometry_evidence_cannot_promote_a_non_third_class_candidate() -> None:
    state = classify_five_minute_setup_state(
        point_type="2sell",
        status="provisional",
        evidence_codes=(
            "provisional_center_completion",
            "core_boundary_held",
        ),
        missing_conditions=("terminal_unit_locked",),
    )

    assert state.formation_state == "forming"
    assert state.actionable is False


def test_terminal_segment_lineage_overrides_legacy_evidence_classification() -> None:
    completed_base = provisional_point("2sell")
    completed = replace(
        completed_base,
        terminal_segment=TerminalSegmentReference(
            role="latest_completed",
            structural_level=completed_base.recursive_level,
            unit_id="segment:latest-completed",
            source_kind="segment",
            direction="up",
            state="formed",
            market_start=completed_base.anchor_at - timedelta(minutes=30),
            market_end=completed_base.anchor_at,
            available_at=completed_base.available_at,
        ),
    )
    unfinished_base = replace(
        provisional_point("3buy"),
        evidence_codes=(
            "unfinished_segment_participates",
            "provisional_center_completion",
            "core_boundary_held",
        ),
    )
    unfinished = replace(
        unfinished_base,
        terminal_segment=TerminalSegmentReference(
            role="latest_unfinished",
            structural_level=unfinished_base.recursive_level,
            unit_id="segment:latest-unfinished",
            source_kind="segment",
            direction="down",
            state="forming",
            market_start=unfinished_base.anchor_at - timedelta(minutes=30),
            market_end=unfinished_base.anchor_at,
            available_at=unfinished_base.available_at,
        ),
    )

    completed_document = point_decision_document(completed)
    unfinished_document = point_decision_document(unfinished)

    assert completed_document["formation_state"] == "geometry_ready"
    assert completed_document["contains_forming_segment"] is False
    assert completed_document["contains_unlocked_segment"] is True
    assert unfinished_document["formation_state"] == "forming"
    assert unfinished_document["contains_forming_segment"] is True
    assert unfinished_document["contains_unlocked_segment"] is True
    validate_setup_state_document(completed_document)
    validate_setup_state_document(unfinished_document)


def test_terminal_segment_document_rejects_contradictory_role_and_state() -> None:
    with pytest.raises(ValueError, match="terminal segment lineage"):
        canonical_setup_state_document(
            {
                "point_type": "3buy",
                "status": "provisional",
                "evidence_codes": ["provisional_center_completion"],
                "missing_conditions": ["terminal_unit_locked"],
                "terminal_segment_role": "latest_completed",
                "terminal_segment_state": "forming",
            }
        )


def test_confirmed_point_is_locked_and_actionable() -> None:
    document = point_decision_document(confirmed_point("3buy"))

    assert document["state_contract"] == FIVE_MINUTE_SETUP_STATE_CONTRACT
    assert document["formation_state"] == "confirmed"
    assert document["lock_state"] == "locked"
    assert document["contains_forming_segment"] is False
    assert document["contains_unlocked_segment"] is False
    assert document["contains_unfinished_segment"] is False
    assert document["actionable"] is True
    validate_setup_state_document(document)


def test_geometry_confirmed_point_is_actionable_while_audit_lock_is_pending() -> None:
    base = confirmed_point("1buy")
    point = replace(
        base,
        evidence_codes=(
            *base.evidence_codes,
            "geometry_confirmed_before_audit_lock",
        ),
        terminal_segment=TerminalSegmentReference(
            role="latest_completed",
            structural_level=base.recursive_level,
            unit_id="segment:geometry-confirmed",
            source_kind="segment",
            direction="down",
            state="formed",
            market_start=base.anchor_at - timedelta(minutes=30),
            market_end=base.anchor_at,
            available_at=base.available_at,
        ),
    )

    document = point_decision_document(point)

    assert document["formation_state"] == "confirmed"
    assert document["lock_state"] == "pending"
    assert document["contains_unlocked_segment"] is True
    assert document["contains_forming_segment"] is False
    assert document["actionable"] is True
    validate_setup_state_document(document)


def test_geometry_candidate_document_keeps_unlocked_tail_unfinished() -> None:
    point = replace(
        provisional_point("3sell"),
        evidence_codes=(
            "unfinished_segment_participates",
            "provisional_center_completion",
            "core_boundary_held",
        ),
        missing_conditions=(
            "unfinished_segment_lock",
            "formal_center_confirmation",
        ),
    )

    document = point_decision_document(point)

    assert document["formation_state"] == "geometry_ready"
    assert document["lock_state"] == "pending"
    assert document["contains_forming_segment"] is False
    assert document["contains_unlocked_segment"] is True
    assert document["contains_unfinished_segment"] is True
    assert document["actionable"] is False
    validate_setup_state_document(document)


def test_canonical_projection_repairs_legacy_derived_fields_but_validation_rejects_tampering() -> None:
    legacy = {
        "point_type": "3buy",
        "status": "provisional",
        "evidence_codes": [
            "unfinished_segment_participates",
            "provisional_center_completion",
            "core_boundary_held",
        ],
        "missing_conditions": ["unfinished_segment_lock"],
        "formation_state": "forming",
        "actionable": True,
    }

    canonical = canonical_setup_state_document(legacy)

    assert canonical["formation_state"] == "geometry_ready"
    assert canonical["actionable"] is False
    validate_setup_state_document(canonical)
    tampered = {**canonical, "formation_state": "forming"}
    with pytest.raises(ValueError, match="formation_state"):
        validate_setup_state_document(tampered)
