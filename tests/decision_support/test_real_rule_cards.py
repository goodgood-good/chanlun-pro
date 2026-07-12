from __future__ import annotations

from pathlib import Path

from chanlun.decision_support.corpus_loader import load_certified_lesson_corpus
from chanlun.decision_support.models import StrategyTrack
from chanlun.decision_support.rule_context import (
    LevelEvaluationFacts,
    RuleRuntimeFacts,
)
from chanlun.decision_support.rule_engine import RuleEngine
from chanlun.decision_support.rule_cards import load_rule_set_file


def test_real_rule_cards_resolve_only_certified_original_evidence(
    make_decision_event,
) -> None:
    corpus_root = Path("audit/chanlun_lesson_corpus_v3")
    rules_path = Path("config/decision_support/rule_cards.json")
    assert rules_path.is_file(), "production RuleCard document is missing"

    corpus = load_certified_lesson_corpus(corpus_root)
    rules = load_rule_set_file(rules_path, corpus=corpus)

    assert {card.rule_id for card in rules.cards} == {
        "chanlun.bottom_reversal.interval_nest",
        "chanlun.trend.third_buy",
    }
    assert rules.corpus_manifest_sha256 == corpus.manifest_sha256
    assert rules.source_pdf_sha256 == corpus.source_pdf_sha256
    for card in rules.cards:
        assert card.evidence
        assert card.counterevidence
        assert card.fingerprint.startswith("sha256:")

    event = make_decision_event(track=StrategyTrack.TREND_CONTINUATION)
    engine = RuleEngine(rules)
    bound_event, evaluation = engine.evaluate(
        event,
        RuleRuntimeFacts(
            fundamental_ok=True,
            comparison_ok=True,
            market_liquid=True,
            risk_allowed=True,
            latest_price=event.signal.price,
            level_facts=(
                LevelEvaluationFacts(
                    frequency="5m",
                    level=1,
                    completed_bar_count=120,
                    latest_bar_closed=True,
                ),
            ),
        ),
    )

    assert evaluation.rule_id == "chanlun.trend.third_buy"
    assert evaluation.verdict.value == "WATCH"
    assert evaluation.safe_to_proceed is False
    assert bound_event.rule_binding_status == "bound"
    assert bound_event.rule_set_fingerprint == rules.fingerprint
    assert (
        bound_event.data_fingerprint
        == evaluation.evaluation_input_fingerprint
    )
