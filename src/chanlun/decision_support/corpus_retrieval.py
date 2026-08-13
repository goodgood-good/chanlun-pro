from __future__ import annotations

import unicodedata
from dataclasses import dataclass
from typing import Iterable, Sequence

from .corpus_types import EvidenceUnit, SourceTier


SOURCE_WEIGHT: dict[SourceTier, float] = {
    SourceTier.LESSON_ORIGINAL: 1.30,
    SourceTier.LESSON_CHART: 1.25,
    SourceTier.PROJECT_IMPLEMENTATION: 1.10,
    SourceTier.SECONDARY_ANNOTATION: 1.00,
    SourceTier.MODEL_INFERENCE: 0.0,
}

_CONCEPT_ALIASES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("一类买点", ("一类买点", "第一类买点", "一买")),
    ("二类买点", ("二类买点", "第二类买点", "二买")),
    ("三类买点", ("三类买点", "第三类买点", "三买")),
    ("一类卖点", ("一类卖点", "第一类卖点", "一卖")),
    ("二类卖点", ("二类卖点", "第二类卖点", "二卖")),
    ("三类卖点", ("三类卖点", "第三类卖点", "三卖")),
    ("趋势背驰", ("趋势背驰", "趋势背离")),
    ("区间套", ("区间套",)),
    (
        "中枢回试",
        (
            "中枢回试",
            "回试中枢",
            "回试不进入中枢",
            "回试不进中枢",
            "回抽不进入中枢",
            "回抽不进中枢",
        ),
    ),
)

def _normalize(text: str) -> str:
    folded = unicodedata.normalize("NFKC", text).casefold()
    return "".join(character for character in folded if character.isalnum())


_SHORT_ALIAS_TO_CONCEPT = {
    _normalize(alias): canonical
    for canonical, aliases in _CONCEPT_ALIASES
    for alias in aliases
    if len(_normalize(alias)) <= 2
}
_SHORT_ALIAS_BIGRAMS = frozenset(_SHORT_ALIAS_TO_CONCEPT)


def char_bigrams(text: str) -> frozenset[str]:
    normalized = _normalize(text)
    return frozenset(
        normalized[index : index + 2]
        for index in range(max(0, len(normalized) - 1))
    )


def _unique(values: Iterable[str]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(value for value in values if value))


def _canonical_concept(value: str) -> str:
    normalized = _normalize(value)
    for canonical, aliases in _CONCEPT_ALIASES:
        if normalized in {_normalize(alias) for alias in aliases}:
            return canonical
    return normalized


def _concepts_in_text(text: str) -> tuple[str, ...]:
    normalized = _normalize(text)
    standalone_terms = frozenset(_query_terms(text))

    def alias_matches(alias: str) -> bool:
        normalized_alias = _normalize(alias)
        if len(normalized_alias) <= 2:
            return normalized_alias in standalone_terms
        return normalized_alias in normalized

    return tuple(
        canonical
        for canonical, aliases in _CONCEPT_ALIASES
        if any(alias_matches(alias) for alias in aliases)
    )


def _query_terms(text: str) -> tuple[str, ...]:
    folded = unicodedata.normalize("NFKC", text).casefold()
    terms: list[str] = []
    current: list[str] = []
    for character in folded:
        if character.isalnum():
            current.append(character)
        elif current:
            terms.append("".join(current))
            current = []
    if current:
        terms.append("".join(current))
    return _unique(terms)


@dataclass(frozen=True)
class EvidenceQuery:
    text: str
    concepts: tuple[str, ...] = ()
    source_tiers: tuple[SourceTier, ...] = ()
    exclude_evidence_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "concepts", tuple(self.concepts))
        object.__setattr__(
            self,
            "source_tiers",
            tuple(SourceTier(tier) for tier in self.source_tiers),
        )
        object.__setattr__(
            self,
            "exclude_evidence_ids",
            tuple(self.exclude_evidence_ids),
        )


@dataclass(frozen=True)
class EvidenceHit:
    unit: EvidenceUnit
    score: float
    matched_terms: tuple[str, ...]
    matched_concepts: tuple[str, ...]
    bigram_overlap: float


@dataclass(frozen=True)
class _IndexEntry:
    unit: EvidenceUnit
    normalized_fields: tuple[str, ...]
    bigrams: frozenset[str]
    concepts: frozenset[str]


class CorpusIndex:
    def __init__(self, entries: tuple[_IndexEntry, ...]) -> None:
        self._entries = entries

    @classmethod
    def build(
        cls,
        units: Sequence[EvidenceUnit],
    ) -> CorpusIndex:
        seen: set[str] = set()
        entries: list[_IndexEntry] = []
        for unit in units:
            if unit.source_tier is SourceTier.MODEL_INFERENCE:
                continue
            if unit.evidence_id in seen:
                raise ValueError(f"duplicate evidence_id: {unit.evidence_id}")
            seen.add(unit.evidence_id)
            normalized_fields = tuple(
                term
                for field in (unit.title, unit.text)
                for term in _query_terms(field)
            )
            declared_concepts = (
                _canonical_concept(concept) for concept in unit.concepts
            )
            concepts = frozenset(
                (
                    *_concepts_in_text(unit.title),
                    *_concepts_in_text(unit.text),
                    *declared_concepts,
                )
            )
            entries.append(
                _IndexEntry(
                    unit=unit,
                    normalized_fields=normalized_fields,
                    bigrams=(
                        frozenset().union(
                            *(char_bigrams(value) for value in normalized_fields)
                        )
                        - _SHORT_ALIAS_BIGRAMS
                    ),
                    concepts=concepts,
                )
            )
        entries.sort(key=lambda entry: entry.unit.evidence_id)
        return cls(tuple(entries))

    def __len__(self) -> int:
        return len(self._entries)

    def units_for(self, evidence_ids: Iterable[str]) -> tuple[EvidenceUnit, ...]:
        units = {entry.unit.evidence_id: entry.unit for entry in self._entries}
        return tuple(
            units[evidence_id]
            for evidence_id in dict.fromkeys(evidence_ids)
            if evidence_id in units
        )

    def search(self, query: EvidenceQuery, limit: int = 8) -> tuple[EvidenceHit, ...]:
        if isinstance(limit, bool) or not isinstance(limit, int) or limit <= 0:
            raise ValueError("limit must be positive integer")

        requested_concepts = _unique(
            (
                *(_canonical_concept(concept) for concept in query.concepts),
                *_concepts_in_text(query.text),
            )
        )
        query_terms = tuple(
            term for term in _query_terms(query.text) if len(term) >= 2
        )
        query_bigrams = (
            frozenset().union(
                *(
                    char_bigrams(term)
                    for term in query_terms
                    if term not in _SHORT_ALIAS_TO_CONCEPT
                )
            )
            - _SHORT_ALIAS_BIGRAMS
        )
        allowed_tiers = frozenset(query.source_tiers)
        excluded_ids = frozenset(query.exclude_evidence_ids)
        hits: list[EvidenceHit] = []

        for entry in self._entries:
            if allowed_tiers and entry.unit.source_tier not in allowed_tiers:
                continue
            if entry.unit.evidence_id in excluded_ids:
                continue

            matched_concepts = tuple(
                concept for concept in requested_concepts if concept in entry.concepts
            )
            matched_terms = tuple(
                term
                for term in query_terms
                if any(term in field for field in entry.normalized_fields)
                and (
                    term not in _SHORT_ALIAS_TO_CONCEPT
                    or _SHORT_ALIAS_TO_CONCEPT[term] in entry.concepts
                )
            )
            common_bigrams = query_bigrams & entry.bigrams
            overlap = len(common_bigrams) / max(1, len(entry.bigrams))
            if not matched_concepts and not matched_terms and not common_bigrams:
                continue

            term_ratio = len(matched_terms) / max(1, len(query_terms))
            source_weight = SOURCE_WEIGHT[entry.unit.source_tier]
            if matched_concepts:
                score = (
                    1000.0 * len(matched_concepts)
                    + 100.0 * source_weight
                    + term_ratio
                    + 0.01 * overlap
                )
            else:
                score = 10.0 * term_ratio + overlap + 0.01 * source_weight
            if score <= 0:
                continue
            hits.append(
                EvidenceHit(
                    unit=entry.unit,
                    score=score,
                    matched_terms=matched_terms,
                    matched_concepts=matched_concepts,
                    bigram_overlap=overlap,
                )
            )

        hits.sort(key=lambda hit: (-hit.score, hit.unit.evidence_id))
        return tuple(hits[:limit])
