from __future__ import annotations

from .event_factory import bind_rule_evaluation
from .models import DecisionEvent
from .rule_cards import RuleEvaluation, RuleSet, evaluate_rule_card
from .rule_context import RuleRuntimeFacts, build_rule_evaluation_context


class RuleEngine:
    def __init__(self, rule_set: RuleSet) -> None:
        if not isinstance(rule_set, RuleSet):
            raise TypeError("rule_set must be a RuleSet")
        self.rule_set = rule_set

    def evaluate(
        self,
        event: DecisionEvent,
        runtime_facts: RuleRuntimeFacts,
    ) -> tuple[DecisionEvent, RuleEvaluation]:
        if not isinstance(event, DecisionEvent):
            raise TypeError("event must be a DecisionEvent")
        if type(runtime_facts) is not RuleRuntimeFacts:
            raise TypeError("runtime_facts must be RuleRuntimeFacts")
        cards = tuple(
            card
            for card in self.rule_set.cards
            if card.track is event.strategy_track
            and event.signal.level in card.applicable_levels
        )
        if len(cards) != 1:
            raise ValueError(
                "exactly one rule card must match the event track and level"
            )
        context = build_rule_evaluation_context(event, runtime_facts)
        evaluation = evaluate_rule_card(
            cards[0],
            context,
            rule_set=self.rule_set,
            track=event.strategy_track,
            level=event.signal.level,
        )
        return bind_rule_evaluation(event, evaluation), evaluation
