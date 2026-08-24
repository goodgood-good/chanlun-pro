from __future__ import annotations

import copy
from decimal import Decimal

from chanlun.decision_support.trading_system import live_human_review as core_review
from chanlun.decision_support.trading_system.position_recommendation import (
    build_position_recommendation,
)
from cl_app.services.live_review_runtime_contract import (
    displayed_decision_evidence_is_consistent,
    install_web_live_review_runtime_contract,
)
from tests.trading_system.test_live_human_review import live_snapshot


def _check(signal: dict[str, object], *, compatible: bool) -> bool:
    snapshot = live_snapshot()
    policy = snapshot["decision_core"]["policy"]
    risk = signal["higher_timeframe_risk"]
    warmup = signal["warmup"]
    check = (
        displayed_decision_evidence_is_consistent
        if compatible
        else core_review._chanlun_web_original_displayed_decision_check
    )
    return check(signal, policy=policy, risk=risk, warmup=warmup)


def _blocked_position(signal: dict[str, object]) -> dict[str, object]:
    setup = signal["setup_5m"]
    return build_position_recommendation(
        side=str(signal["side"]),
        recommendation="BLOCKED",
        risk_multiplier=str(signal["risk_multiplier"]),
        context_risk_scale=str(signal["execution_profile"]["context_risk_scale"]),
        entry_price=setup["anchor_price"],
        structural_stop=setup["invalidation_price"],
        exit_action="none",
    ).document()


def test_unconfirmed_sell_matches_source_auditor_without_account_advisory() -> None:
    source = live_snapshot()
    signal = copy.deepcopy(next(row for row in source["signals"] if row["side"] == "sell"))
    signal["point_type"] = "3sell"
    signal["setup_5m"].update(
        point_type="3sell",
        status="provisional",
        confirmed_at=None,
        evidence_codes=[
            "unfinished_segment_participates",
            "provisional_center_completion",
            "core_boundary_held",
        ],
        missing_conditions=["unfinished_segment_lock"],
        formation_state="geometry_ready",
        lock_state="pending",
        contains_forming_segment=False,
        contains_unlocked_segment=True,
        contains_unfinished_segment=True,
        actionable=False,
    )
    signal["lifecycle_stage"] = "formed"
    signal["decision_reasons"] = [
        "five_minute_geometry_candidate_awaiting_confirmation",
        "SAME_PERIOD_CONTEXT_GRADE_UNRESOLVED",
    ]
    profile = signal["execution_profile"]
    profile.update(
        structure_signal_confirmed=False,
        one_minute_segment_difference_present=False,
        segment_difference_status="STRUCTURE_PENDING",
        segment_difference_ready=False,
        precise_execution_ready=False,
        recommendation="GEOMETRY_AWAITING_CONFIRMATION",
        recommendation_label="5分钟买卖点仅为几何候选，尚未达到操作确认",
        hard_blocked=False,
        hard_block_reason_codes=[],
        advisory_reason_codes=["SAME_PERIOD_CONTEXT_GRADE_UNRESOLVED"],
    )
    position = build_position_recommendation(
        side="sell",
        recommendation="GEOMETRY_AWAITING_CONFIRMATION",
        risk_multiplier="0",
        context_risk_scale="0.50",
        entry_price=signal["setup_5m"]["anchor_price"],
        structural_stop=signal["setup_5m"]["invalidation_price"],
        exit_action="none",
    ).document()
    signal["position_recommendation"] = position
    profile["position_recommendation"] = position

    assert _check(signal, compatible=False) is True
    assert _check(signal, compatible=True) is True
    assert profile["recommendation"] == "GEOMETRY_AWAITING_CONFIRMATION"


def test_invalidated_sell_preserves_nonactionable_cause_without_adapter() -> None:
    source = live_snapshot()
    signal = copy.deepcopy(next(row for row in source["signals"] if row["side"] == "sell"))
    signal["lifecycle_stage"] = "invalidated"
    signal["decision_reasons"] = [
        "lifecycle_not_actionable",
        "SAME_PERIOD_CONTEXT_GRADE_UNRESOLVED",
        "structure_invalidated",
    ]
    profile = signal["execution_profile"]
    profile.update(
        one_minute_segment_difference_present=False,
        segment_difference_status="WAITING_ONE_MINUTE",
        segment_difference_ready=False,
        precise_execution_ready=False,
        recommendation="BLOCKED",
        recommendation_label="当前不满足操作条件，等待结构或数据恢复",
        hard_blocked=True,
        hard_block_reason_codes=["structure_invalidated"],
        advisory_reason_codes=["SAME_PERIOD_CONTEXT_GRADE_UNRESOLVED"],
    )
    position = _blocked_position(signal)
    signal["position_recommendation"] = position
    profile["position_recommendation"] = position

    assert _check(signal, compatible=False) is True
    assert _check(signal, compatible=True) is True
    assert signal["decision_reasons"][0] == "lifecycle_not_actionable"


def test_invalidated_buy_may_retain_already_observed_segment_expiry_advisory() -> None:
    source = live_snapshot()
    signal = copy.deepcopy(next(row for row in source["signals"] if row["side"] == "buy"))
    signal["lifecycle_stage"] = "invalidated"
    signal["technical_entry_allowed"] = False
    signal["entry_allowed"] = False
    signal["decision_reasons"] = [
        "lifecycle_not_actionable",
        "one_minute_not_confirmed",
        "SAME_PERIOD_CONTEXT_GRADE_UNRESOLVED",
        "ONE_MINUTE_SEGMENT_BOUNDARY_EXPIRED",
        "structure_invalidated",
    ]
    profile = signal["execution_profile"]
    profile.update(
        one_minute_segment_difference_present=False,
        segment_difference_status="WAITING_ONE_MINUTE",
        segment_difference_ready=False,
        precise_execution_ready=False,
        recommendation="BLOCKED",
        recommendation_label="当前不满足操作条件，等待结构或数据恢复",
        hard_blocked=True,
        hard_block_reason_codes=["structure_invalidated"],
        advisory_reason_codes=[
            "SAME_PERIOD_CONTEXT_GRADE_UNRESOLVED",
            "ONE_MINUTE_SEGMENT_BOUNDARY_EXPIRED",
        ],
    )
    position = _blocked_position(signal)
    signal["position_recommendation"] = position
    profile["position_recommendation"] = position

    assert Decimal(str(signal["risk_multiplier"])) > 0
    assert _check(signal, compatible=False) is False
    assert _check(signal, compatible=True) is True
    assert "ONE_MINUTE_SEGMENT_BOUNDARY_EXPIRED" in profile["advisory_reason_codes"]


def test_runtime_adapter_still_rejects_unrelated_safety_mutation() -> None:
    source = live_snapshot()
    signal = copy.deepcopy(source["signals"][0])
    signal["execution_profile"]["automated_order_authorized"] = True

    assert _check(signal, compatible=True) is False
    install_web_live_review_runtime_contract()
    assert (
        core_review._displayed_decision_evidence_is_consistent
        is displayed_decision_evidence_is_consistent
    )
