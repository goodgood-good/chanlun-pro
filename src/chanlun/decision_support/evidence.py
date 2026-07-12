from __future__ import annotations

from dataclasses import dataclass
import json
import re
from typing import TYPE_CHECKING

from .corpus_retrieval import CorpusIndex, EvidenceQuery, concepts_for_event
from .corpus_types import EvidenceUnit, ImageEvidence, SourceTier
from .fingerprints import sha256_json
from .models import DecisionEvent
from .risk import RiskDecision

if TYPE_CHECKING:
    from .rule_cards import RuleCard, RuleEvaluation, RuleSet


_COUNTER_MARKERS = (
    "失效",
    "风险",
    "不成立",
    "跌破",
    "反例",
    "失败",
    "否定",
)


@dataclass(frozen=True, slots=True)
class ModelCapabilities:
    supports_images: bool
    supports_json_schema: bool

    def __post_init__(self) -> None:
        if type(self.supports_images) is not bool:
            raise TypeError("supports_images must be boolean")
        if type(self.supports_json_schema) is not bool:
            raise TypeError("supports_json_schema must be boolean")


_FINGERPRINT_RE = re.compile(r"sha256:[0-9a-f]{64}")


def _binding_ids(value: object, field_name: str, *, required: bool) -> tuple[str, ...]:
    try:
        normalized = tuple(value)  # type: ignore[arg-type]
    except TypeError as exc:
        raise TypeError(f"{field_name} must be an iterable of strings") from exc
    if required and not normalized:
        raise ValueError(f"{field_name} cannot be empty")
    if any(not isinstance(item, str) or not item for item in normalized):
        raise ValueError(f"{field_name} must contain non-empty strings")
    if len(normalized) != len(set(normalized)):
        raise ValueError(f"{field_name} contains duplicate identities")
    if normalized != tuple(sorted(normalized)):
        raise ValueError(f"{field_name} must be canonical and sorted")
    return normalized


@dataclass(frozen=True, slots=True)
class RuleEvidenceBinding:
    rule_id: str
    rule_card_version: int
    rule_card_fingerprint: str
    rule_set_fingerprint: str
    corpus_manifest_fingerprint: str
    algorithm_fingerprint: str
    supporting_evidence_ids: tuple[str, ...]
    counterevidence_ids: tuple[str, ...]
    image_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.rule_id, str) or not self.rule_id:
            raise ValueError("rule_id must be a non-empty string")
        if (
            isinstance(self.rule_card_version, bool)
            or not isinstance(self.rule_card_version, int)
            or self.rule_card_version <= 0
        ):
            raise ValueError("rule_card_version must be a positive integer")
        for field_name in (
            "rule_card_fingerprint",
            "rule_set_fingerprint",
            "corpus_manifest_fingerprint",
            "algorithm_fingerprint",
        ):
            if _FINGERPRINT_RE.fullmatch(getattr(self, field_name)) is None:
                raise ValueError(f"{field_name} must use sha256:<64 lowercase hex>")
        supporting = _binding_ids(
            self.supporting_evidence_ids,
            "supporting_evidence_ids",
            required=True,
        )
        counter = _binding_ids(
            self.counterevidence_ids,
            "counterevidence_ids",
            required=True,
        )
        images = _binding_ids(self.image_ids, "image_ids", required=False)
        if set(supporting) & set(counter):
            raise ValueError("supporting and counter evidence identities overlap")
        object.__setattr__(self, "supporting_evidence_ids", supporting)
        object.__setattr__(self, "counterevidence_ids", counter)
        object.__setattr__(self, "image_ids", images)

    @classmethod
    def from_rule_card(
        cls,
        card: RuleCard,
        rule_set: RuleSet,
    ) -> RuleEvidenceBinding:
        from .rule_cards import RuleCard as RuleCardType
        from .rule_cards import RuleSet as RuleSetType

        if not isinstance(card, RuleCardType):
            raise TypeError("card must be RuleCard")
        if not isinstance(rule_set, RuleSetType):
            raise TypeError("rule_set must be RuleSet")
        matching_cards = tuple(
            candidate
            for candidate in rule_set.cards
            if candidate.rule_id == card.rule_id and candidate.version == card.version
        )
        if len(matching_cards) != 1 or matching_cards[0].fingerprint != card.fingerprint:
            raise ValueError("rule card does not belong to rule set")
        references = (*card.evidence, *card.counterevidence)
        return cls(
            rule_id=card.rule_id,
            rule_card_version=card.version,
            rule_card_fingerprint=card.fingerprint,
            rule_set_fingerprint=rule_set.fingerprint,
            corpus_manifest_fingerprint=(
                f"sha256:{rule_set.corpus_manifest_sha256}"
            ),
            algorithm_fingerprint=sha256_json(
                {"algorithm_version": card.algorithm_version}
            ),
            supporting_evidence_ids=tuple(
                sorted(reference.evidence_id for reference in card.evidence)
            ),
            counterevidence_ids=tuple(
                sorted(reference.evidence_id for reference in card.counterevidence)
            ),
            image_ids=tuple(
                sorted(
                    {
                        image_id
                        for reference in references
                        for image_id in reference.lesson_chart_ids
                    }
                )
            ),
        )

    @classmethod
    def from_rule_evaluation(
        cls,
        evaluation: RuleEvaluation,
        *,
        card: RuleCard,
        rule_set: RuleSet,
    ) -> RuleEvidenceBinding:
        from .rule_cards import RuleEvaluation as RuleEvaluationType

        if not isinstance(evaluation, RuleEvaluationType):
            raise TypeError("evaluation must be RuleEvaluation")
        binding = cls.from_rule_card(card, rule_set)
        identity_fields = (
            "rule_id",
            "rule_card_version",
            "rule_card_fingerprint",
            "rule_set_fingerprint",
            "corpus_manifest_fingerprint",
            "algorithm_fingerprint",
        )
        if any(
            getattr(evaluation, field_name) != getattr(binding, field_name)
            for field_name in identity_fields
        ):
            raise ValueError("rule evaluation identity mismatch")
        if tuple(evaluation.supporting_evidence_ids) != (
            binding.supporting_evidence_ids
        ):
            raise ValueError("rule evaluation supporting evidence mismatch")
        if tuple(evaluation.counterevidence_ids) != binding.counterevidence_ids:
            raise ValueError("rule evaluation counter evidence mismatch")
        expected_ids = tuple(
            sorted(
                {
                    *binding.supporting_evidence_ids,
                    *binding.counterevidence_ids,
                }
            )
        )
        if tuple(evaluation.evidence_ids) != expected_ids:
            raise ValueError("rule evaluation evidence identity set mismatch")
        return binding


@dataclass(frozen=True, slots=True)
class EvidencePacket:
    event: DecisionEvent
    risk: RiskDecision
    rule_evidence_binding: RuleEvidenceBinding | None
    supporting: tuple[EvidenceUnit, ...]
    counter_evidence: tuple[EvidenceUnit, ...]
    image_evidence: tuple[ImageEvidence, ...]
    reviewable: bool
    blockers: tuple[str, ...]
    packet_fingerprint: str

    def __post_init__(self) -> None:
        if not isinstance(self.event, DecisionEvent):
            raise TypeError("event must be DecisionEvent")
        if not isinstance(self.risk, RiskDecision):
            raise TypeError("risk must be RiskDecision")
        if self.rule_evidence_binding is not None and not isinstance(
            self.rule_evidence_binding,
            RuleEvidenceBinding,
        ):
            raise TypeError("rule_evidence_binding must be RuleEvidenceBinding")
        for field_name in (
            "supporting",
            "counter_evidence",
            "image_evidence",
            "blockers",
        ):
            object.__setattr__(self, field_name, tuple(getattr(self, field_name)))
        for field_name in ("supporting", "counter_evidence"):
            if not all(
                isinstance(item, EvidenceUnit)
                for item in getattr(self, field_name)
            ):
                raise TypeError(f"{field_name} must contain EvidenceUnit values")
        if not all(
            isinstance(item, ImageEvidence) for item in self.image_evidence
        ):
            raise TypeError("image_evidence must contain ImageEvidence values")
        units = (*self.supporting, *self.counter_evidence)
        if any(
            unit.source_tier is SourceTier.MODEL_INFERENCE for unit in units
        ) or any(
            image.source_tier is SourceTier.MODEL_INFERENCE
            for image in self.image_evidence
        ):
            raise ValueError("model inference cannot be packet evidence")
        evidence_ids = [unit.evidence_id for unit in units]
        evidence_ids.extend(image.image_id for image in self.image_evidence)
        if len(evidence_ids) != len(set(evidence_ids)):
            raise ValueError("duplicate evidence identity")
        if any(
            not isinstance(blocker, str) or not blocker.strip()
            for blocker in self.blockers
        ):
            raise ValueError("blockers must contain non-empty strings")
        if len(self.blockers) != len(set(self.blockers)):
            raise ValueError("blockers must be unique")
        if type(self.reviewable) is not bool:
            raise TypeError("reviewable must be boolean")
        if self.reviewable != (not self.blockers):
            raise ValueError("reviewable must match blockers")
        if (
            self.event.rule_binding_status == "bound"
            and self.rule_evidence_binding is None
            and "missing_rule_evidence_binding" not in self.blockers
        ):
            raise ValueError("bound event must fail closed without rule evidence binding")
        if (
            not isinstance(self.packet_fingerprint, str)
            or not self.packet_fingerprint.startswith("sha256:")
            or len(self.packet_fingerprint) != 71
        ):
            raise ValueError("packet_fingerprint must use sha256")


def _event_concepts(event: DecisionEvent) -> tuple[str, ...]:
    concepts = list(concepts_for_event(event.signal.bs_type))
    if event.signal.divergence_kind == "qs" and "趋势背驰" not in concepts:
        concepts.append("趋势背驰")
    if event.signal.nest_operable and "区间套" not in concepts:
        concepts.append("区间套")
    return tuple(concepts)


def _project_fact_unit(
    event: DecisionEvent,
    concepts: tuple[str, ...],
) -> EvidenceUnit:
    facts = {
        "event_id": event.event_id,
        "data_fingerprint": event.data_fingerprint,
        "strategy_track": event.strategy_track.value,
        "code": event.code,
        "bar_closed_at": event.bar_closed_at.isoformat(),
        "signal": event.to_dict()["signal"],
        "levels": event.to_dict()["levels"],
        "market_constraints": event.to_dict()["market_constraints"],
    }
    fingerprint = sha256_json(facts)
    return EvidenceUnit(
        evidence_id=f"project-event-{fingerprint[7:]}",
        source_tier=SourceTier.PROJECT_IMPLEMENTATION,
        source_path=f"decision_event/{event.event_id}",
        title="项目算法结构事实快照",
        text=json.dumps(
            facts,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ),
        sha256=fingerprint,
        concepts=concepts,
    )


def _is_counter_evidence(unit: EvidenceUnit) -> bool:
    text = f"{unit.title}\n{unit.text}".casefold()
    return any(marker in text for marker in _COUNTER_MARKERS)


def _prefer_original(
    units: list[EvidenceUnit],
) -> list[EvidenceUnit]:
    originals = [
        unit
        for unit in units
        if unit.source_tier is SourceTier.LESSON_ORIGINAL
    ]
    if not originals:
        return units
    first = originals[0]
    return [first, *(unit for unit in units if unit.evidence_id != first.evidence_id)]


def build_evidence_packet(
    event: DecisionEvent,
    risk: RiskDecision,
    index: CorpusIndex,
    capabilities: ModelCapabilities,
    max_units: int = 8,
    *,
    rule_evidence_binding: RuleEvidenceBinding | None = None,
) -> EvidencePacket:
    if not isinstance(event, DecisionEvent):
        raise TypeError("event must be DecisionEvent")
    if not isinstance(risk, RiskDecision):
        raise TypeError("risk must be RiskDecision")
    if not isinstance(index, CorpusIndex):
        raise TypeError("index must be CorpusIndex")
    if not isinstance(capabilities, ModelCapabilities):
        raise TypeError("capabilities must be ModelCapabilities")
    if rule_evidence_binding is not None and not isinstance(
        rule_evidence_binding,
        RuleEvidenceBinding,
    ):
        raise TypeError("rule_evidence_binding must be RuleEvidenceBinding")
    if isinstance(max_units, bool) or not isinstance(max_units, int) or max_units <= 0:
        raise ValueError("max_units must be a positive integer")

    blockers: list[str] = []

    def block(reason: str) -> None:
        if reason not in blockers:
            blockers.append(reason)

    binding_valid = False
    if event.rule_binding_status == "bound":
        if rule_evidence_binding is None:
            block("missing_rule_evidence_binding")
        else:
            identity_fields = (
                "rule_id",
                "rule_card_version",
                "rule_card_fingerprint",
                "rule_set_fingerprint",
                "corpus_manifest_fingerprint",
                "algorithm_fingerprint",
            )
            if any(
                getattr(event, field_name)
                != getattr(rule_evidence_binding, field_name)
                for field_name in identity_fields
            ):
                block("rule_evidence_binding_mismatch")
            else:
                binding_valid = True
    elif rule_evidence_binding is not None:
        block("unexpected_rule_evidence_binding")

    concepts = _event_concepts(event)
    query_text = " ".join((*concepts, event.signal.bs_type))
    hits = index.search(
        EvidenceQuery(text=query_text, concepts=concepts),
        limit=max(8, max_units * 4),
    )
    source_units = [
        hit.unit
        for hit in hits
        if hit.unit.source_tier is not SourceTier.MODEL_INFERENCE
    ]
    rag_counter_candidates = [
        unit for unit in source_units if _is_counter_evidence(unit)
    ]
    rag_support_candidates = _prefer_original(
        [unit for unit in source_units if not _is_counter_evidence(unit)]
    )

    project_unit = _project_fact_unit(event, concepts)
    if binding_valid and rule_evidence_binding is not None:
        required_supporting = index.units_for(
            rule_evidence_binding.supporting_evidence_ids
        )
        required_counter = index.units_for(
            rule_evidence_binding.counterevidence_ids
        )
        found_support_ids = {unit.evidence_id for unit in required_supporting}
        found_counter_ids = {unit.evidence_id for unit in required_counter}
        if found_support_ids != set(
            rule_evidence_binding.supporting_evidence_ids
        ):
            block("missing_rule_supporting_evidence")
        if found_counter_ids != set(rule_evidence_binding.counterevidence_ids):
            block("missing_rule_counter_evidence")
        if any(
            unit.source_tier is not SourceTier.LESSON_ORIGINAL
            for unit in required_supporting
        ):
            block("rule_supporting_evidence_not_original")
        if any(
            unit.source_tier is not SourceTier.LESSON_ORIGINAL
            for unit in required_counter
        ):
            block("rule_counter_evidence_not_original")

        required_count = 1 + len(required_supporting) + len(required_counter)
        if required_count > max_units:
            block("rule_evidence_exceeds_max_units")
        supporting = [project_unit]
        counter: list[EvidenceUnit] = []
        remaining = max_units - 1
        supporting.extend(required_supporting[:remaining])
        remaining = max_units - len(supporting)
        counter.extend(required_counter[:remaining])

        selected_ids = {
            unit.evidence_id for unit in (*supporting, *counter)
        }
        rag_counter = [
            unit
            for unit in rag_counter_candidates
            if unit.evidence_id not in selected_ids
        ]
        rag_support = [
            unit
            for unit in rag_support_candidates
            if unit.evidence_id not in selected_ids
        ]
        remaining = max_units - len(supporting) - len(counter)
        reserve = min(max(0, 2 - len(counter)), len(rag_counter), remaining)
        counter.extend(rag_counter[:reserve])
        remaining = max_units - len(supporting) - len(counter)
        supporting.extend(rag_support[:remaining])
        remaining = max_units - len(supporting) - len(counter)
        counter.extend(rag_counter[reserve : reserve + remaining])
    else:
        counter_slots = min(
            2,
            len(rag_counter_candidates),
            max(0, max_units - 1),
        )
        support_budget = max_units - counter_slots
        supporting = [project_unit]
        supporting.extend(
            rag_support_candidates[: max(0, support_budget - 1)]
        )
        counter = rag_counter_candidates[:counter_slots]
        remaining = max_units - len(supporting) - len(counter)
        if remaining > 0:
            support_start = len(supporting) - 1
            supporting.extend(
                rag_support_candidates[
                    support_start : support_start + remaining
                ]
            )
        remaining = max_units - len(supporting) - len(counter)
        if remaining > 0:
            counter.extend(
                rag_counter_candidates[len(counter) : len(counter) + remaining]
            )

    selected_units = (*supporting, *counter)
    selected_image_ids = tuple(
        dict.fromkeys(
            image_id
            for unit in selected_units
            for image_id in unit.image_ids
        )
    )
    required_image_ids = (
        rule_evidence_binding.image_ids
        if binding_valid and rule_evidence_binding is not None
        else ()
    )
    image_ids = tuple(dict.fromkeys((*required_image_ids, *selected_image_ids)))
    images = index.images_for(image_ids)
    found_image_ids = {image.image_id for image in images}
    if not risk.allowed:
        block("risk_rejected")
    if not any(
        unit.source_tier is SourceTier.LESSON_ORIGINAL
        for unit in (*supporting, *counter)
    ):
        block("missing_original_evidence")
    if not any(
        unit.source_tier is not SourceTier.PROJECT_IMPLEMENTATION
        for unit in supporting
    ):
        block("missing_supporting_evidence")
    if not counter:
        block("missing_counter_evidence")
    if any(image_id not in found_image_ids for image_id in image_ids):
        block("missing_image_evidence")
    if any(image_id not in found_image_ids for image_id in required_image_ids):
        block("missing_rule_image_evidence")
    if required_image_ids:
        required_text_ids = {
            *rule_evidence_binding.supporting_evidence_ids,
            *rule_evidence_binding.counterevidence_ids,
        }
        required_units = tuple(
            unit
            for unit in selected_units
            if unit.evidence_id in required_text_ids
        )
        linked_images = {
            image_id for unit in required_units for image_id in unit.image_ids
        }
        if any(image_id not in linked_images for image_id in required_image_ids):
            block("unlinked_rule_image_evidence")
        if any(
            image.source_tier is not SourceTier.LESSON_CHART
            for image in images
            if image.image_id in required_image_ids
        ):
            block("rule_image_evidence_not_lesson_chart")
    if image_ids and not capabilities.supports_images:
        block("image_evidence_unseen")

    packet_payload = {
        "schema_version": 2,
        "event": event,
        "risk": risk,
        "rule_evidence_binding": rule_evidence_binding,
        "supporting": tuple(supporting),
        "counter_evidence": tuple(counter),
        "image_evidence": images,
        "blockers": tuple(blockers),
        "capabilities": capabilities,
    }
    return EvidencePacket(
        event=event,
        risk=risk,
        rule_evidence_binding=rule_evidence_binding,
        supporting=tuple(supporting),
        counter_evidence=tuple(counter),
        image_evidence=images,
        reviewable=not blockers,
        blockers=tuple(blockers),
        packet_fingerprint=sha256_json(packet_payload),
    )
