from __future__ import annotations

from dataclasses import replace
from decimal import Decimal
import json

import pytest

from chanlun.decision_support.corpus_retrieval import CorpusIndex
from chanlun.decision_support.corpus_types import EvidenceUnit, SourceTier
from chanlun.decision_support.evidence import (
    ModelCapabilities,
    build_evidence_packet,
)
from chanlun.decision_support.review_prompt import review_response_schema
from chanlun.decision_support.review_schema import ReviewVerdict, parse_review
from chanlun.decision_support.risk import RiskDecision
from tests.decision_support.conftest import ts


def _unit(evidence_id: str, text: str) -> EvidenceUnit:
    return EvidenceUnit(
        evidence_id=evidence_id,
        source_tier=SourceTier.LESSON_ORIGINAL,
        source_path=f"{evidence_id}.md",
        title=f"3buy {evidence_id}",
        text=text,
        sha256="sha256:" + "7" * 64,
    )


@pytest.fixture
def packet(make_decision_event):
    event = make_decision_event(bs_type="3buy")
    risk = RiskDecision(
        allowed=True,
        shares=500,
        planned_risk_cash=Decimal("500"),
        target_weight=Decimal("0.05"),
        entry_reference=Decimal("10"),
        reasons=(),
        daily_loss_locked=False,
        drawdown_locked=False,
        evaluated_at=ts("2026-07-13T10:35:00+08:00"),
    )
    index = CorpusIndex.build(
        (
            _unit("original-support", "3buy lesson support"),
            _unit("original-counter", "3buy 失效风险与失败条件"),
        )
    )
    value = build_evidence_packet(
        event,
        risk,
        index,
        ModelCapabilities(supports_images=True, supports_json_schema=True),
    )
    assert value.reviewable is True
    return value


def _claim(packet, text: str, *, counter: bool = False) -> dict:
    units = packet.counter_evidence if counter else packet.supporting
    if not counter:
        original = next(
            unit
            for unit in units
            if unit.source_tier is SourceTier.LESSON_ORIGINAL
        )
        project = next(
            unit
            for unit in units
            if unit.source_tier is SourceTier.PROJECT_IMPLEMENTATION
        )
        units = (original, project)
    return {
        "text": text,
        "evidence_ids": [unit.evidence_id for unit in units],
        "source_labels": [unit.source_tier.value for unit in units],
        "supports": not counter,
    }


def _valid_payload(packet, verdict: str = "CONFIRM") -> dict:
    return {
        "verdict": verdict,
        "strategy_track": packet.event.strategy_track.value,
        "summary": _claim(packet, "The frozen structure matches the cited rule."),
        "structure_read": [
            _claim(packet, "The event contains a completed cited structure.")
        ],
        "bull_case": {
            "claims": [_claim(packet, "The support case remains structurally valid.")],
            "conditions": ["The cited support conditions remain present."],
            "rank": 1,
        },
        "bear_case": {
            "claims": [_claim(packet, "The adverse case requires active monitoring.")],
            "conditions": ["The cited adverse conditions require monitoring."],
            "rank": 2,
        },
        "invalidation_checks": [
            _claim(packet, "The cited structural stop is the invalidation boundary.")
        ],
        "counter_evidence": [
            _claim(
                packet,
                "The source also states a failure condition.",
                counter=True,
            )
        ],
        "risk_acknowledged": True,
        "missing_evidence": [],
        "reviewed_event_id": packet.event.event_id,
        "reviewed_data_fingerprint": packet.event.data_fingerprint,
        "reviewed_packet_fingerprint": packet.packet_fingerprint,
    }


def _raw(payload: dict) -> str:
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def test_valid_review_preserves_event_and_packet_fingerprints(packet) -> None:
    review = parse_review(_raw(_valid_payload(packet)), packet)

    assert review.verdict is ReviewVerdict.CONFIRM
    assert review.model_verdict is ReviewVerdict.CONFIRM
    assert review.reviewed_event_id == packet.event.event_id
    assert review.reviewed_data_fingerprint == packet.event.data_fingerprint
    assert review.reviewed_packet_fingerprint == packet.packet_fingerprint
    assert review.summary is not None
    assert review.summary.supports is True
    assert review.bull_case.conditions == (
        "The cited support conditions remain present.",
    )
    assert review.bull_case.rank == 1
    assert review.bear_case.rank == 2
    assert review.validation_errors == ()


@pytest.mark.parametrize("supports", (None, "true", 1, 0))
def test_claim_supports_is_required_strict_boolean(packet, supports) -> None:
    payload = _valid_payload(packet)
    if supports is None:
        del payload["summary"]["supports"]
    else:
        payload["summary"]["supports"] = supports

    review = parse_review(_raw(payload), packet)

    assert review.verdict is ReviewVerdict.ABSTAIN
    assert "invalid_claim_supports" in review.validation_errors


@pytest.mark.parametrize(
    ("case_name", "field_name", "value", "error"),
    (
        ("bull_case", "conditions", None, "invalid_scenario_conditions"),
        ("bull_case", "conditions", "condition", "invalid_scenario_conditions"),
        ("bull_case", "conditions", [1], "invalid_scenario_conditions"),
        ("bear_case", "conditions", [""], "invalid_scenario_conditions"),
        ("bull_case", "rank", None, "invalid_scenario_rank"),
        ("bull_case", "rank", True, "invalid_scenario_rank"),
        ("bull_case", "rank", 0, "invalid_scenario_rank"),
        ("bear_case", "rank", 2.0, "invalid_scenario_rank"),
    ),
)
def test_scenario_conditions_and_rank_are_required_and_typed(
    packet,
    case_name,
    field_name,
    value,
    error,
) -> None:
    payload = _valid_payload(packet)
    if value is None:
        del payload[case_name][field_name]
    else:
        payload[case_name][field_name] = value

    review = parse_review(_raw(payload), packet)

    assert review.verdict is ReviewVerdict.ABSTAIN
    assert error in review.validation_errors


@pytest.mark.parametrize(
    ("case_name", "rank"),
    (("bull_case", 2), ("bear_case", 1), ("bear_case", 3)),
)
def test_scenario_rank_must_match_bull_bear_order(packet, case_name, rank) -> None:
    payload = _valid_payload(packet)
    payload[case_name]["rank"] = rank

    review = parse_review(_raw(payload), packet)

    assert review.verdict is ReviewVerdict.ABSTAIN
    assert "scenario_rank_mismatch" in review.validation_errors
    assert review.bull_case.rank == 1
    assert review.bear_case.rank == 2


@pytest.mark.parametrize("section", ("claim", "scenario"))
def test_nested_review_objects_still_reject_unknown_fields(packet, section) -> None:
    payload = _valid_payload(packet)
    if section == "claim":
        payload["summary"]["confidence"] = "high"
        expected = "invalid_claim_fields"
    else:
        payload["bull_case"]["trade_action"] = "BUY"
        expected = "invalid_scenario_fields"

    review = parse_review(_raw(payload), packet)

    assert review.verdict is ReviewVerdict.ABSTAIN
    assert expected in review.validation_errors


def test_provider_schema_encodes_supports_conditions_and_fixed_ranks(packet) -> None:
    schema = review_response_schema(packet)
    claim = schema["$defs"]["claim"]
    bull = schema["properties"]["bull_case"]
    bear = schema["properties"]["bear_case"]

    assert "supports" in claim["required"]
    assert claim["properties"]["supports"] == {"type": "boolean"}
    assert set(bull["required"]) == {"claims", "conditions", "rank"}
    assert bull["properties"]["rank"]["const"] == 1
    assert bear["properties"]["rank"]["const"] == 2
    assert bull["properties"]["conditions"]["items"]["type"] == "string"


def test_fake_evidence_id_forces_abstain(packet) -> None:
    payload = _valid_payload(packet)
    payload["structure_read"][0]["evidence_ids"][0] = "invented"

    review = parse_review(_raw(payload), packet)

    assert review.verdict is ReviewVerdict.ABSTAIN
    assert "unknown_evidence_id" in review.validation_errors


def test_wrong_source_label_forces_abstain(packet) -> None:
    payload = _valid_payload(packet)
    payload["summary"]["source_labels"][0] = "project_implementation"

    review = parse_review(_raw(payload), packet)

    assert review.verdict is ReviewVerdict.ABSTAIN
    assert "evidence_source_mismatch" in review.validation_errors


def test_unknown_source_label_forces_abstain(packet) -> None:
    payload = _valid_payload(packet)
    payload["summary"]["source_labels"][0] = "internet_summary"

    review = parse_review(_raw(payload), packet)

    assert review.verdict is ReviewVerdict.ABSTAIN
    assert "unknown_source_label" in review.validation_errors


def test_unknown_verdict_forces_abstain(packet) -> None:
    payload = _valid_payload(packet)
    payload["verdict"] = "BUY"

    review = parse_review(_raw(payload), packet)

    assert review.verdict is ReviewVerdict.ABSTAIN
    assert review.model_verdict is None
    assert "unknown_verdict" in review.validation_errors


def test_model_abstain_is_preserved_without_fabricated_validation_error(packet) -> None:
    review = parse_review(_raw(_valid_payload(packet, verdict="ABSTAIN")), packet)

    assert review.verdict is ReviewVerdict.ABSTAIN
    assert review.model_verdict is ReviewVerdict.ABSTAIN
    assert review.validation_errors == ()


@pytest.mark.parametrize("mutation", ("missing", "extra"))
def test_top_level_shape_is_strict(packet, mutation) -> None:
    payload = _valid_payload(packet)
    if mutation == "missing":
        del payload["bear_case"]
        expected = "missing_top_level_fields"
    else:
        payload["trade_instruction"] = "BUY"
        expected = "unexpected_top_level_fields"

    review = parse_review(_raw(payload), packet)

    assert review.verdict is ReviewVerdict.ABSTAIN
    assert expected in review.validation_errors


@pytest.mark.parametrize(
    ("raw", "error"),
    (
        ("not-json", "invalid_json"),
        ("[]", "top_level_not_object"),
        (
            '{"verdict":"CONFIRM","verdict":"REJECT"}',
            "duplicate_json_key",
        ),
    ),
)
def test_parse_failures_are_retained_as_abstain(packet, raw, error) -> None:
    review = parse_review(raw, packet)

    assert review.verdict is ReviewVerdict.ABSTAIN
    assert error in review.validation_errors
    assert review.raw_response == raw


@pytest.mark.parametrize(
    "text",
    ("Invented target is 12.345.", "Invented target is 1e9."),
)
def test_untrusted_numeric_claim_forces_abstain(packet, text) -> None:
    payload = _valid_payload(packet)
    payload["summary"]["text"] = text

    review = parse_review(_raw(payload), packet)

    assert review.verdict is ReviewVerdict.ABSTAIN
    assert "untrusted_numeric_claim" in review.validation_errors


def test_untrusted_numeric_scenario_condition_forces_abstain(packet) -> None:
    payload = _valid_payload(packet)
    payload["bull_case"]["conditions"] = ["Invented target is 12.345."]

    review = parse_review(_raw(payload), packet)

    assert review.verdict is ReviewVerdict.ABSTAIN
    assert "untrusted_numeric_claim" in review.validation_errors


def test_event_and_risk_numeric_values_are_allowed(packet) -> None:
    payload = _valid_payload(packet)
    payload["summary"]["text"] = (
        "Entry 10, stop 9, shares 500, and target weight 5%."
    )

    review = parse_review(_raw(payload), packet)

    assert review.verdict is ReviewVerdict.CONFIRM
    assert review.validation_errors == ()


@pytest.mark.parametrize(
    ("field", "value", "error"),
    (
        ("reviewed_event_id", "event-stale", "stale_event_id"),
        (
            "reviewed_data_fingerprint",
            "sha256:" + "0" * 64,
            "stale_data_fingerprint",
        ),
        (
            "reviewed_packet_fingerprint",
            "sha256:" + "0" * 64,
            "stale_packet_fingerprint",
        ),
        ("strategy_track", "bottom_reversal", "strategy_track_mismatch"),
    ),
)
def test_stale_or_cross_track_response_forces_abstain(
    packet,
    field,
    value,
    error,
) -> None:
    payload = _valid_payload(packet)
    payload[field] = value

    review = parse_review(_raw(payload), packet)

    assert review.verdict is ReviewVerdict.ABSTAIN
    assert error in review.validation_errors


@pytest.mark.parametrize("blocker", ("source_conflict", "image_evidence_unseen"))
def test_packet_blocker_overrides_model_confirmation(packet, blocker) -> None:
    blocked = replace(packet, reviewable=False, blockers=(blocker,))

    review = parse_review(_raw(_valid_payload(blocked)), blocked)

    assert review.verdict is ReviewVerdict.ABSTAIN
    assert blocker in review.validation_errors


def test_risk_not_acknowledged_forces_abstain(packet) -> None:
    payload = _valid_payload(packet)
    payload["risk_acknowledged"] = False

    review = parse_review(_raw(payload), packet)

    assert review.verdict is ReviewVerdict.ABSTAIN
    assert "risk_not_acknowledged" in review.validation_errors


def test_declared_missing_evidence_forces_abstain(packet) -> None:
    payload = _valid_payload(packet)
    payload["missing_evidence"] = ["higher-level source chart"]

    review = parse_review(_raw(payload), packet)

    assert review.verdict is ReviewVerdict.ABSTAIN
    assert "declared_missing_evidence" in review.validation_errors


def test_empty_executable_section_forces_abstain(packet) -> None:
    payload = _valid_payload(packet)
    payload["counter_evidence"] = []

    review = parse_review(_raw(payload), packet)

    assert review.verdict is ReviewVerdict.ABSTAIN
    assert "missing_executable_citations" in review.validation_errors
