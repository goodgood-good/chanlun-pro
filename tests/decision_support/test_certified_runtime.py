from __future__ import annotations

from dataclasses import replace
import hashlib
from pathlib import Path

import pytest

from chanlun.decision_support.certified_runtime import CertifiedCorpusRuntime
from chanlun.decision_support.evidence import (
    ModelCapabilities,
    RuleEvidenceBinding,
)
from chanlun.decision_support.event_factory import bind_rule_evaluation
from chanlun.decision_support.fingerprints import sha256_json
from chanlun.decision_support.llm_provider import ProviderImage
from chanlun.decision_support.models import StrategyTrack
from chanlun.decision_support.risk import RiskPolicy, evaluate_entry
from chanlun.decision_support.rule_cards import (
    EvaluationVerdict,
    RuleEvaluation,
    load_rule_set_file,
)


_CERTIFIED_CORPUS_ROOT = Path("audit/chanlun_lesson_corpus_v3")
_CERTIFIED_RULE_CARDS = Path("config/decision_support/rule_cards.json")
pytestmark = pytest.mark.skipif(
    not _CERTIFIED_CORPUS_ROOT.is_dir() or not _CERTIFIED_RULE_CARDS.is_file(),
    reason="optional certified legacy corpus package is not versioned",
)


def test_certified_runtime_reverifies_status_evidence_and_image_bytes(
    make_bound_decision_event,
    make_risk_context,
) -> None:
    runtime = CertifiedCorpusRuntime(_CERTIFIED_CORPUS_ROOT)
    event = make_bound_decision_event()
    context = make_risk_context(
        quote_code=event.code,
        asof=event.observed_at,
    )
    risk = evaluate_entry(event, context, RiskPolicy.conservative())

    status = runtime.status()
    corpus = runtime.corpus()
    packet = runtime.evidence_packet(
        event,
        risk,
        ModelCapabilities(
            supports_images=True,
            supports_json_schema=True,
        ),
    )
    image = corpus.images[0]
    payload, media_type = runtime.read(image.image_id)
    index = runtime.corpus_index()
    provider_image = runtime.load_provider_image(image)

    assert status["integrity"] == "complete"
    assert status["original_integrity"] == "complete"
    assert status["original_evidence"] == "available"
    assert status["trusted_units"] == len(corpus.units)
    assert status["semantic_units"] == len(corpus.semantic_units)
    assert status["trusted_images"] == len(corpus.images)
    assert packet.event == event
    assert any(
        unit.source_tier.value == "lesson_original"
        for unit in (*packet.supporting, *packet.counter_evidence)
    )
    assert media_type == image.media_type
    assert "sha256:" + hashlib.sha256(payload).hexdigest() == image.sha256
    assert index is runtime.corpus_index()
    assert isinstance(provider_image, ProviderImage)
    assert provider_image.image_id == image.image_id
    assert provider_image.media_type == image.media_type


def test_real_rule_card_evidence_binding_resolves_certified_text_and_chart(
    make_decision_event,
    make_risk_context,
) -> None:
    runtime = CertifiedCorpusRuntime(_CERTIFIED_CORPUS_ROOT)
    corpus = runtime.corpus()
    rules = load_rule_set_file(
        _CERTIFIED_RULE_CARDS,
        corpus=corpus,
    )
    card = next(
        item for item in rules.cards if item.rule_id == "chanlun.trend.third_buy"
    )
    event = make_decision_event(
        level=0,
        track=StrategyTrack.CHANLUN_SOURCE_FAITHFUL,
    )
    assert event.signal.level in card.applicable_levels
    support_ids = tuple(sorted(item.evidence_id for item in card.evidence))
    counter_ids = tuple(sorted(item.evidence_id for item in card.counterevidence))
    evaluation = RuleEvaluation(
        rule_id=card.rule_id,
        rule_card_version=card.version,
        rule_card_fingerprint=card.fingerprint,
        rule_set_fingerprint=rules.fingerprint,
        corpus_manifest_fingerprint="sha256:" + rules.corpus_manifest_sha256,
        algorithm_fingerprint=sha256_json(
            {"algorithm_version": card.algorithm_version}
        ),
        evaluation_input_fingerprint=event.data_fingerprint,
        strategy_track=card.track,
        level=event.signal.level,
        verdict=EvaluationVerdict.WATCH,
        candidate_satisfied=True,
        confirmation_satisfied=False,
        invalidation_triggered=False,
        conflict_triggered=False,
        critical_indeterminate=False,
        safe_to_proceed=False,
        reasons=(),
        evidence_ids=tuple(sorted({*support_ids, *counter_ids})),
        supporting_evidence_ids=support_ids,
        counterevidence_ids=counter_ids,
    )
    bound_event = bind_rule_evaluation(event, evaluation)
    context = make_risk_context(
        quote_code=bound_event.code,
        asof=bound_event.observed_at,
    )
    risk = evaluate_entry(bound_event, context, RiskPolicy.conservative())
    binding = RuleEvidenceBinding.from_rule_evaluation(
        evaluation,
        card=card,
        rule_set=rules,
    )

    packet = runtime.evidence_packet(
        bound_event,
        risk,
        ModelCapabilities(True, True),
        rule_evidence_binding=binding,
    )

    packet_support_ids = {item.evidence_id for item in packet.supporting}
    packet_counter_ids = {item.evidence_id for item in packet.counter_evidence}
    expected_image_ids = {
        image_id
        for reference in (*card.evidence, *card.counterevidence)
        for image_id in reference.lesson_chart_ids
    }
    assert set(support_ids).issubset(packet_support_ids)
    assert set(counter_ids).issubset(packet_counter_ids)
    assert {item.image_id for item in packet.image_evidence}.issuperset(
        expected_image_ids
    )
    assert runtime.corpus_index().images_for(expected_image_ids)
    assert packet.reviewable is True
    assert not any(blocker.startswith("missing_rule_") for blocker in packet.blockers)

    with pytest.raises(ValueError, match="supporting evidence mismatch"):
        RuleEvidenceBinding.from_rule_evaluation(
            replace(evaluation, supporting_evidence_ids=counter_ids),
            card=card,
            rule_set=rules,
        )
