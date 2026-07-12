from __future__ import annotations

import dataclasses
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
import json
import math

import pytest

from chanlun.decision_support.promotion import (
    ComplianceConfirmation,
    PromotionMetrics,
    PromotionState,
    evaluate_promotion,
)


_CST = timezone(timedelta(hours=8))
_AS_OF = datetime(2026, 7, 14, 18, 0, tzinfo=_CST)
_SHA = {
    "calendar": "sha256:" + "0" * 64,
    "corpus": "sha256:" + "1" * 64,
    "rules": "sha256:" + "2" * 64,
    "algorithm": "sha256:" + "3" * 64,
    "data": "sha256:" + "4" * 64,
}


def _verified_trading_dates(count: int = 20) -> tuple[date, ...]:
    current = date(2026, 6, 1)
    result: list[date] = []
    while len(result) < count:
        if current.weekday() < 5:
            result.append(current)
        current += timedelta(days=1)
    return tuple(result)


def _compliance(
    *,
    expires_at: datetime | None = None,
    tracks: tuple[str, ...] = ("trend_continuation", "bottom_reversal"),
) -> ComplianceConfirmation:
    return ComplianceConfirmation(
        confirmation_id="broker-confirmation-2026-07",
        confirmed_by="operator-001",
        confirmed_at=_AS_OF - timedelta(days=2),
        expires_at=expires_at or _AS_OF + timedelta(days=30),
        tracks=tracks,
    )


def _passing_metrics(**changes: object) -> PromotionMetrics:
    values: dict[str, object] = {
        "evaluated_at": _AS_OF,
        "oos_trades": 100,
        "net_expectancy": 0.0001,
        "profit_factor": 1.100001,
        "max_drawdown": 0.08,
        "event_parity": 1.0,
        "risk_violations": 0,
        "lookahead_events": 0,
        "zero_fill_fake_positions": 0,
        "paper_trading_dates": _verified_trading_dates(),
        "exchange_calendar_verified": True,
        "exchange_calendar_fingerprint": _SHA["calendar"],
        "paper_executable_events": 30,
        "critical_ledger_mismatches": 0,
        "uncited_executable_reviews": 0,
        "restart_recovery": True,
        "corpus_manifest_fingerprints": (_SHA["corpus"],),
        "rule_set_fingerprints": (_SHA["rules"],),
        "algorithm_fingerprints": (_SHA["algorithm"],),
        "data_fingerprints": (_SHA["data"],),
        "compliance_confirmation": _compliance(),
    }
    values.update(changes)
    return PromotionMetrics(**values)


def test_default_metrics_fail_closed_with_explicit_paper_gate_pending() -> None:
    decision = evaluate_promotion("trend_continuation", PromotionMetrics())

    assert decision.promoted is False
    assert decision.state is PromotionState.RESEARCH
    assert decision.paper_gate_pending is True
    assert "paper_gate_pending" in decision.reasons
    assert "oos_trades_unknown" in decision.reasons
    assert "paper_trading_dates_unknown" in decision.reasons
    assert "compliance_confirmation_missing" in decision.reasons
    json.dumps(decision.to_dict(), allow_nan=False, sort_keys=True)


def test_exact_promotion_boundaries_allow_small_cap_manual_only() -> None:
    decision = evaluate_promotion(
        "trend_continuation",
        _passing_metrics(),
    )

    assert decision.promoted is True
    assert decision.state is PromotionState.SMALL_CAP_MANUAL
    assert decision.paper_gate_pending is False
    assert decision.reasons == ()
    payload = decision.to_dict()
    assert payload["state"] == "small_cap_manual"
    assert "auto_live" not in json.dumps(payload, sort_keys=True)


def test_each_oos_threshold_fails_with_deterministic_reason() -> None:
    cases = (
        ("oos_trades", 99, "insufficient_oos_trades"),
        ("net_expectancy", 0.0, "non_positive_expectancy"),
        ("profit_factor", 1.1, "profit_factor_not_above_1_1"),
        ("max_drawdown", 0.0800001, "drawdown_above_8_percent"),
        ("event_parity", 0.999, "event_parity_not_100_percent"),
    )

    for field_name, value, reason in cases:
        decision = evaluate_promotion(
            "trend_continuation",
            replace(_passing_metrics(), **{field_name: value}),
        )
        assert decision.promoted is False
        assert reason in decision.reasons

def test_safety_and_recovery_gates_must_be_exactly_clean() -> None:
    cases = (
        ("risk_violations", 1, "risk_violation"),
        ("lookahead_events", 1, "lookahead_event"),
        ("zero_fill_fake_positions", 1, "zero_fill_fake_position"),
        (
            "critical_ledger_mismatches",
            1,
            "critical_ledger_mismatch",
        ),
        (
            "uncited_executable_reviews",
            1,
            "uncited_executable_review",
        ),
        ("restart_recovery", False, "restart_recovery_failed"),
    )

    for field_name, value, reason in cases:
        decision = evaluate_promotion(
            "trend_continuation",
            replace(_passing_metrics(), **{field_name: value}),
        )
        assert decision.promoted is False
        assert reason in decision.reasons

    uncited = evaluate_promotion(
        "trend_continuation",
        replace(_passing_metrics(), uncited_executable_reviews=1),
    )
    assert uncited.state is PromotionState.RESEARCH
    assert uncited.paper_gate_pending is True


def test_paper_gate_requires_20_dates_and_30_executable_events() -> None:
    for metrics, reason in (
        (
            replace(
                _passing_metrics(),
                paper_trading_dates=_verified_trading_dates(19),
            ),
            "insufficient_paper_trading_days",
        ),
        (
            replace(_passing_metrics(), paper_executable_events=29),
            "insufficient_paper_executable_events",
        ),
    ):
        decision = evaluate_promotion("trend_continuation", metrics)
        assert decision.promoted is False
        assert decision.state is PromotionState.PAPER
        assert decision.paper_gate_pending is True
        assert reason in decision.reasons
        assert "paper_gate_pending" in decision.reasons


def test_duplicate_or_unverified_trading_dates_never_pass() -> None:
    dates = _verified_trading_dates()
    duplicate = replace(
        _passing_metrics(),
        paper_trading_dates=dates + (dates[-1],),
    )
    unverified = replace(
        _passing_metrics(),
        exchange_calendar_verified=False,
    )

    duplicate_decision = evaluate_promotion("trend_continuation", duplicate)
    unverified_decision = evaluate_promotion("trend_continuation", unverified)

    assert "duplicate_paper_trading_date" in duplicate_decision.reasons
    assert duplicate_decision.paper_gate_pending is True
    assert "paper_trading_dates_unverified" in unverified_decision.reasons
    assert unverified_decision.paper_gate_pending is True


def test_unknown_and_non_finite_metrics_never_pass_or_leak_nan_to_json() -> None:
    cases = (
        ("net_expectancy", None, "net_expectancy_unknown"),
        ("profit_factor", math.nan, "profit_factor_not_finite"),
        ("max_drawdown", math.inf, "max_drawdown_not_finite"),
        ("event_parity", -math.inf, "event_parity_not_finite"),
    )

    for field_name, value, reason in cases:
        decision = evaluate_promotion(
            "trend_continuation",
            replace(_passing_metrics(), **{field_name: value}),
        )
        assert decision.promoted is False
        assert reason in decision.reasons
        json.dumps(decision.to_dict(), allow_nan=False, sort_keys=True)


def test_missing_or_mixed_fingerprint_versions_never_pass() -> None:
    missing = replace(
        _passing_metrics(),
        corpus_manifest_fingerprints=None,
    )
    mixed = replace(
        _passing_metrics(),
        algorithm_fingerprints=(
            _SHA["algorithm"],
            "sha256:" + "9" * 64,
        ),
        corpus_manifest_fingerprints=(_SHA["corpus"], _SHA["corpus"]),
        rule_set_fingerprints=(_SHA["rules"], _SHA["rules"]),
        data_fingerprints=(_SHA["data"], _SHA["data"]),
    )

    missing_decision = evaluate_promotion("trend_continuation", missing)
    mixed_decision = evaluate_promotion("trend_continuation", mixed)

    assert "corpus_manifest_fingerprint_missing" in missing_decision.reasons
    assert "algorithm_fingerprint_mixed" in mixed_decision.reasons
    assert missing_decision.promoted is False
    assert mixed_decision.promoted is False


def test_invalid_fingerprint_or_evidence_count_mismatch_never_passes() -> None:
    invalid = replace(
        _passing_metrics(),
        data_fingerprints=("not-a-sha256",),
    )
    mismatched = replace(
        _passing_metrics(),
        data_fingerprints=(_SHA["data"], _SHA["data"]),
    )

    invalid_decision = evaluate_promotion("trend_continuation", invalid)
    mismatched_decision = evaluate_promotion("trend_continuation", mismatched)

    assert "data_fingerprint_invalid" in invalid_decision.reasons
    assert (
        "fingerprint_evidence_count_mismatch"
        in mismatched_decision.reasons
    )


def test_compliance_confirmation_must_exist_cover_track_and_be_unexpired() -> None:
    missing = replace(_passing_metrics(), compliance_confirmation=None)
    expired = replace(
        _passing_metrics(),
        compliance_confirmation=_compliance(expires_at=_AS_OF),
    )
    wrong_track = replace(
        _passing_metrics(),
        compliance_confirmation=_compliance(tracks=("bottom_reversal",)),
    )

    assert "compliance_confirmation_missing" in evaluate_promotion(
        "trend_continuation", missing
    ).reasons
    assert "compliance_confirmation_expired" in evaluate_promotion(
        "trend_continuation", expired
    ).reasons
    assert "compliance_confirmation_track_mismatch" in evaluate_promotion(
        "trend_continuation", wrong_track
    ).reasons


def test_missing_evaluation_time_fails_closed() -> None:
    decision = evaluate_promotion(
        "trend_continuation",
        replace(_passing_metrics(), evaluated_at=None),
    )

    assert decision.promoted is False
    assert "evaluation_time_unknown" in decision.reasons


def test_tampered_nested_confirmation_still_fails_closed() -> None:
    confirmation = _compliance()
    object.__setattr__(confirmation, "expires_at", "not-a-datetime")
    object.__setattr__(confirmation, "tracks", (["trend_continuation"],))

    decision = evaluate_promotion(
        "trend_continuation",
        replace(_passing_metrics(), compliance_confirmation=confirmation),
    )

    assert decision.promoted is False
    assert "compliance_confirmation_invalid" in decision.reasons
    json.dumps(decision.to_dict(), allow_nan=False, sort_keys=True)


def test_metrics_are_immutable_and_copy_mutable_sequences() -> None:
    dates = list(_verified_trading_dates())
    metrics = _passing_metrics(paper_trading_dates=dates)
    dates.pop()

    assert len(metrics.paper_trading_dates or ()) == 20
    assert isinstance(metrics.paper_trading_dates, tuple)
    with pytest.raises(dataclasses.FrozenInstanceError):
        metrics.oos_trades = 101


def test_evaluation_is_deterministic_and_track_state_is_independent() -> None:
    metrics = _passing_metrics()

    first = evaluate_promotion("trend_continuation", metrics)
    second = evaluate_promotion("trend_continuation", metrics)
    other = evaluate_promotion(
        "bottom_reversal",
        replace(
            metrics,
            compliance_confirmation=_compliance(
                tracks=("bottom_reversal",),
            ),
        ),
    )

    assert first == second
    assert first.to_dict() == second.to_dict()
    assert first.track == "trend_continuation"
    assert other.track == "bottom_reversal"
    assert first.metrics_fingerprint == second.metrics_fingerprint
