from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from enum import Enum
import json
import math
import re
from typing import Iterable, Mapping

from .corpus_types import SourceTier
from .evidence import EvidencePacket


class ReviewVerdict(str, Enum):
    CONFIRM = "CONFIRM"
    WATCH = "WATCH"
    REJECT = "REJECT"
    ABSTAIN = "ABSTAIN"


REQUIRED_TOP_LEVEL = frozenset(
    {
        "verdict",
        "strategy_track",
        "summary",
        "structure_read",
        "bull_case",
        "bear_case",
        "invalidation_checks",
        "counter_evidence",
        "risk_acknowledged",
        "missing_evidence",
        "reviewed_event_id",
        "reviewed_data_fingerprint",
        "reviewed_packet_fingerprint",
    }
)

_CLAIM_FIELDS = frozenset({"text", "evidence_ids", "source_labels", "supports"})
_SCENARIO_FIELDS = frozenset({"claims", "conditions", "rank"})
_EXECUTABLE_VERDICTS = frozenset(
    {ReviewVerdict.CONFIRM, ReviewVerdict.WATCH, ReviewVerdict.REJECT}
)
_NUMBER_PATTERN = re.compile(
    r"(?<![A-Za-z0-9_])"
    r"[-+]?(?:\d{1,3}(?:,\d{3})+|\d+(?:\.\d+)?|\.\d+)"
    r"(?:[eE][-+]?\d+)?%?"
    r"(?![A-Za-z0-9_])"
)


@dataclass(frozen=True, slots=True)
class ReviewClaim:
    text: str
    evidence_ids: tuple[str, ...]
    source_labels: tuple[SourceTier, ...]
    supports: bool

    def __post_init__(self) -> None:
        if not isinstance(self.text, str) or not self.text.strip():
            raise ValueError("claim text must be non-empty")
        object.__setattr__(self, "text", self.text.strip())
        object.__setattr__(self, "evidence_ids", tuple(self.evidence_ids))
        object.__setattr__(
            self,
            "source_labels",
            tuple(SourceTier(label) for label in self.source_labels),
        )
        if len(self.evidence_ids) != len(self.source_labels):
            raise ValueError("evidence_ids and source_labels must align")
        if any(
            not isinstance(evidence_id, str) or not evidence_id.strip()
            for evidence_id in self.evidence_ids
        ):
            raise ValueError("evidence_ids must contain non-empty strings")
        if type(self.supports) is not bool:
            raise ValueError("supports must be boolean")


@dataclass(frozen=True, slots=True)
class Scenario:
    claims: tuple[ReviewClaim, ...]
    conditions: tuple[str, ...]
    rank: int

    def __post_init__(self) -> None:
        object.__setattr__(self, "claims", tuple(self.claims))
        if not all(isinstance(claim, ReviewClaim) for claim in self.claims):
            raise TypeError("claims must contain ReviewClaim values")
        if isinstance(self.conditions, (str, bytes)):
            raise TypeError("conditions must contain non-empty strings")
        object.__setattr__(self, "conditions", tuple(self.conditions))
        if any(
            not isinstance(condition, str) or not condition.strip()
            for condition in self.conditions
        ):
            raise ValueError("conditions must contain non-empty strings")
        object.__setattr__(
            self,
            "conditions",
            tuple(condition.strip() for condition in self.conditions),
        )
        if type(self.rank) is not int or self.rank <= 0:
            raise ValueError("rank must be a positive integer")


@dataclass(frozen=True, slots=True)
class ValidatedReview:
    verdict: ReviewVerdict
    model_verdict: ReviewVerdict | None
    strategy_track: str | None
    summary: ReviewClaim | None
    structure_read: tuple[ReviewClaim, ...]
    bull_case: Scenario
    bear_case: Scenario
    invalidation_checks: tuple[ReviewClaim, ...]
    counter_evidence: tuple[ReviewClaim, ...]
    risk_acknowledged: bool | None
    missing_evidence: tuple[str, ...]
    reviewed_event_id: str | None
    reviewed_data_fingerprint: str | None
    reviewed_packet_fingerprint: str | None
    raw_response: str
    parsed_response_json: str | None
    validation_errors: tuple[str, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "verdict", ReviewVerdict(self.verdict))
        if self.model_verdict is not None:
            object.__setattr__(
                self,
                "model_verdict",
                ReviewVerdict(self.model_verdict),
            )
        for field_name in (
            "structure_read",
            "invalidation_checks",
            "counter_evidence",
            "missing_evidence",
            "validation_errors",
        ):
            object.__setattr__(self, field_name, tuple(getattr(self, field_name)))

    @property
    def executable(self) -> bool:
        return self.verdict in _EXECUTABLE_VERDICTS and not self.validation_errors

    @property
    def claims(self) -> tuple[ReviewClaim, ...]:
        summary = () if self.summary is None else (self.summary,)
        return (
            *summary,
            *self.structure_read,
            *self.bull_case.claims,
            *self.bear_case.claims,
            *self.invalidation_checks,
            *self.counter_evidence,
        )


class _DuplicateJSONKey(ValueError):
    pass


def _strict_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    value: dict[str, object] = {}
    for key, item in pairs:
        if key in value:
            raise _DuplicateJSONKey(key)
        value[key] = item
    return value


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"non-finite JSON constant: {value}")


def _add_error(errors: list[str], code: str) -> None:
    if code not in errors:
        errors.append(code)


def _safe_review(
    raw_response: object,
    error: str,
    *,
    parsed_response_json: str | None = None,
) -> ValidatedReview:
    return ValidatedReview(
        verdict=ReviewVerdict.ABSTAIN,
        model_verdict=None,
        strategy_track=None,
        summary=None,
        structure_read=(),
        bull_case=Scenario((), (), 1),
        bear_case=Scenario((), (), 2),
        invalidation_checks=(),
        counter_evidence=(),
        risk_acknowledged=None,
        missing_evidence=(),
        reviewed_event_id=None,
        reviewed_data_fingerprint=None,
        reviewed_packet_fingerprint=None,
        raw_response=(
            raw_response if isinstance(raw_response, str) else repr(raw_response)
        ),
        parsed_response_json=parsed_response_json,
        validation_errors=(error,),
    )


def _parse_string_list(
    value: object,
    errors: list[str],
    error_code: str,
) -> tuple[str, ...]:
    if not isinstance(value, list) or any(
        not isinstance(item, str) or not item.strip() for item in value
    ):
        _add_error(errors, error_code)
        return ()
    return tuple(item.strip() for item in value)


def _evidence_sources(packet: EvidencePacket) -> dict[str, SourceTier]:
    sources = {
        unit.evidence_id: unit.source_tier
        for unit in (*packet.supporting, *packet.counter_evidence)
    }
    sources.update(
        {image.image_id: image.source_tier for image in packet.image_evidence}
    )
    return sources


def _parse_claim(
    value: object,
    errors: list[str],
    evidence_sources: Mapping[str, SourceTier],
) -> ReviewClaim | None:
    if not isinstance(value, dict):
        _add_error(errors, "invalid_claim_shape")
        return None
    if frozenset(value) != _CLAIM_FIELDS:
        _add_error(errors, "invalid_claim_fields")

    text = value.get("text")
    if not isinstance(text, str) or not text.strip():
        _add_error(errors, "invalid_claim_text")
        return None
    evidence_ids = _parse_string_list(
        value.get("evidence_ids"),
        errors,
        "invalid_evidence_ids",
    )
    raw_labels = _parse_string_list(
        value.get("source_labels"),
        errors,
        "invalid_source_labels",
    )
    supports = value.get("supports")
    if type(supports) is not bool:
        _add_error(errors, "invalid_claim_supports")
        return None
    if len(evidence_ids) != len(raw_labels):
        _add_error(errors, "citation_source_count_mismatch")
        return None
    if len(evidence_ids) != len(set(evidence_ids)):
        _add_error(errors, "duplicate_evidence_id")

    labels: list[SourceTier] = []
    for evidence_id, raw_label in zip(evidence_ids, raw_labels):
        try:
            label = SourceTier(raw_label)
        except ValueError:
            _add_error(errors, "unknown_source_label")
            return None
        labels.append(label)
        expected = evidence_sources.get(evidence_id)
        if expected is None:
            _add_error(errors, "unknown_evidence_id")
        elif expected is not label:
            _add_error(errors, "evidence_source_mismatch")
    return ReviewClaim(text, evidence_ids, tuple(labels), supports)


def _parse_claim_list(
    value: object,
    errors: list[str],
    evidence_sources: Mapping[str, SourceTier],
) -> tuple[ReviewClaim, ...]:
    if not isinstance(value, list):
        _add_error(errors, "invalid_claim_list")
        return ()
    parsed = [
        _parse_claim(item, errors, evidence_sources) for item in value
    ]
    return tuple(item for item in parsed if item is not None)


def _parse_scenario(
    value: object,
    errors: list[str],
    evidence_sources: Mapping[str, SourceTier],
    *,
    expected_rank: int,
) -> Scenario:
    if not isinstance(value, dict):
        _add_error(errors, "invalid_scenario_shape")
        return Scenario((), (), expected_rank)
    if frozenset(value) != _SCENARIO_FIELDS:
        _add_error(errors, "invalid_scenario_fields")
    conditions = _parse_string_list(
        value.get("conditions"),
        errors,
        "invalid_scenario_conditions",
    )
    rank = value.get("rank")
    if type(rank) is not int or rank <= 0:
        _add_error(errors, "invalid_scenario_rank")
        rank = expected_rank
    elif rank != expected_rank:
        _add_error(errors, "scenario_rank_mismatch")
        rank = expected_rank
    return Scenario(
        _parse_claim_list(value.get("claims"), errors, evidence_sources),
        conditions,
        rank,
    )


def _normalized_decimal(value: object) -> Decimal | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, Decimal):
        return value if value.is_finite() else None
    if isinstance(value, int):
        return Decimal(value)
    if isinstance(value, float):
        if not math.isfinite(value):
            return None
        return Decimal(str(value))
    return None


def _walk_numeric_values(value: object) -> Iterable[Decimal]:
    numeric = _normalized_decimal(value)
    if numeric is not None:
        yield numeric
        return
    if isinstance(value, Mapping):
        for item in value.values():
            yield from _walk_numeric_values(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            yield from _walk_numeric_values(item)


def _number_from_token(token: str) -> Decimal | None:
    percent = token.endswith("%")
    normalized = token[:-1] if percent else token
    try:
        value = Decimal(normalized.replace(",", ""))
    except InvalidOperation:
        return None
    return value / Decimal("100") if percent else value


def _numbers_in_text(text: str) -> tuple[Decimal, ...]:
    return tuple(
        number
        for token in _NUMBER_PATTERN.findall(text)
        if (number := _number_from_token(token)) is not None
    )


def _allowed_numeric_values(packet: EvidencePacket) -> frozenset[Decimal]:
    event = packet.event
    values = set(_walk_numeric_values(event.to_dict()))
    values.update(
        value
        for value in (
            _normalized_decimal(packet.risk.shares),
            _normalized_decimal(packet.risk.planned_risk_cash),
            _normalized_decimal(packet.risk.target_weight),
            _normalized_decimal(packet.risk.entry_reference),
        )
        if value is not None
    )
    trusted_strings = [
        event.code,
        event.signal.bs_type,
        event.observed_at.isoformat(),
        event.bar_closed_at.isoformat(),
        packet.risk.evaluated_at.isoformat(),
        *(level.frequency for level in event.levels),
    ]
    for trusted in trusted_strings:
        values.update(
            Decimal(token)
            for token in re.findall(r"\d+(?:\.\d+)?", trusted)
        )
    return frozenset(values)


def _required_text(
    payload: Mapping[str, object],
    field_name: str,
    errors: list[str],
) -> str | None:
    value = payload.get(field_name)
    if not isinstance(value, str) or not value.strip():
        _add_error(errors, f"invalid_{field_name}")
        return None
    return value.strip()


def _has_citations(claim: ReviewClaim | None) -> bool:
    return claim is not None and bool(claim.evidence_ids)


def parse_review(raw: object, packet: EvidencePacket) -> ValidatedReview:
    if not isinstance(packet, EvidencePacket):
        raise TypeError("packet must be EvidencePacket")
    if not isinstance(raw, str):
        return _safe_review(raw, "raw_response_not_string")
    try:
        payload = json.loads(
            raw,
            object_pairs_hook=_strict_object,
            parse_constant=_reject_json_constant,
        )
    except _DuplicateJSONKey:
        return _safe_review(raw, "duplicate_json_key")
    except (TypeError, ValueError, json.JSONDecodeError):
        return _safe_review(raw, "invalid_json")
    if not isinstance(payload, dict):
        return _safe_review(
            raw,
            "top_level_not_object",
            parsed_response_json=json.dumps(
                payload,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ),
        )

    errors: list[str] = []
    payload_fields = frozenset(payload)
    if REQUIRED_TOP_LEVEL - payload_fields:
        _add_error(errors, "missing_top_level_fields")
    if payload_fields - REQUIRED_TOP_LEVEL:
        _add_error(errors, "unexpected_top_level_fields")

    model_verdict = None
    raw_verdict = payload.get("verdict")
    if isinstance(raw_verdict, str):
        try:
            model_verdict = ReviewVerdict(raw_verdict)
        except ValueError:
            _add_error(errors, "unknown_verdict")
    else:
        _add_error(errors, "unknown_verdict")

    strategy_track = _required_text(payload, "strategy_track", errors)
    reviewed_event_id = _required_text(payload, "reviewed_event_id", errors)
    reviewed_data_fingerprint = _required_text(
        payload,
        "reviewed_data_fingerprint",
        errors,
    )
    reviewed_packet_fingerprint = _required_text(
        payload,
        "reviewed_packet_fingerprint",
        errors,
    )
    if strategy_track is not None and strategy_track != packet.event.strategy_track.value:
        _add_error(errors, "strategy_track_mismatch")
    if reviewed_event_id is not None and reviewed_event_id != packet.event.event_id:
        _add_error(errors, "stale_event_id")
    if (
        reviewed_data_fingerprint is not None
        and reviewed_data_fingerprint != packet.event.data_fingerprint
    ):
        _add_error(errors, "stale_data_fingerprint")
    if (
        reviewed_packet_fingerprint is not None
        and reviewed_packet_fingerprint != packet.packet_fingerprint
    ):
        _add_error(errors, "stale_packet_fingerprint")

    risk_acknowledged = payload.get("risk_acknowledged")
    if type(risk_acknowledged) is not bool:
        _add_error(errors, "invalid_risk_acknowledged")
        risk_acknowledged = None
    missing_evidence = _parse_string_list(
        payload.get("missing_evidence"),
        errors,
        "invalid_missing_evidence",
    )

    sources = _evidence_sources(packet)
    summary = _parse_claim(payload.get("summary"), errors, sources)
    structure_read = _parse_claim_list(
        payload.get("structure_read"), errors, sources
    )
    bull_case = _parse_scenario(
        payload.get("bull_case"),
        errors,
        sources,
        expected_rank=1,
    )
    bear_case = _parse_scenario(
        payload.get("bear_case"),
        errors,
        sources,
        expected_rank=2,
    )
    invalidation_checks = _parse_claim_list(
        payload.get("invalidation_checks"), errors, sources
    )
    counter_evidence = _parse_claim_list(
        payload.get("counter_evidence"), errors, sources
    )

    all_claims = (
        *((summary,) if summary is not None else ()),
        *structure_read,
        *bull_case.claims,
        *bear_case.claims,
        *invalidation_checks,
        *counter_evidence,
    )
    if any(not claim.evidence_ids for claim in all_claims):
        _add_error(errors, "uncited_claim")

    if model_verdict in _EXECUTABLE_VERDICTS:
        if risk_acknowledged is not True:
            _add_error(errors, "risk_not_acknowledged")
        if missing_evidence:
            _add_error(errors, "declared_missing_evidence")
        required_sections = (
            _has_citations(summary),
            bool(structure_read)
            and all(_has_citations(claim) for claim in structure_read),
            bool(bull_case.claims)
            and all(_has_citations(claim) for claim in bull_case.claims),
            bool(bear_case.claims)
            and all(_has_citations(claim) for claim in bear_case.claims),
            bool(invalidation_checks)
            and all(_has_citations(claim) for claim in invalidation_checks),
            bool(counter_evidence)
            and all(_has_citations(claim) for claim in counter_evidence),
        )
        if not all(required_sections):
            _add_error(errors, "missing_executable_citations")
        counter_ids = {
            unit.evidence_id for unit in packet.counter_evidence
        }
        if not any(
            counter_ids.intersection(claim.evidence_ids)
            for claim in counter_evidence
        ):
            _add_error(errors, "counter_evidence_not_cited")

    allowed_numbers = _allowed_numeric_values(packet)
    claim_texts = [claim.text for claim in all_claims]
    claim_texts.extend(bull_case.conditions)
    claim_texts.extend(bear_case.conditions)
    claim_texts.extend(missing_evidence)
    if any(
        number not in allowed_numbers
        for text_value in claim_texts
        for number in _numbers_in_text(text_value)
    ):
        _add_error(errors, "untrusted_numeric_claim")

    for blocker in packet.blockers:
        _add_error(errors, blocker)

    verdict = (
        model_verdict
        if model_verdict is not None and not errors
        else ReviewVerdict.ABSTAIN
    )
    parsed_response_json = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return ValidatedReview(
        verdict=verdict,
        model_verdict=model_verdict,
        strategy_track=strategy_track,
        summary=summary,
        structure_read=structure_read,
        bull_case=bull_case,
        bear_case=bear_case,
        invalidation_checks=invalidation_checks,
        counter_evidence=counter_evidence,
        risk_acknowledged=risk_acknowledged,
        missing_evidence=missing_evidence,
        reviewed_event_id=reviewed_event_id,
        reviewed_data_fingerprint=reviewed_data_fingerprint,
        reviewed_packet_fingerprint=reviewed_packet_fingerprint,
        raw_response=raw,
        parsed_response_json=parsed_response_json,
        validation_errors=tuple(errors),
    )
