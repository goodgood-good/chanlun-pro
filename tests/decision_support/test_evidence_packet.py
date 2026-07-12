from __future__ import annotations

from dataclasses import replace
from decimal import Decimal

import pytest

from chanlun.decision_support.corpus_retrieval import CorpusIndex
from chanlun.decision_support.corpus_types import (
    EvidenceUnit,
    ImageEvidence,
    SourceTier,
)
from chanlun.decision_support.evidence import (
    ModelCapabilities,
    RuleEvidenceBinding,
    build_evidence_packet,
)
from chanlun.decision_support.risk import RiskDecision
from tests.decision_support.conftest import ts


def _unit(
    evidence_id: str,
    tier: SourceTier,
    text: str,
    *,
    image_ids: tuple[str, ...] = (),
) -> EvidenceUnit:
    return EvidenceUnit(
        evidence_id=evidence_id,
        source_tier=tier,
        source_path=f"{evidence_id}.md",
        title=f"三类买点 {evidence_id}",
        text=text,
        sha256="sha256:" + "1" * 64,
        image_ids=image_ids,
        concepts=("三类买点", "中枢回试"),
    )


@pytest.fixture
def risk_decision() -> RiskDecision:
    return RiskDecision(
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


@pytest.fixture
def corpus_index() -> CorpusIndex:
    units = (
        _unit(
            "original-support",
            SourceTier.LESSON_ORIGINAL,
            "三类买点要求回试不进入前中枢。",
        ),
        _unit(
            "secondary-support",
            SourceTier.SECONDARY_ANNOTATION,
            "整理者将三类买点标注为中枢上方回试。",
        ),
        _unit(
            "original-counter-1",
            SourceTier.LESSON_ORIGINAL,
            "三类买点若跌破中枢上沿则条件失效。",
        ),
        _unit(
            "original-counter-2",
            SourceTier.LESSON_ORIGINAL,
            "三类买点仍有失败风险，结构不成立时退出。",
        ),
    )
    return CorpusIndex.build(units)


@pytest.fixture
def image_corpus_index() -> CorpusIndex:
    image = ImageEvidence(
        image_id="image-1",
        source_tier=SourceTier.LESSON_CHART,
        source_path="chart.jpg",
        sha256="sha256:" + "2" * 64,
        media_type="image/jpeg",
        width=800,
        height=600,
        alt_text="三类买点课程图",
    )
    units = (
        _unit(
            "original-image-support",
            SourceTier.LESSON_ORIGINAL,
            "三类买点配图说明回试与中枢的位置。",
            image_ids=(image.image_id,),
        ),
        _unit(
            "original-image-counter",
            SourceTier.LESSON_ORIGINAL,
            "三类买点跌破后失效风险。",
        ),
    )
    return CorpusIndex.build(units, images=(image,))


def test_packet_keeps_source_tiers_and_counter_evidence(
    make_decision_event,
    corpus_index,
    risk_decision,
) -> None:
    packet = build_evidence_packet(
        make_decision_event(bs_type="3buy"),
        risk_decision,
        corpus_index,
        ModelCapabilities(True, True),
    )

    assert packet.reviewable is True
    assert any(
        item.source_tier is SourceTier.LESSON_ORIGINAL
        for item in packet.supporting
    )
    assert any(
        item.source_tier is SourceTier.PROJECT_IMPLEMENTATION
        for item in packet.supporting
    )
    assert len(packet.counter_evidence) == 2
    assert all(
        item.evidence_id
        for item in packet.supporting + packet.counter_evidence
    )


def test_bound_event_without_rule_evidence_binding_is_unreviewable(
    make_bound_decision_event,
    corpus_index,
    risk_decision,
) -> None:
    packet = build_evidence_packet(
        make_bound_decision_event(bs_type="3buy"),
        risk_decision,
        corpus_index,
        ModelCapabilities(True, True),
    )

    assert packet.reviewable is False
    assert "missing_rule_evidence_binding" in packet.blockers


def test_bound_packet_pins_rule_support_counter_and_chart_ids_before_rag(
    make_bound_decision_event,
    risk_decision,
) -> None:
    image = ImageEvidence(
        image_id="required-chart",
        source_tier=SourceTier.LESSON_CHART,
        source_path="required-chart.jpg",
        sha256="sha256:" + "2" * 64,
        media_type="image/jpeg",
        width=800,
        height=600,
        alt_text="规则卡要求的课程图",
    )
    required_support = _unit(
        "required-support",
        SourceTier.LESSON_ORIGINAL,
        "原文结构定义。",
        image_ids=(image.image_id,),
    )
    required_counter = _unit(
        "required-counter",
        SourceTier.LESSON_ORIGINAL,
        "原文否定条件。",
    )
    distracting = _unit(
        "rag-only",
        SourceTier.LESSON_ORIGINAL,
        "三类买点要求回试不进入前中枢。",
    )
    event = make_bound_decision_event(bs_type="3buy")
    binding = RuleEvidenceBinding(
        rule_id=event.rule_id,
        rule_card_version=event.rule_card_version,
        rule_card_fingerprint=event.rule_card_fingerprint,
        rule_set_fingerprint=event.rule_set_fingerprint,
        corpus_manifest_fingerprint=event.corpus_manifest_fingerprint,
        algorithm_fingerprint=event.algorithm_fingerprint,
        supporting_evidence_ids=(required_support.evidence_id,),
        counterevidence_ids=(required_counter.evidence_id,),
        image_ids=(image.image_id,),
    )

    packet = build_evidence_packet(
        event,
        risk_decision,
        CorpusIndex.build(
            (distracting, required_counter, required_support),
            images=(image,),
        ),
        ModelCapabilities(True, True),
        rule_evidence_binding=binding,
    )

    assert required_support in packet.supporting
    assert required_counter in packet.counter_evidence
    assert packet.image_evidence == (image,)
    assert packet.rule_evidence_binding == binding
    assert packet.reviewable is True


def test_rule_evidence_identity_mismatch_is_unreviewable(
    make_bound_decision_event,
    corpus_index,
    risk_decision,
) -> None:
    event = make_bound_decision_event(bs_type="3buy")
    binding = RuleEvidenceBinding(
        rule_id=event.rule_id,
        rule_card_version=event.rule_card_version,
        rule_card_fingerprint=event.rule_card_fingerprint,
        rule_set_fingerprint=event.rule_set_fingerprint,
        corpus_manifest_fingerprint=event.corpus_manifest_fingerprint,
        algorithm_fingerprint="sha256:" + "9" * 64,
        supporting_evidence_ids=("original-support",),
        counterevidence_ids=("original-counter-1",),
        image_ids=(),
    )

    packet = build_evidence_packet(
        event,
        risk_decision,
        corpus_index,
        ModelCapabilities(True, True),
        rule_evidence_binding=binding,
    )

    assert packet.reviewable is False
    assert "rule_evidence_binding_mismatch" in packet.blockers


def test_rag_cannot_substitute_for_missing_rule_card_evidence_ids(
    make_bound_decision_event,
    corpus_index,
    risk_decision,
) -> None:
    event = make_bound_decision_event(bs_type="3buy")
    binding = RuleEvidenceBinding(
        rule_id=event.rule_id,
        rule_card_version=event.rule_card_version,
        rule_card_fingerprint=event.rule_card_fingerprint,
        rule_set_fingerprint=event.rule_set_fingerprint,
        corpus_manifest_fingerprint=event.corpus_manifest_fingerprint,
        algorithm_fingerprint=event.algorithm_fingerprint,
        supporting_evidence_ids=("missing-rule-support",),
        counterevidence_ids=("missing-rule-counter",),
        image_ids=(),
    )

    packet = build_evidence_packet(
        event,
        risk_decision,
        corpus_index,
        ModelCapabilities(True, True),
        rule_evidence_binding=binding,
    )

    assert packet.reviewable is False
    assert "missing_rule_supporting_evidence" in packet.blockers
    assert "missing_rule_counter_evidence" in packet.blockers


def test_required_image_without_vision_marks_packet_unreviewable(
    make_decision_event,
    image_corpus_index,
    risk_decision,
) -> None:
    packet = build_evidence_packet(
        make_decision_event(bs_type="3buy_nest"),
        risk_decision,
        image_corpus_index,
        ModelCapabilities(False, True),
    )

    assert packet.image_evidence[0].image_id == "image-1"
    assert packet.reviewable is False
    assert "image_evidence_unseen" in packet.blockers


def test_packet_is_bounded_and_reserves_two_counter_slots(
    make_decision_event,
    risk_decision,
) -> None:
    units = [
        _unit(
            f"support-{index:02d}",
            SourceTier.LESSON_ORIGINAL,
            "三类买点回试中枢上沿。",
        )
        for index in range(10)
    ]
    units.extend(
        (
            _unit(
                "counter-a",
                SourceTier.LESSON_ORIGINAL,
                "三类买点跌破后失效。",
            ),
            _unit(
                "counter-b",
                SourceTier.LESSON_ORIGINAL,
                "三类买点存在失败风险。",
            ),
            _unit(
                "counter-c",
                SourceTier.LESSON_ORIGINAL,
                "三类买点结构不成立。",
            ),
        )
    )

    packet = build_evidence_packet(
        make_decision_event(bs_type="3buy"),
        risk_decision,
        CorpusIndex.build(units),
        ModelCapabilities(True, True),
        max_units=8,
    )

    assert len(packet.supporting) + len(packet.counter_evidence) == 8
    assert len(packet.counter_evidence) >= 2
    ids = [
        item.evidence_id
        for item in packet.supporting + packet.counter_evidence
    ]
    assert len(ids) == len(set(ids))


def test_missing_original_evidence_is_explicitly_unreviewable(
    make_decision_event,
    risk_decision,
) -> None:
    index = CorpusIndex.build(
        [
            _unit(
                "secondary-only",
                SourceTier.SECONDARY_ANNOTATION,
                "三类买点整理说明。",
            )
        ]
    )

    packet = build_evidence_packet(
        make_decision_event(bs_type="3buy"),
        risk_decision,
        index,
        ModelCapabilities(True, True),
    )

    assert packet.reviewable is False
    assert "missing_original_evidence" in packet.blockers


def test_risk_rejection_blocks_packet_without_erasing_evidence(
    make_decision_event,
    corpus_index,
    risk_decision,
) -> None:
    rejected = replace(risk_decision, allowed=False, reasons=("max_positions",))

    packet = build_evidence_packet(
        make_decision_event(bs_type="3buy"),
        rejected,
        corpus_index,
        ModelCapabilities(True, True),
    )

    assert packet.supporting
    assert packet.reviewable is False
    assert "risk_rejected" in packet.blockers


def test_packet_fingerprint_is_deterministic_and_event_bound(
    make_decision_event,
    corpus_index,
    risk_decision,
) -> None:
    capabilities = ModelCapabilities(True, True)
    event = make_decision_event(bs_type="3buy")

    first = build_evidence_packet(event, risk_decision, corpus_index, capabilities)
    second = build_evidence_packet(event, risk_decision, corpus_index, capabilities)
    changed = build_evidence_packet(
        make_decision_event(bs_type="2buy"),
        risk_decision,
        corpus_index,
        capabilities,
    )

    assert first.packet_fingerprint == second.packet_fingerprint
    assert first.packet_fingerprint != changed.packet_fingerprint


def test_missing_referenced_image_is_a_packet_blocker(
    make_decision_event,
    risk_decision,
) -> None:
    index = CorpusIndex.build(
        [
            _unit(
                "missing-image-unit",
                SourceTier.LESSON_ORIGINAL,
                "三类买点课程图。",
                image_ids=("missing-image",),
            )
        ]
    )

    packet = build_evidence_packet(
        make_decision_event(bs_type="3buy"),
        risk_decision,
        index,
        ModelCapabilities(True, True),
    )

    assert packet.reviewable is False
    assert "missing_image_evidence" in packet.blockers


def test_missing_counter_evidence_blocks_provider_review(
    make_decision_event,
    risk_decision,
) -> None:
    index = CorpusIndex.build(
        [
            _unit(
                "support-only",
                SourceTier.LESSON_ORIGINAL,
                "第三类买点回试中枢上沿。",
            )
        ]
    )

    packet = build_evidence_packet(
        make_decision_event(bs_type="3buy"),
        risk_decision,
        index,
        ModelCapabilities(True, True),
    )

    assert packet.reviewable is False
    assert "missing_counter_evidence" in packet.blockers


def test_packet_rejects_duplicate_text_evidence_identity(
    make_decision_event,
    corpus_index,
    risk_decision,
) -> None:
    packet = build_evidence_packet(
        make_decision_event(bs_type="3buy"),
        risk_decision,
        corpus_index,
        ModelCapabilities(True, True),
    )

    with pytest.raises(ValueError, match="duplicate evidence identity"):
        replace(packet, counter_evidence=(packet.supporting[0],))


def test_packet_rejects_cross_text_image_identity_collision(
    make_decision_event,
    corpus_index,
    risk_decision,
) -> None:
    packet = build_evidence_packet(
        make_decision_event(bs_type="3buy"),
        risk_decision,
        corpus_index,
        ModelCapabilities(True, True),
    )
    image = ImageEvidence(
        image_id=packet.supporting[0].evidence_id,
        source_tier=SourceTier.LESSON_CHART,
        source_path="collision.jpg",
        sha256="sha256:" + "3" * 64,
        media_type="image/jpeg",
        width=10,
        height=10,
    )

    with pytest.raises(ValueError, match="duplicate evidence identity"):
        replace(packet, image_evidence=(image,))


def test_packet_rejects_wrong_member_types(
    make_decision_event,
    corpus_index,
    risk_decision,
) -> None:
    packet = build_evidence_packet(
        make_decision_event(bs_type="3buy"),
        risk_decision,
        corpus_index,
        ModelCapabilities(True, True),
    )

    with pytest.raises(TypeError, match="supporting must contain EvidenceUnit"):
        replace(packet, supporting=(object(),))
