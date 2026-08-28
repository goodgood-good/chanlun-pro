from __future__ import annotations

from chanlun.decision_support.trading_system.signal_aggregates import (
    POINT_DISTRIBUTION_CONTRACT_ID,
    point_distribution_document,
)


def signal(
    point_type: str,
    *,
    stage: str,
    status: str,
    lock_state: str = "pending",
    entry_allowed: bool = False,
    exit_allowed: bool = False,
) -> dict[str, object]:
    return {
        "point_type": point_type,
        "lifecycle_stage": stage,
        "setup_5m": {
            "status": status,
            "formation_state": "confirmed" if status == "confirmed" else "forming",
            "lock_state": lock_state,
        },
        "entry_allowed": entry_allowed,
        "exit_allowed": exit_allowed,
    }


def test_distribution_separates_candidate_confirmed_locked_and_executable() -> None:
    document = point_distribution_document(
        (
            signal("1buy", stage="approaching", status="provisional"),
            signal("2buy", stage="triggered", status="confirmed"),
            signal(
                "3sell",
                stage="executable",
                status="confirmed",
                lock_state="locked",
                exit_allowed=True,
            ),
            signal("1sell", stage="invalidated", status="confirmed"),
        )
    )

    assert document["contract_id"] == POINT_DISTRIBUTION_CONTRACT_ID
    assert document["trading_level"] == "5m"
    assert document["precision_level"] == "1m"
    assert document["precision_points_included"] is False
    assert document["all_signals"]["total"] == 3
    assert document["candidate"]["counts_by_point_type"]["1buy"] == 1
    assert document["operational_confirmed"]["total"] == 2
    assert document["audit_locked"]["counts_by_point_type"]["3sell"] == 1
    assert document["executable"]["counts_by_point_type"]["3sell"] == 1
    assert document["executable"]["counts_by_point_type"]["2buy"] == 0


def test_legacy_current_stage_is_classified_without_setup_status() -> None:
    document = point_distribution_document(
        (
            {
                "point_type": "2sell",
                "lifecycle_stage": "triggered",
                "setup_5m": {},
                "exit_allowed": True,
            },
        )
    )

    assert document["operational_confirmed"]["counts_by_point_type"]["2sell"] == 1
    assert document["executable"]["total"] == 1
