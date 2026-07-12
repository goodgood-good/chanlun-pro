from __future__ import annotations

from dataclasses import dataclass

from .models import DecisionEvent, StrategyTrack
from .rule_cards import RuleEvaluation


TREND_BUYS = frozenset({"2buy", "3buy", "3buy_nest"})
REVERSAL_BUYS = frozenset({"1buy_nest"})
REVERSAL_OBSERVATIONS = frozenset({"1buy", "1buy_nest"})
REVERSAL_CONFIRMATIONS = frozenset({"2buy", "3buy"})
_DAILY_FREQUENCIES = frozenset({"d", "1d", "day", "daily"})
_DIRECTIONS = frozenset({"up", "down", "neutral"})


@dataclass(frozen=True, slots=True)
class CandidateDecision:
    event: DecisionEvent
    track: StrategyTrack
    accepted: bool
    observation: bool
    reasons: tuple[str, ...]
    big_direction: str
    mid_direction: str
    daily_resonance: bool = False
    rule_evaluation: RuleEvaluation | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.event, DecisionEvent):
            raise TypeError("event must be DecisionEvent")
        object.__setattr__(self, "track", StrategyTrack(self.track))
        if type(self.accepted) is not bool or type(self.observation) is not bool:
            raise TypeError("candidate states must be boolean")
        if self.accepted and self.observation:
            raise ValueError("accepted candidate cannot be observation-only")
        if not all(isinstance(reason, str) and reason for reason in self.reasons):
            raise ValueError("reasons must contain non-empty strings")
        object.__setattr__(self, "reasons", tuple(self.reasons))
        if self.rule_evaluation is not None:
            if not isinstance(self.rule_evaluation, RuleEvaluation):
                raise TypeError("rule_evaluation must be RuleEvaluation")
            if (
                self.rule_evaluation.strategy_track is not self.track
                or self.rule_evaluation.level != self.event.signal.level
                or self.event.rule_id != self.rule_evaluation.rule_id
            ):
                raise ValueError("candidate rule evaluation binding mismatch")

    @property
    def strategy_track(self) -> StrategyTrack:
        return self.track


def _directions(event: DecisionEvent) -> tuple[str, str]:
    ordered = sorted(
        event.levels,
        key=lambda level: (level.level, level.frequency),
        reverse=True,
    )
    big = ordered[0].direction
    mid = ordered[1].direction if len(ordered) > 1 else big
    return big, mid


def _daily_resonance(event: DecisionEvent) -> bool:
    return any(
        level.frequency.casefold() in _DAILY_FREQUENCIES
        and level.direction in {"up", "neutral"}
        and any(mmd in {"3buy", "3buy_nest"} for mmd in level.mmds)
        for level in event.levels
    )


def _has_invalid_level_direction(event: DecisionEvent) -> bool:
    return any(level.direction not in _DIRECTIONS for level in event.levels)


def _valid_buy_stop(event: DecisionEvent) -> tuple[bool, str | None]:
    stop = event.signal.structural_stop_below
    if stop is None:
        return False, "missing_structural_stop"
    if stop >= event.signal.price:
        return False, "structural_stop_invalidated"
    return True, None


def evaluate_trend_continuation(
    event: DecisionEvent,
    *,
    fund_ok: bool,
    comparison_ok: bool,
) -> CandidateDecision:
    if not isinstance(event, DecisionEvent):
        raise TypeError("event must be DecisionEvent")
    if type(fund_ok) is not bool or type(comparison_ok) is not bool:
        raise TypeError("strategy gates must be boolean")

    big_direction, mid_direction = _directions(event)
    reasons: list[str] = []
    if _has_invalid_level_direction(event):
        reasons.append("invalid_level_direction")
    if event.signal.bs_type not in TREND_BUYS:
        reasons.append("unsupported_signal")
    if big_direction not in _DIRECTIONS:
        reasons.append("invalid_big_direction")
    elif big_direction == "down":
        reasons.append("big_level_down")
    if mid_direction not in _DIRECTIONS:
        reasons.append("invalid_mid_direction")
    elif mid_direction == "down":
        reasons.append("mid_level_down")
    _stop_ok, stop_reason = _valid_buy_stop(event)
    if stop_reason is not None:
        reasons.append(stop_reason)
    if not fund_ok:
        reasons.append("fundamental_gate_failed")
    if not comparison_ok:
        reasons.append("comparison_gate_failed")

    return CandidateDecision(
        event=event,
        track=StrategyTrack.TREND_CONTINUATION,
        accepted=not reasons,
        observation=False,
        reasons=tuple(reasons),
        big_direction=big_direction,
        mid_direction=mid_direction,
        daily_resonance=_daily_resonance(event),
    )


def evaluate_bottom_reversal(event: DecisionEvent) -> CandidateDecision:
    if not isinstance(event, DecisionEvent):
        raise TypeError("event must be DecisionEvent")

    big_direction, mid_direction = _directions(event)
    reasons: list[str] = []
    if _has_invalid_level_direction(event):
        reasons.append("invalid_level_direction")
    observation_candidate = event.signal.bs_type in REVERSAL_OBSERVATIONS
    if not observation_candidate:
        reasons.append("unsupported_signal")
    elif event.signal.bs_type not in REVERSAL_BUYS:
        reasons.append("lone_first_buy")
    if not event.signal.live_divergence:
        reasons.append("missing_live_divergence")
    if event.signal.divergence_kind != "qs":
        reasons.append("invalid_divergence_kind")
    confirmation = event.signal.confirmation_bs_type
    if confirmation is None:
        reasons.append("missing_confirmation")
    elif confirmation not in REVERSAL_CONFIRMATIONS:
        reasons.append("invalid_confirmation")
    stop_ok, stop_reason = _valid_buy_stop(event)
    if stop_reason is not None:
        reasons.append(stop_reason)
    if big_direction not in _DIRECTIONS:
        reasons.append("invalid_big_direction")
    elif big_direction == "down":
        reasons.append("big_level_down")
    if mid_direction not in _DIRECTIONS:
        reasons.append("invalid_mid_direction")

    accepted = not reasons
    malformed_structure = any(
        reason
        in {
            "invalid_level_direction",
            "invalid_big_direction",
            "invalid_mid_direction",
            "invalid_divergence_kind",
        }
        for reason in reasons
    )
    return CandidateDecision(
        event=event,
        track=StrategyTrack.BOTTOM_REVERSAL,
        accepted=accepted,
        observation=(
            observation_candidate
            and stop_ok
            and not accepted
            and not malformed_structure
        ),
        reasons=tuple(reasons),
        big_direction=big_direction,
        mid_direction=mid_direction,
        daily_resonance=False,
    )


def trend_rank_key(decision: CandidateDecision) -> tuple[int, int, float, str]:
    if decision.track is not StrategyTrack.TREND_CONTINUATION:
        raise ValueError("decision is not a trend-continuation candidate")
    signal_rank = 0 if decision.event.signal.bs_type in {"3buy", "3buy_nest"} else 1
    return (
        0 if decision.daily_resonance else 1,
        signal_rank,
        -decision.event.observed_at.timestamp(),
        decision.event.code,
    )


def reversal_rank_key(
    decision: CandidateDecision,
) -> tuple[int, int, int, float, str]:
    if decision.track is not StrategyTrack.BOTTOM_REVERSAL:
        raise ValueError("decision is not a bottom-reversal candidate")
    confirmation_rank = (
        0 if decision.event.signal.confirmation_bs_type == "3buy" else 1
    )
    return (
        0 if decision.event.signal.divergence_kind == "qs" else 1,
        confirmation_rank,
        -decision.event.signal.nest_depth,
        -decision.event.observed_at.timestamp(),
        decision.event.code,
    )
