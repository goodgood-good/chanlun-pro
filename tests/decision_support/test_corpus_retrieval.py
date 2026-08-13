from __future__ import annotations

import pytest

from chanlun.decision_support.corpus_retrieval import (
    CorpusIndex,
    EvidenceQuery,
)
from chanlun.decision_support.corpus_types import (
    EvidenceUnit,
    SourceTier,
)


def make_unit(
    evidence_id: str,
    source_tier: SourceTier | str,
    text: str,
    *,
    image_ids: tuple[str, ...] = (),
    concepts: tuple[str, ...] = (),
) -> EvidenceUnit:
    return EvidenceUnit(
        evidence_id=evidence_id,
        source_tier=SourceTier(source_tier),
        source_path=f"fixture/{evidence_id}.md",
        title=evidence_id,
        text=text,
        image_ids=image_ids,
        concepts=concepts,
    )


def test_search_prefers_original_exact_concept_over_secondary() -> None:
    original = make_unit(
        "o",
        SourceTier.LESSON_ORIGINAL,
        "第三类买点形成后，回试不进入中枢，继续向上。",
    )
    secondary = make_unit(
        "s",
        SourceTier.SECONDARY_ANNOTATION,
        "第三类买点图解。",
    )

    hits = CorpusIndex.build([secondary, original]).search(
        EvidenceQuery(text="第三类买点 中枢 回试"), limit=2
    )

    assert [hit.unit.evidence_id for hit in hits] == ["o", "s"]
    assert hits[0].matched_concepts == ("三类买点", "中枢回试")
    assert hits[0].score > hits[1].score


def test_original_exact_concept_outranks_short_secondary_exact_concept() -> None:
    original = EvidenceUnit(
        evidence_id="long-original",
        source_tier=SourceTier.LESSON_ORIGINAL,
        source_path="fixture/original.md",
        title="",
        text="第三类买点" + "".join(chr(0x4E00 + index) for index in range(200)),
    )
    secondary = EvidenceUnit(
        evidence_id="short-secondary",
        source_tier=SourceTier.SECONDARY_ANNOTATION,
        source_path="fixture/secondary.md",
        title="",
        text="第三类买点",
    )

    hits = CorpusIndex.build([secondary, original]).search(
        EvidenceQuery(text="第三类买点"), limit=2
    )

    assert [hit.unit.evidence_id for hit in hits] == [
        "long-original",
        "short-secondary",
    ]

def test_original_concept_priority_survives_secondary_short_alias_literal() -> None:
    original = EvidenceUnit(
        evidence_id="canonical-original",
        source_tier=SourceTier.LESSON_ORIGINAL,
        source_path="fixture/canonical-original.md",
        title="",
        text="第三类买点" + "".join(chr(0x5000 + index) for index in range(200)),
    )
    secondary = EvidenceUnit(
        evidence_id="alias-secondary",
        source_tier=SourceTier.SECONDARY_ANNOTATION,
        source_path="fixture/alias-secondary.md",
        title="",
        text="三买",
    )

    hits = CorpusIndex.build([secondary, original]).search(
        EvidenceQuery(text="三买"), limit=2
    )

    assert [hit.unit.evidence_id for hit in hits] == [
        "canonical-original",
        "alias-secondary",
    ]

def test_search_keeps_image_ids_on_hit() -> None:
    unit = make_unit(
        "chart",
        SourceTier.SECONDARY_ANNOTATION,
        "区间套定位示意图。",
        image_ids=("img-1",),
    )

    hit = CorpusIndex.build([unit]).search(EvidenceQuery(text="区间套"), limit=1)[0]

    assert hit.unit.image_ids == ("img-1",)


def test_units_for_resolves_exact_ids_in_requested_order() -> None:
    first = make_unit("first", SourceTier.LESSON_ORIGINAL, "第一类买点")
    second = make_unit("second", SourceTier.LESSON_ORIGINAL, "第三类买点")
    index = CorpusIndex.build((first, second))

    assert index.units_for(("second", "missing", "first", "second")) == (
        second,
        first,
    )


def test_model_inference_is_never_indexed() -> None:
    inference = make_unit(
        "model-only",
        SourceTier.MODEL_INFERENCE,
        "第三类买点 区间套 中枢回试",
    )

    index = CorpusIndex.build([inference])

    assert len(index) == 0
    assert index.search(EvidenceQuery(text="第三类买点")) == ()


def test_search_ties_are_deterministic_by_evidence_id() -> None:
    later = make_unit("z-unit", SourceTier.SECONDARY_ANNOTATION, "区间套")
    earlier = make_unit("a-unit", SourceTier.SECONDARY_ANNOTATION, "区间套")
    query = EvidenceQuery(text="区间套")

    first = CorpusIndex.build([later, earlier]).search(query)
    second = CorpusIndex.build([earlier, later]).search(query)

    assert [hit.unit.evidence_id for hit in first] == ["a-unit", "z-unit"]
    assert first == second


def test_model_inference_id_collision_cannot_block_trusted_index() -> None:
    trusted = make_unit("same", SourceTier.LESSON_ORIGINAL, "第三类买点")
    inference = make_unit("same", SourceTier.MODEL_INFERENCE, "模型判断")

    for units in ([inference, trusted], [trusted, inference]):
        index = CorpusIndex.build(units)
        assert len(index) == 1
        assert index.search(EvidenceQuery(text="第三类买点"))[0].unit == trusted

def test_build_rejects_duplicate_evidence_ids() -> None:
    first = make_unit("duplicate", SourceTier.LESSON_ORIGINAL, "一类买点")
    second = make_unit("duplicate", SourceTier.SECONDARY_ANNOTATION, "三类买点")

    with pytest.raises(ValueError, match="duplicate evidence_id"):
        CorpusIndex.build([first, second])


def test_search_normalizes_nfkc_case_punctuation_and_whitespace() -> None:
    unit = make_unit(
        "normalized",
        SourceTier.PROJECT_IMPLEMENTATION,
        "ＡＢＣ：第三类买点；回试中枢。",
    )

    hit = CorpusIndex.build([unit]).search(
        EvidenceQuery(text="abc 第三类买点 中枢回试"), limit=1
    )[0]

    assert hit.unit.evidence_id == "normalized"
    assert hit.matched_concepts == ("三类买点", "中枢回试")
    assert "abc" in hit.matched_terms


def test_short_alias_requires_a_standalone_token() -> None:
    ordinary = make_unit(
        "ordinary",
        SourceTier.SECONDARY_ANNOTATION,
        "统一买入后，周一卖出。",
    )
    standalone = make_unit("standalone", SourceTier.LESSON_ORIGINAL, "三买")
    index = CorpusIndex.build([ordinary, standalone])

    assert index.search(EvidenceQuery(text="一类买点")) == ()
    assert index.search(EvidenceQuery(text="第三类买点"))[0].unit == standalone

def test_short_alias_query_cannot_use_substring_or_cross_field_similarity() -> None:
    ordinary = make_unit(
        "ordinary-query-target",
        SourceTier.SECONDARY_ANNOTATION,
        "统一买入后，周一卖出。",
    )
    cross_field = EvidenceUnit(
        evidence_id="cross-field",
        source_tier=SourceTier.SECONDARY_ANNOTATION,
        source_path="fixture/cross-field.md",
        title="三",
        text="买",
    )
    index = CorpusIndex.build([ordinary, cross_field])

    assert index.search(EvidenceQuery(text="一买")) == ()
    assert index.search(EvidenceQuery(text="一卖")) == ()
    assert index.search(EvidenceQuery(text="三买")) == ()

def test_short_alias_cannot_be_reassembled_across_separators() -> None:
    for separator in ("_", "＿", "-"):
        separated = make_unit(
            f"separated-{ord(separator)}",
            SourceTier.SECONDARY_ANNOTATION,
            f"三{separator}买",
        )
        standalone = make_unit(
            f"standalone-{ord(separator)}",
            SourceTier.SECONDARY_ANNOTATION,
            "三买",
        )

        assert CorpusIndex.build([separated]).search(EvidenceQuery(text="三买")) == ()
        assert CorpusIndex.build([standalone]).search(
            EvidenceQuery(text=f"三{separator}买")
        ) == ()

def test_long_query_cannot_match_only_a_short_alias_bigram() -> None:
    short_buy = make_unit("short-buy", SourceTier.SECONDARY_ANNOTATION, "一买")
    short_sell = make_unit("short-sell", SourceTier.SECONDARY_ANNOTATION, "一卖")
    index = CorpusIndex.build([short_buy, short_sell])

    assert index.search(EvidenceQuery(text="统一买入")) == ()
    assert index.search(EvidenceQuery(text="周一卖出")) == ()

def test_search_filters_source_tiers_and_excluded_ids() -> None:
    original = make_unit("original", SourceTier.LESSON_ORIGINAL, "区间套")
    project = make_unit("project", SourceTier.PROJECT_IMPLEMENTATION, "区间套")
    secondary = make_unit("secondary", SourceTier.SECONDARY_ANNOTATION, "区间套")
    query = EvidenceQuery(
        text="区间套",
        source_tiers=(SourceTier.PROJECT_IMPLEMENTATION, SourceTier.SECONDARY_ANNOTATION),
        exclude_evidence_ids=("secondary",),
    )

    hits = CorpusIndex.build([original, project, secondary]).search(query)

    assert [hit.unit.evidence_id for hit in hits] == ["project"]


def test_search_returns_empty_for_no_match() -> None:
    unit = make_unit("unit", SourceTier.LESSON_ORIGINAL, "中枢震荡")

    hits = CorpusIndex.build([unit]).search(EvidenceQuery(text="完全无关的词"))

    assert hits == ()


def test_search_rejects_non_positive_limit() -> None:
    index = CorpusIndex.build([])

    with pytest.raises(ValueError, match="limit must be positive"):
        index.search(EvidenceQuery(text="区间套"), limit=0)


def test_search_rejects_non_integer_limit() -> None:
    index = CorpusIndex.build([])

    for invalid_limit in (1.5, True, "1"):
        with pytest.raises(ValueError, match="limit must be positive"):
            index.search(EvidenceQuery(text="区间套"), limit=invalid_limit)
