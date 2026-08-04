from __future__ import annotations

from dataclasses import asdict
from datetime import datetime, timedelta
from decimal import Decimal
import json
from zoneinfo import ZoneInfo

import pytest

from chanlun.decision_support.fingerprints import sha256_json
from chanlun.decision_support.trading_system.v3_human_review_screening import (
    HumanReviewAlert,
    human_review_screening_parameters,
    load_human_review_feedback_ledger,
)
from chanlun.decision_support.trading_system.v3_technical_approximation import (
    technical_approximation_parameters,
)
from tools.record_v3_human_review import main


CN = ZoneInfo("Asia/Shanghai")


def _alert() -> HumanReviewAlert:
    available = datetime(2026, 7, 28, 14, 59, tzinfo=CN)
    return HumanReviewAlert(
        symbol="SH.600000",
        alert_type="POSSIBLE_30M_BUY",
        signal_at=available - timedelta(minutes=1),
        review_available_at=available,
        source_point_id="sha256:" + "1" * 64,
        structure_snapshot_id="sha256:" + "2" * 64,
        sector_id="qmt-gics3:test",
        confidence="MEDIUM",
        review_priority=50,
        reference_price=Decimal("10"),
        structural_invalidation_price=Decimal("9"),
        market_risk_gate="GREEN",
        sector_risk_gate="GREEN",
        symbol_risk_gate="GREEN",
        warning_codes=(),
        source_fact_ids=("sha256:" + "3" * 64,),
        screening_parameter_set_id=(
            human_review_screening_parameters().parameter_set_id
        ),
        technical_approximation_parameter_set_id=(
            technical_approximation_parameters().parameter_set_id
        ),
    )


def _jsonable(value: object) -> object:
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_jsonable(item) for item in value]
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Decimal):
        return format(value, "f")
    return value


def _screen(alert: HumanReviewAlert) -> dict[str, object]:
    row = {
        **_jsonable(asdict(alert)),
        "candidate_id": alert.candidate_id,
        "signal_lifecycle_id": alert.signal_lifecycle_id,
    }
    payload: dict[str, object] = {
        "schema": "chanlun-v3-human-review-screen/v1",
        "data_grade": "HUMAN_REVIEW_SCREENING",
        "review_queue": [row],
        "portfolio_backtest_performed": False,
        "portfolio_performance_evaluable": False,
        "orders_created": 0,
        "fills_created": 0,
        "positions_created": 0,
        "automated_order_authorized": False,
        "human_confirmation_required": True,
        "highest_status": "REVIEW_REQUIRED",
        "live_status": "LIVE_DISABLED",
    }
    payload["content_sha256"] = sha256_json(payload)
    return payload


def test_cli_records_review_but_never_creates_a_trade(tmp_path) -> None:
    alert = _alert()
    candidate_id = alert.candidate_id
    screen_path = tmp_path / "screen.json"
    ledger_path = tmp_path / "feedback.json"
    screen_path.write_text(
        json.dumps(_screen(alert), ensure_ascii=False),
        encoding="utf-8",
    )

    result = main(
        (
            "--screen-report",
            str(screen_path),
            "--ledger",
            str(ledger_path),
            "--candidate-id",
            candidate_id,
            "--reviewer",
            "reviewer-a",
            "--reviewed-at",
            datetime(2026, 7, 28, 15, 0, tzinfo=CN).isoformat(),
            "--center",
            "UNCERTAIN",
            "--trend",
            "CONSOLIDATION",
            "--level",
            "30M",
            "--point",
            "UNCERTAIN",
            "--disposition",
            "WATCH",
        )
    )

    ledger = load_human_review_feedback_ledger(ledger_path)
    assert result == 0
    assert len(ledger["entries"]) == 1
    assert ledger["entries"][0]["candidate_id"] == candidate_id
    assert ledger["automated_order_authorized"] is False
    assert ledger["live_status"] == "LIVE_DISABLED"


def test_cli_rejects_feedback_before_review_evidence_is_available(
    tmp_path,
) -> None:
    alert = _alert()
    screen_path = tmp_path / "screen.json"
    ledger_path = tmp_path / "feedback.json"
    screen_path.write_text(json.dumps(_screen(alert)), encoding="utf-8")

    with pytest.raises(ValueError, match="predates available evidence"):
        main(
            (
                "--screen-report",
                str(screen_path),
                "--ledger",
                str(ledger_path),
                "--candidate-id",
                alert.candidate_id,
                "--reviewer",
                "reviewer-a",
                "--reviewed-at",
                (
                    alert.review_available_at - timedelta(seconds=1)
                ).isoformat(),
                "--center",
                "CONFIRMED",
                "--trend",
                "UP",
                "--level",
                "30M",
                "--point",
                "BUY_3",
                "--disposition",
                "PAPER_OBSERVE",
            )
        )
    assert not ledger_path.exists()


def test_cli_rejects_future_review_timestamp(tmp_path) -> None:
    alert = _alert()
    screen_path = tmp_path / "screen.json"
    ledger_path = tmp_path / "feedback.json"
    screen_path.write_text(json.dumps(_screen(alert)), encoding="utf-8")
    observed_now = alert.review_available_at + timedelta(minutes=1)

    with pytest.raises(ValueError, match="cannot be in the future"):
        main(
            (
                "--screen-report",
                str(screen_path),
                "--ledger",
                str(ledger_path),
                "--candidate-id",
                alert.candidate_id,
                "--reviewer",
                "reviewer-a",
                "--reviewed-at",
                (observed_now + timedelta(seconds=1)).isoformat(),
                "--center",
                "CONFIRMED",
                "--trend",
                "UP",
                "--level",
                "30M",
                "--point",
                "BUY_3",
                "--disposition",
                "PAPER_OBSERVE",
            ),
            clock=observed_now,
        )
    assert not ledger_path.exists()


def test_cli_rejects_a_tampered_source_screen(tmp_path) -> None:
    alert = _alert()
    candidate_id = alert.candidate_id
    screen = _screen(alert)
    screen["orders_created"] = 1
    stable = dict(screen)
    stable.pop("content_sha256")
    screen["content_sha256"] = sha256_json(stable)
    screen_path = tmp_path / "screen.json"
    screen_path.write_text(json.dumps(screen), encoding="utf-8")

    with pytest.raises(ValueError, match="human_review_report_boundary_invalid"):
        main(
            (
                "--screen-report",
                str(screen_path),
                "--candidate-id",
                candidate_id,
                "--reviewer",
                "reviewer-a",
                "--center",
                "UNCERTAIN",
                "--trend",
                "UNCERTAIN",
                "--level",
                "UNCERTAIN",
                "--point",
                "UNCERTAIN",
                "--disposition",
                "NEEDS_MORE_DATA",
            )
        )
