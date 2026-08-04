from __future__ import annotations

from chanlun.decision_support.trading_system.v3_timeframe_alignment import (
    independent_alignment_contract,
)
from tools.backtest_chanlun_v3_independent_timeframes import build_report


def inputs() -> dict[str, object]:
    core = {"core_contract_sha256": "sha256:core"}
    return {
        "baseline": {"core_contract": core},
        "current_core": core,
        "structure": {
            "timeframe_point_streams_available": True,
            "entry_alignment_status": "CERTIFIED_CAUSAL_ALIGNMENT",
            "entry_alignment_parameter_set_id": (
                independent_alignment_contract().parameter_set_id
            ),
            "aligned_entry_chain_count": 0,
            "alignment_decisions": [
                {
                    "l0_point_id": "sha256:l0",
                    "window_end": "2019-04-17T11:00:00+08:00",
                    "status": "REJECT",
                    "reason_codes": [
                        "NO_COMPLETED_L1_UP_DEPARTURE_IN_L0_CONFIRMATION_WINDOW"
                    ],
                }
            ],
            "alignment_rejection_counts": {
                "NO_COMPLETED_L1_UP_DEPARTURE_IN_L0_CONFIRMATION_WINDOW": 1
            },
            "entry_fact_counts": {
                "l0_first_center_third_buy": 2,
                "l1_confirmed_points": 20,
                "l2_first_or_second_buy": 5,
            },
            "source_start": "2018-01-01",
            "source_end": "2023-01-01",
            "source_sessions": 1000,
            "content_sha256": "sha256:structure",
        },
        "data_acceptance": {
            "membership_snapshot_scope": "EXPLORATORY_ONLY",
            "data_grade": "COMPONENT_ONLY",
        },
    }


def test_certified_zero_entry_alignment_produces_measured_cash_result() -> None:
    report = build_report(**inputs())

    statuses = {gate["gate"]: gate["status"] for gate in report["gates"]}
    assert statuses["user_override_independent_timeframe_mapping"] == "PASS"
    assert statuses["independent_timeframe_confirmed_point_streams"] == "PASS"
    assert statuses["causal_l0_l1_l2_entry_alignment"] == "PASS"
    assert report["first_failed_gate"] is None
    assert report["evaluation_status"] == "EVALUATED_COMPONENT_ZERO_ENTRY"
    assert report["performance"]["total_return"] == 0.0
    assert report["performance"]["maximum_drawdown"] == 0.0
    assert report["full_system_return_evaluation_allowed"] is False
    assert report["live_status"] == "LIVE_DISABLED"


def test_missing_independent_streams_fail_before_alignment() -> None:
    values = inputs()
    values["structure"] = {
        **values["structure"],
        "timeframe_point_streams_available": False,
    }

    report = build_report(**values)

    assert report["first_failed_gate"]["gate"] == (
        "independent_timeframe_confirmed_point_streams"
    )
    assert report["first_failed_gate"]["status"] == (
        "INSUFFICIENT_STRUCTURE_FACTS"
    )
